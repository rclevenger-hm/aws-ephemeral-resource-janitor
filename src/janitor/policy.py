"""Pure eligibility decisions. Never infer creation time from EC2 LaunchTime."""

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

MANAGED_PREFIXES = (
    "aws:autoscaling:",
    "aws:cloudformation:",
    "aws:ec2:fleet",
    "aws:eks:",
    "eks:",
    "kubernetes.io/cluster/",
    "aws:elasticmapreduce:",
    "aws:ecs:",
)


def timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("timestamp must be an ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def iso(value):
    return timestamp(value).isoformat()


def tags(instance):
    return {t["Key"]: t["Value"] for t in instance.get("Tags", [])}


def fingerprint(instance, stopped=False):
    if instance.get("Kind", "ec2") != "ec2":
        data = {
            "tags": tags(instance),
            "snapshot": instance["Snapshot"],
            "state": instance["State"],
            "kind": instance["Kind"],
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    data = {
        "tags": tags(instance),
        "launch_time": iso(instance["LaunchTime"]),
        "volumes": sorted(
            (
                b.get("DeviceName", ""),
                b.get("Ebs", {}).get("VolumeId", ""),
                b.get("Ebs", {}).get("DeleteOnTermination", False),
            )
            for b in instance.get("BlockDeviceMappings", [])
        ),
    }
    if stopped:
        data["reason"] = instance.get("StateTransitionReason", "")
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def evaluate(instance, state, policy, now):
    t = tags(instance)
    if t.get("JanitorManaged") != "true":
        return "not_opted_in"
    if "DoNotCleanup" in t:
        return "excluded"
    if instance["InstanceId"] in policy.protected_arns:
        return "janitor_infrastructure"
    if instance.get("Kind", "ec2") == "ec2" and any(k.startswith(MANAGED_PREFIXES) for k in t):
        return "managed_resource"
    if instance.get("InstanceLifecycle") or instance.get("SpotInstanceRequestId"):
        return "non_on_demand"
    if instance.get("Kind", "ec2") == "ec2" and instance.get("RootDeviceType") != "ebs":
        return "unsupported_root_device"
    if instance.get("State", {}).get("Name") not in ("running", "stopped"):
        return "transitional_state"
    try:
        if "ExpiresAt" in t:
            expiry = timestamp(t["ExpiresAt"])
        else:
            ttl = float(t.get("TTLHours", policy.ttl_hours))
            if not math.isfinite(ttl) or not 0 < ttl <= 87600:
                return "invalid_ttl"
            expiry = timestamp(state["first_seen_at"]) + timedelta(hours=ttl)
    except (ValueError, TypeError, OverflowError, KeyError):
        return "invalid_expiry"
    return "expired" if expiry <= now else "not_expired"


def lifecycle(instance, state, policy, now):
    """Update observed history and return a proposed action and reason."""
    reason = evaluate(instance, state, policy, now)
    if instance.get("Kind", "ec2") != "ec2":
        if reason != "expired":
            if reason == "not_expired":
                state.pop("quarantine", None)
            return None, reason
        if state.get("pending"):
            return None, "unresolved_action"
        if instance["State"]["Name"] == "stopped":
            return None, "already_inactive"
        if state.get("quarantine"):
            return None, "quarantine_submitted_or_manually_restored"
        action = {
            "ecs": "scale_service_zero",
            "eks": "scale_nodegroup_zero",
            "lambda": "quarantine_function",
            "eventbridge": "disable_rule",
            "scheduler": "disable_schedule",
        }[instance["Kind"]]
        return action, reason
    history = state.get("stop")
    if history and history["fingerprint"] != fingerprint(instance):
        state.pop("stop", None)
        history = None
    if reason != "expired":
        # Opt-out/extension invalidates prior authority to terminate.
        if reason not in ("transitional_state",):
            state.pop("stop", None)
        return None, reason
    if state.get("pending"):
        return None, "unresolved_action"
    if state.get("terminated"):
        return None, "termination_submitted"
    if instance["State"]["Name"] == "running":
        if history:
            if history.get("observed_stopped_at"):
                state.pop("stop", None)
                return None, "restarted_history_reset"
            return None, "stop_submitted"
        return "stop", "expired"
    if policy.mode != "lifecycle":
        return None, "already_stopped"
    if tags(instance).get("JanitorAllowTerminate") != "true":
        return None, "termination_not_opted_in"
    if not history:
        return None, "no_janitor_stop_history"
    token = fingerprint(instance, stopped=True)
    if history.get("stopped_fingerprint") != token:
        history["stopped_fingerprint"] = token
        history["observed_stopped_at"] = iso(now)
    if (
        now - timestamp(history["observed_stopped_at"])
    ).total_seconds() < policy.grace_hours * 3600:
        return None, "recovery_grace"
    return "terminate", "grace_elapsed"
