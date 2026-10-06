"""Discover -> durable plan -> fresh guards -> reserved intent -> recorded outcome."""

import copy
import hashlib
import uuid
from collections import Counter
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from .aws import Deadline, ScopeError
from .config import narrow
from .policy import fingerprint, iso, lifecycle, tags, timestamp
from .store import StateError

RESOURCE_ERRORS = {
    "InvalidInstanceID.NotFound",
    "IncorrectInstanceState",
    "OperationNotPermitted",
    "UnsupportedOperation",
    "InvalidParameterValue",
    "ResourceNotFoundException",
    "ServiceNotFoundException",
    "ResourceInUseException",
}
AUTH_ERRORS = {
    "UnauthorizedOperation",
    "AccessDenied",
    "AccessDeniedException",
    "AuthFailure",
    "ExpiredToken",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
}


def error_code(exc):
    return exc.response["Error"]["Code"] if isinstance(exc, ClientError) else type(exc).__name__


def run_id_for(event):
    if "schedule_arn" in event:
        value = event["schedule_arn"] + "\n" + event["scheduled_time"]
        return hashlib.sha256(value.encode()).hexdigest()[:32]
    return uuid.uuid4().hex


def run(operator, event, aws, store, *, clock=None, remaining_ms=None):
    policy = narrow(operator, event)
    clock = clock or (lambda: datetime.now(timezone.utc))
    remaining_ms = remaining_ms or (lambda: 900_000)

    def check_time():
        if remaining_ms() < 60_000:
            raise Deadline("Less than 60 seconds remain; no further action may begin")

    check_time()
    aws.verify_account()
    run_id = run_id_for(event)
    run_key = f"run#{run_id}"
    # Strong read permits delivery deduplication without touching a completed run.
    previous = store.get(run_key)
    if previous and previous["status"] in ("complete", "partial", "failed"):
        return {**previous, "duplicate": True}
    started = clock()
    retention = int(started.timestamp()) + policy.report_retention_days * 86400
    store.acquire(run_id, iso(started))
    report = {
        "schema": 1,
        "run_id": run_id,
        "scope": policy.scope,
        "started_at": iso(started),
        "status": "discovering",
        "dry_run": policy.dry_run,
        "policy": policy.public(),
        "discovered": 0,
        "reserved": 0,
        "counts": {},
    }
    store.write({run_key: report}, retention=retention)
    plans = []
    states = {}
    selected = event.get("resource_ids", event.get("instance_ids"))
    requested = set(selected) if selected is not None else None

    def plan_key(plan):
        return f"plan#{run_id}#{plan['resource_id']}"

    def save_plan(plan, state=None):
        records = {plan_key(plan): plan}
        if state is not None:
            records[f"resource#{plan['resource_id']}"] = state
        store.write(records, retention=retention)

    def finish():
        report["counts"] = dict(Counter(p["outcome"] for p in plans))
        report["reasons"] = dict(Counter(p["reason"] for p in plans))
        report["finished_at"] = iso(clock())
        if policy.notification_topic and (report["reserved"] or report["status"] != "complete"):
            try:
                aws.notify(report)
                report["notification"] = "submitted"
            except Exception as exc:
                report["notification"] = error_code(exc)
                report["status"] = "partial" if report["status"] == "complete" else report["status"]
        store.write({run_key: report}, retention=retention, finish=True)
        return report

    try:
        seen = set()
        for instance in aws.discover(check_time):
            check_time()
            iid = instance["InstanceId"]
            if iid in seen:
                continue
            seen.add(iid)
            report["discovered"] += 1
            if report["discovered"] > policy.max_resources:
                raise ScopeError(
                    "Discovery limit exceeded; narrow the fleet before enabling cleanup"
                )
            if requested is not None and iid not in requested:
                continue
            state = store.get(f"resource#{iid}") or {"first_seen_at": iso(clock())}
            # Persist observation but evaluate lifecycle only on a fresh read during execution.
            reason_action, reason = lifecycle(instance, copy.deepcopy(state), policy, clock())
            plan = {
                "resource_id": iid,
                "kind": instance.get("Kind", "ec2"),
                "owner": tags(instance).get("Owner", ""),
                "name": tags(instance).get("Name", ""),
                "action": reason_action,
                "reason": reason,
                "outcome": "planned",
                "discovered_state": instance.get("State", {}).get("Name"),
                "delete_on_termination_volumes": [
                    b["Ebs"]["VolumeId"]
                    for b in instance.get("BlockDeviceMappings", [])
                    if b.get("Ebs", {}).get("DeleteOnTermination")
                ],
                "observed_at": iso(clock()),
            }
            plans.append(plan)
            states[iid] = state
            save_plan(plan, state)
        report["status"] = "executing"
        store.write({run_key: report}, retention=retention)
    except StateError:
        raise  # Retain the lock and the last durable checkpoint.
    except Exception as exc:
        report.update(status="failed", error=error_code(exc))
        for plan in plans:
            plan.update(outcome="not_attempted", reason="discovery_incomplete")
            save_plan(plan)
        return finish()

    # Stable order makes reports repeatable. Caps are applied after fresh eligibility checks.
    priority = {"scheduler": 0, "eventbridge": 1, "lambda": 2, "ecs": 3, "eks": 4, "ec2": 5}
    plans.sort(key=lambda p: (priority[p["kind"]], p["resource_id"]))
    abort = None
    budget = store.get("budget") or {
        "policy": [operator.max_actions_per_window, operator.window_seconds],
        "reservations": [],
    }
    if budget["policy"] != [operator.max_actions_per_window, operator.window_seconds]:
        abort = "budget_policy_changed"
    for plan in plans:
        iid = plan["resource_id"]
        state = states[iid]
        if abort:
            plan.update(outcome="not_attempted", reason=abort)
            save_plan(plan)
            continue
        try:
            check_time()
            fresh = aws.refresh(iid)
            action, reason = lifecycle(fresh, state, policy, clock())
            plan.update(action=action, reason=reason)
            if not action:
                plan["outcome"] = "blocked" if reason == "unresolved_action" else "skipped"
                save_plan(plan, state)
                continue
            protection = aws.protection(iid, action, fresh)
            if protection:
                plan.update(outcome="skipped", reason=protection)
                save_plan(plan, state)
                continue
            if report["reserved"] >= policy.max_actions_per_run:
                plan.update(outcome="skipped", reason="run_budget_exhausted")
                save_plan(plan, state)
                continue
            now = clock()
            budget["reservations"] = [
                r
                for r in budget["reservations"]
                if (now - timestamp(r["at"])).total_seconds() < operator.window_seconds
            ]
            if len(budget["reservations"]) >= operator.max_actions_per_window:
                plan.update(outcome="skipped", reason="shared_budget_exhausted")
                save_plan(plan, state)
                continue
            if policy.dry_run:
                report["reserved"] += 1  # Preview count only; shared budget is unchanged.
                plan.update(outcome="would_act", reason=reason)
                save_plan(plan, state)
                continue
            check_time()
            # Refresh again after auxiliary API checks, immediately before reserving the action.
            latest = aws.refresh(iid)
            if (
                fingerprint(latest, stopped=True) != fingerprint(fresh, stopped=True)
                or latest["State"]["Name"] != fresh["State"]["Name"]
            ):
                plan.update(outcome="skipped", reason="changed_before_action")
                save_plan(plan, state)
                continue
            check_time()
            store.assert_owned()
            action_token = hashlib.sha256(f"{run_id}\n{iid}\n{action}".encode()).hexdigest()
            intent = {
                "run_id": run_id,
                "action": action,
                "at": iso(clock()),
                "action_token": action_token,
            }
            state["pending"] = intent
            budget["reservations"].append({**intent, "resource_id": iid})
            plan.update(outcome="pending", intent=intent)
            if "Snapshot" in latest:
                plan["before"] = latest["Snapshot"]
            # Intent, rolling budget, and plan outcome commit together, before workload mutation.
            store.write(
                {"budget": budget, f"resource#{iid}": state, plan_key(plan): plan},
                retention=retention,
            )
            report["reserved"] += 1
            try:
                response = aws.act(iid, action, latest, action_token)
                state.pop("pending")
                if action == "stop" and response.get("started"):
                    state["stop"] = {**intent, "fingerprint": fingerprint(latest)}
                elif action == "terminate":
                    state["terminated"] = intent
                elif action != "stop":
                    state["quarantine"] = {**intent, "before": latest["Snapshot"]}
                plan.update(
                    outcome="submitted",
                    reason="api_accepted",
                    aws_request_id=response.get("request_id"),
                    operation_id=response.get("operation_id"),
                )
            except Exception as exc:
                code = error_code(exc)
                if code in RESOURCE_ERRORS | AUTH_ERRORS:
                    state.pop("pending")
                    plan.update(outcome="failed", reason=code)
                    if code in AUTH_ERRORS:
                        abort = code
                else:
                    # Timeouts, throttles, server errors and malformed responses are ambiguous.
                    plan.update(outcome="unknown", reason=code)
                    abort = "uncertain_mutation"
            save_plan(plan, state)
        except StateError:
            raise
        except Exception as exc:
            code = error_code(exc)
            plan.update(outcome="failed", reason=code)
            if code not in RESOURCE_ERRORS:
                abort = code
            save_plan(plan)
    bad = any(p["outcome"] in ("failed", "unknown", "blocked") for p in plans)
    report["status"] = "failed" if abort else "partial" if bad else "complete"
    if abort:
        report["error"] = abort
    return finish()
