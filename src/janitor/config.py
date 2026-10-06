"""Operator policy and a deliberately narrow invocation contract."""

import json
import math
import os
import re
from dataclasses import asdict, dataclass, replace


class ConfigError(ValueError):
    pass


def number(value, name, minimum, maximum, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    if not minimum <= value <= maximum or not math.isfinite(value):
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    if integer and not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    return value


@dataclass(frozen=True)
class Policy:
    account_id: str
    region: str
    table_name: str
    dry_run: bool = True
    mode: str = "stop"
    ttl_hours: float = 24
    grace_hours: float = 24
    max_actions_per_run: int = 10
    max_actions_per_window: int = 10
    window_seconds: int = 3600
    max_resources: int = 1000
    report_retention_days: int = 90
    notification_topic: str = ""
    resource_types: tuple = ("ec2", "ecs", "eks", "lambda", "eventbridge", "scheduler")
    scheduler_group: str = ""
    scheduler_role_arns: tuple = ()
    protected_arns: tuple = ()

    def __post_init__(self):
        if not isinstance(self.account_id, str) or not re.fullmatch(r"\d{12}", self.account_id):
            raise ConfigError("account_id must be an explicit 12-digit AWS account ID")
        if not isinstance(self.region, str) or not re.fullmatch(
            r"[a-z]{2}(?:-[a-z]+)+-\d", self.region
        ):
            raise ConfigError("region must be an explicit AWS region")
        if not isinstance(self.table_name, str) or not re.fullmatch(
            r"[\w.-]{3,255}", self.table_name
        ):
            raise ConfigError("table_name must be a DynamoDB table name")
        if type(self.dry_run) is not bool or self.mode not in ("stop", "lifecycle"):
            raise ConfigError("dry_run must be boolean and mode must be stop or lifecycle")
        for field in ("ttl_hours", "grace_hours"):
            number(getattr(self, field), field, 1 / 60, 87600)
        for field, maximum in (
            ("max_actions_per_run", 1000),
            ("max_actions_per_window", 1000),
            ("window_seconds", 86400),
            ("max_resources", 10000),
            ("report_retention_days", 3650),
        ):
            number(getattr(self, field), field, 1, maximum, integer=True)
        if self.max_actions_per_run > self.max_actions_per_window:
            raise ConfigError("per-run action limit must not exceed the shared window limit")
        kinds = {"ec2", "ecs", "eks", "lambda", "eventbridge", "scheduler"}
        if (
            not isinstance(self.resource_types, (list, tuple))
            or not set(self.resource_types) <= kinds
        ):
            raise ConfigError("resource_types contains an unsupported service")
        if not isinstance(self.scheduler_group, str) or (
            self.scheduler_group and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.scheduler_group)
        ):
            raise ConfigError("scheduler_group must be an explicit schedule group name")
        for field in ("scheduler_role_arns", "protected_arns"):
            values = getattr(self, field)
            if not isinstance(values, (list, tuple)) or any(
                not isinstance(v, str)
                or "*" in v
                or len(v.split(":")) < 6
                or v.split(":")[4] != self.account_id
                for v in values
            ):
                raise ConfigError(f"{field} must contain exact ARNs in the configured account")
        if self.notification_topic and not re.fullmatch(
            rf"arn:[a-z-]+:sns:{self.region}:{self.account_id}:[A-Za-z0-9_-]+",
            self.notification_topic,
        ):
            raise ConfigError("notification_topic must be a standard SNS topic in this scope")

    @property
    def scope(self):
        return f"scope#{self.account_id}#{self.region}"

    def public(self):
        return json.loads(json.dumps(asdict(self)))


def load_policy(environ=None):
    env = os.environ if environ is None else environ
    try:
        raw = json.loads(env["JANITOR_POLICY"])
        if not isinstance(raw, dict):
            raise ConfigError("JANITOR_POLICY must be a JSON object")
        if "JANITOR_RESOURCE_TYPES" in env:
            raw["resource_types"] = [v for v in env["JANITOR_RESOURCE_TYPES"].split(",") if v]
        if "JANITOR_SCHEDULER_ROLE_ARNS" in env:
            raw["scheduler_role_arns"] = [
                v for v in env["JANITOR_SCHEDULER_ROLE_ARNS"].split(",") if v
            ]
        return Policy(**raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid operator policy: {exc}") from exc


def narrow(policy, event):
    if not isinstance(event, dict):
        raise ConfigError("invocation must be a JSON object")
    allowed = {
        "dry_run",
        "max_actions_per_run",
        "instance_ids",
        "resource_ids",
        "schedule_arn",
        "scheduled_time",
    }
    if event.keys() - allowed:
        raise ConfigError(f"Unsupported request fields: {sorted(event.keys() - allowed)}")
    changes = {}
    if "dry_run" in event:
        if type(event["dry_run"]) is not bool or (policy.dry_run and not event["dry_run"]):
            raise ConfigError("requests cannot disable operator dry_run")
        changes["dry_run"] = event["dry_run"]
    if "max_actions_per_run" in event:
        changes["max_actions_per_run"] = number(
            event["max_actions_per_run"],
            "max_actions_per_run",
            1,
            policy.max_actions_per_run,
            integer=True,
        )
    if "instance_ids" in event:
        ids = event["instance_ids"]
        if (
            not isinstance(ids, list)
            or len(ids) > policy.max_resources
            or any(
                not isinstance(i, str) or not re.fullmatch(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", i)
                for i in ids
            )
        ):
            raise ConfigError("instance_ids must be a bounded list of EC2 instance IDs")
    if "resource_ids" in event:
        ids = event["resource_ids"]
        if (
            not isinstance(ids, list)
            or len(ids) > policy.max_resources
            or any(not isinstance(i, str) or not 1 <= len(i) <= 2048 for i in ids)
        ):
            raise ConfigError("resource_ids must be a bounded list of IDs or ARNs")
    if "resource_ids" in event and "instance_ids" in event:
        raise ConfigError("Use either resource_ids or instance_ids")
    if ("schedule_arn" in event) != ("scheduled_time" in event):
        raise ConfigError("schedule_arn and scheduled_time must be supplied together")
    for key in ("schedule_arn", "scheduled_time"):
        if key in event and (not isinstance(event[key], str) or not 1 <= len(event[key]) <= 512):
            raise ConfigError(f"invalid {key}")
    return replace(policy, **changes)
