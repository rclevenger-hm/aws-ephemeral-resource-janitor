import copy
from dataclasses import replace
from datetime import timedelta

import pytest
from botocore.exceptions import ClientError
from conftest import NOW, Fleet, instance

from janitor.config import ConfigError, Policy, narrow
from janitor.engine import run
from janitor.policy import evaluate, iso
from janitor.services import normalized
from janitor.store import RunLocked, StateError


def execute(policy, store, fleet, **kwargs):
    return run(policy, {}, fleet, store, clock=lambda: NOW, **kwargs)


def error(code):
    return ClientError({"Error": {"Code": code, "Message": "test failure"}}, "StopInstances")


@pytest.mark.parametrize(
    "payload",
    [
        {"dry_run": False},
        {"dry_run": "false"},
        {"max_actions_per_run": None},
        {"max_actions_per_run": 11},
        {"max_actions_per_run": True},
        {"mode": "lifecycle"},
        {"account_id": "999999999999"},
        {"region": "us-west-2"},
        {"grace_hours": 0},
        {"resource_types": ["lambda"]},
        {"table_name": "different"},
        {"scheduler_role_arns": ["*"]},
        {"threshold_hours": 0},
        {"allow_terminate": True},
    ],
)
def test_payload_cannot_expand_operator_authority(payload):
    with pytest.raises(ConfigError):
        narrow(Policy("123456789012", "us-east-1", "janitor"), payload)


def test_payload_can_narrow(setup):
    p, _ = setup()
    q = narrow(p, {"dry_run": True, "max_actions_per_run": 1, "resource_ids": []})
    assert q.dry_run and q.max_actions_per_run == 1


@pytest.mark.parametrize(
    "ttl", ["NaN", "Infinity", "-Infinity", "1e100", "-1", "0", "87601", "bad"]
)
def test_malformed_ttl_is_individual_skip(setup, ttl):
    p, s = setup()
    bad = instance(1)
    bad["Tags"] = [{"Key": "JanitorManaged", "Value": "true"}, {"Key": "TTLHours", "Value": ttl}]
    fleet = Fleet([bad, instance(2)])
    report = execute(p, s, fleet)
    assert report["status"] == "complete"
    assert fleet.actions == [(instance(2)["InstanceId"], "stop")]


@pytest.mark.parametrize("expiry", ["bad", "2026-01-01", "NaN", "100000-01-01T00:00:00Z"])
def test_invalid_absolute_expiry_has_no_ttl_fallback(setup, expiry):
    p, _ = setup()
    assert evaluate(instance(extra_tags={"ExpiresAt": expiry}), {}, p, NOW) == "invalid_expiry"


def test_first_observed_time_is_ttl_anchor(setup):
    p, s = setup()
    i = instance()
    i["Tags"] = [{"Key": "JanitorManaged", "Value": "true"}]
    i["LaunchTime"] = NOW - timedelta(days=365)
    fleet = Fleet([i])
    assert execute(p, s, fleet)["counts"] == {"skipped": 1}
    run(p, {}, fleet, s, clock=lambda: NOW + timedelta(hours=25))
    assert len(fleet.actions) == 1


def test_complete_plan_and_reservation_exist_before_first_mutation(setup):
    p, s = setup()
    fleet = Fleet([instance(1), instance(2)])

    def inspect(iid, action):
        plans = list(s.plans(s.owner))
        assert len(plans) == 2
        assert s.get(f"run#{s.owner}")["status"] == "executing"
        assert s.get(f"resource#{iid}")["pending"]["action"] == action
        assert any(r["resource_id"] == iid for r in s.get("budget")["reservations"])

    fleet.act_hook = inspect
    report = execute(p, s, fleet)
    assert report["counts"] == {"submitted": 2}


def test_second_resource_failure_preserves_first_and_continues(setup):
    p, s = setup()
    fleet = Fleet([instance(n) for n in (1, 2, 3)])
    fleet.errors[instance(2)["InstanceId"]] = error("IncorrectInstanceState")
    report = execute(p, s, fleet)
    assert report["status"] == "partial"
    assert report["counts"] == {"submitted": 2, "failed": 1}
    assert s.get(f"run#{report['run_id']}") == report
    assert len(list(s.plans(report["run_id"]))) == 3
    assert len(s.get("budget")["reservations"]) == 3


@pytest.mark.parametrize(
    "failure", [error("UnauthorizedOperation"), TimeoutError(), error("Throttling")]
)
def test_global_or_uncertain_failure_aborts_remaining_actions(setup, failure):
    p, s = setup()
    fleet = Fleet([instance(1), instance(2)])
    fleet.errors[instance(1)["InstanceId"]] = failure
    report = execute(p, s, fleet)
    assert report["status"] == "failed"
    assert len(fleet.actions) == 1
    assert report["counts"]["not_attempted"] == 1
    pending = s.get(f"resource#{instance(1)['InstanceId']}").get("pending")
    assert bool(pending) == (
        not isinstance(failure, ClientError)
        or failure.response["Error"]["Code"] != "UnauthorizedOperation"
    )


def test_unknown_action_is_not_replayed(setup):
    p, s = setup()
    fleet = Fleet([instance()])
    fleet.errors[instance()["InstanceId"]] = TimeoutError()
    execute(p, s, fleet)
    fleet.errors.clear()
    report = execute(p, s, fleet)
    assert report["counts"] == {"blocked": 1}
    assert len(fleet.actions) == 1


def test_failed_post_action_checkpoint_keeps_lock_and_pending(setup, monkeypatch):
    p, s = setup()
    original = s.write

    def fail(records, **kwargs):
        if any(v.get("outcome") == "submitted" for v in records.values()):
            raise StateError("storage unavailable")
        return original(records, **kwargs)

    monkeypatch.setattr(s, "write", fail)
    fleet = Fleet([instance(1), instance(2)])
    with pytest.raises(StateError):
        execute(p, s, fleet)
    assert len(fleet.actions) == 1
    assert s.get(f"resource#{instance(1)['InstanceId']}")["pending"]
    _, other = setup()
    with pytest.raises(RunLocked):
        execute(p, other, fleet)


def test_journal_failure_before_action_never_mutates(setup, monkeypatch):
    p, s = setup()
    original = s.write

    def fail(records, **kwargs):
        if "budget" in records:
            raise StateError("transaction failed")
        return original(records, **kwargs)

    monkeypatch.setattr(s, "write", fail)
    fleet = Fleet([instance()])
    with pytest.raises(StateError):
        execute(p, s, fleet)
    assert not fleet.actions


def test_two_workers_cannot_acquire_same_scope(setup):
    p, first = setup()
    _, second = setup()
    first.acquire("worker-one", iso(NOW))
    with pytest.raises(RunLocked):
        execute(p, second, Fleet([instance()]))


def test_lost_lock_cannot_write_journal(setup):
    _, s = setup()
    s.acquire("one", iso(NOW))
    s.owner = "two"
    with pytest.raises(StateError):
        s.write({"resource#example": {"pending": True}})
    assert s.get("resource#example") is None


def test_duplicate_scheduler_delivery_is_noop(setup):
    p, s = setup()
    fleet = Fleet([instance()])
    event = {
        "schedule_arn": "arn:aws:scheduler:us-east-1:123456789012:schedule/g/s",
        "scheduled_time": iso(NOW),
    }
    first = run(p, event, fleet, s, clock=lambda: NOW)
    again = run(p, event, fleet, s, clock=lambda: NOW)
    assert again["duplicate"] and first["run_id"] == again["run_id"]
    assert len(fleet.actions) == 1


def test_shared_budget_survives_new_invocations_and_clock_rollback(setup):
    p, s = setup(max_actions_per_run=1, max_actions_per_window=1)
    execute(p, s, Fleet([instance(1)]))
    fleet = Fleet([instance(2)])
    report = run(p, {}, fleet, s, clock=lambda: NOW - timedelta(hours=1))
    assert report["reasons"] == {"shared_budget_exhausted": 1}
    assert not fleet.actions
    run(p, {}, fleet, s, clock=lambda: NOW + timedelta(hours=1))
    assert len(fleet.actions) == 1


def test_changed_budget_policy_requires_reconciliation(setup):
    p, s = setup()
    execute(p, s, Fleet([instance()]))
    report = execute(replace(p, max_actions_per_window=20), s, Fleet([instance(2)]))
    assert report["error"] == "budget_policy_changed"


def test_dry_run_writes_audit_without_action_or_shared_reservation(setup):
    p, s = setup(dry_run=True)
    fleet = Fleet([instance()])
    report = execute(p, s, fleet)
    assert report["counts"] == {"would_act": 1}
    assert not fleet.actions and s.get("budget") is None


def test_stale_tags_are_rechecked(setup):
    p, s = setup()
    fleet = Fleet([instance()])

    def change(f, iid):
        f.instances[iid]["Tags"].append({"Key": "DoNotCleanup", "Value": "false"})

    fleet.refresh_hook = change
    report = execute(p, s, fleet)
    assert report["reasons"] == {"excluded": 1} and not fleet.actions


def test_change_after_protection_read_is_rejected(setup):
    p, s = setup()
    fleet = Fleet([instance()])

    def change(f, iid):
        if f.refreshes == 2:
            f.instances[iid]["Tags"].append({"Key": "Owner", "Value": "new"})

    fleet.refresh_hook = change
    report = execute(p, s, fleet)
    assert report["reasons"] == {"changed_before_action": 1} and not fleet.actions


def test_termination_requires_own_stop_and_full_observed_grace(setup):
    p, s = setup(mode="lifecycle", grace_hours=24)
    i = instance(extra_tags={"JanitorAllowTerminate": "true"})
    fleet = Fleet([i, instance(2, state="stopped", extra_tags={"JanitorAllowTerminate": "true"})])
    execute(p, s, fleet)
    report = run(p, {}, fleet, s, clock=lambda: NOW + timedelta(hours=10))
    assert report["reasons"] == {"recovery_grace": 1, "no_janitor_stop_history": 1}
    run(p, {}, fleet, s, clock=lambda: NOW + timedelta(hours=33))
    assert len(fleet.actions) == 1
    run(p, {}, fleet, s, clock=lambda: NOW + timedelta(hours=34))
    assert fleet.actions[-1] == (i["InstanceId"], "terminate")
    assert len(fleet.actions) == 2


def test_tag_change_invalidates_termination_history(setup):
    p, s = setup(mode="lifecycle")
    fleet = Fleet([instance(extra_tags={"JanitorAllowTerminate": "true"})])
    execute(p, s, fleet)
    fleet.instances[instance()["InstanceId"]]["Tags"].append({"Key": "Owner", "Value": "new"})
    report = run(p, {}, fleet, s, clock=lambda: NOW + timedelta(days=2))
    assert report["reasons"] == {"no_janitor_stop_history": 1}


def test_ineligible_resources_do_not_consume_run_cap(setup):
    p, s = setup(max_actions_per_run=1)
    fleet = Fleet([instance(1, "stopped"), instance(2)])
    execute(p, s, fleet)
    assert fleet.actions == [(instance(2)["InstanceId"], "stop")]


def test_discovery_failure_never_executes_partial_plan(setup):
    p, s = setup()
    fleet = Fleet([instance()])
    fleet.discovery_error = TimeoutError()
    report = execute(p, s, fleet)
    assert report["status"] == "failed" and not fleet.actions
    assert report["counts"] == {"not_attempted": 1}


def test_discovery_limit_never_executes_truncated_plan(setup):
    p, s = setup(max_resources=1)
    fleet = Fleet([instance(1), instance(2)])
    assert execute(p, s, fleet)["status"] == "failed"
    assert not fleet.actions


def test_deadline_stops_before_mutation(setup):
    p, s = setup()
    fleet = Fleet([instance()])
    time_left = iter([300000, 250000, 59000])
    report = execute(p, s, fleet, remaining_ms=lambda: next(time_left, 59000))
    assert report["status"] == "failed" and not fleet.actions


@pytest.mark.parametrize(
    "kind,action",
    [
        ("ecs", "scale_service_zero"),
        ("eks", "scale_nodegroup_zero"),
        ("lambda", "quarantine_function"),
        ("eventbridge", "disable_rule"),
        ("scheduler", "disable_schedule"),
    ],
)
def test_service_actions_preserve_original_settings_and_do_not_replay(setup, kind, action):
    p, s = setup()
    resource = normalized(
        kind,
        f"arn:aws:{kind}:us-east-1:123456789012:resource/test",
        {"JanitorManaged": "true", "ExpiresAt": "2026-01-01T00:00:00Z"},
        {"original_capacity": 3},
        "running",
    )
    fleet = Fleet([resource])
    report = execute(p, s, fleet)
    assert fleet.actions == [(resource["InstanceId"], action)]
    plan = list(s.plans(report["run_id"]))[0]
    assert plan["before"] == {"original_capacity": 3}
    fleet.instances[resource["InstanceId"]]["State"]["Name"] = "running"
    execute(p, s, fleet)
    assert len(fleet.actions) == 1


def test_resource_request_is_subset_not_a_new_discovery_scope(setup):
    p, s = setup()
    fleet = Fleet([instance(1), instance(2)])
    run(p, {"resource_ids": [instance(2)["InstanceId"]]}, fleet, s, clock=lambda: NOW)
    assert fleet.actions == [(instance(2)["InstanceId"], "stop")]


def test_protected_self_resource_never_quarantined(setup):
    arn = "arn:aws:lambda:us-east-1:123456789012:function:janitor"
    p, s = setup(protected_arns=[arn])
    resource = normalized(
        "lambda",
        arn,
        {"JanitorManaged": "true", "ExpiresAt": iso(NOW)},
        {"reserved_concurrency": 1},
        "running",
    )
    fleet = Fleet([resource])
    report = execute(p, s, fleet)
    assert report["reasons"] == {"janitor_infrastructure": 1} and not fleet.actions


def test_notification_failure_remains_visible(setup):
    p, s = setup(notification_topic="arn:aws:sns:us-east-1:123456789012:notify")
    fleet = Fleet([instance()])

    def fail(summary):
        raise TimeoutError()

    fleet.notify = fail
    report = execute(p, s, fleet)
    assert report["status"] == "partial" and report["notification"] == "TimeoutError"
    assert s.get(f"run#{report['run_id']}")["notification"] == "TimeoutError"


def test_planning_does_not_mutate_nested_history(setup):
    p, s = setup(mode="lifecycle")
    fleet = Fleet([instance(extra_tags={"JanitorAllowTerminate": "true"})])
    execute(p, s, fleet)
    before = copy.deepcopy(s.get(f"resource#{instance()['InstanceId']}"))
    fleet.discovery_error = TimeoutError()
    execute(p, s, fleet)
    assert s.get(f"resource#{instance()['InstanceId']}")["stop"] == before["stop"]


def test_idempotency_tokens_are_unique_for_each_resource(setup):
    p, s = setup()
    fleet = Fleet([instance(1), instance(2)])
    execute(p, s, fleet)
    assert len(set(fleet.tokens)) == 2


def test_running_instance_with_unconfirmed_stop_is_not_stopped_again(setup):
    p, s = setup()
    fleet = Fleet([instance()])
    execute(p, s, fleet)
    fleet.instances[instance()["InstanceId"]]["State"]["Name"] = "running"
    report = execute(p, s, fleet)
    assert report["reasons"] == {"stop_submitted": 1}
    assert len(fleet.actions) == 1


def test_stopped_transition_change_resets_grace(setup):
    p, s = setup(mode="lifecycle", grace_hours=1)
    fleet = Fleet([instance(extra_tags={"JanitorAllowTerminate": "true"})])
    execute(p, s, fleet)
    run(p, {}, fleet, s, clock=lambda: NOW + timedelta(minutes=1))
    fleet.instances[instance()["InstanceId"]]["StateTransitionReason"] = "new stop event"
    report = run(p, {}, fleet, s, clock=lambda: NOW + timedelta(hours=2))
    assert report["reasons"] == {"recovery_grace": 1}
    assert len(fleet.actions) == 1


def test_exclusions_for_managed_compute_and_spot(setup):
    p, s = setup()
    resources = [
        instance(1, extra_tags={"aws:autoscaling:groupName": "g"}),
        instance(2, extra_tags={"aws:cloudformation:stack-id": "stack"}),
        instance(3, extra_tags={"eks:cluster-name": "cluster"}),
        instance(4),
    ]
    resources[-1]["InstanceLifecycle"] = "spot"
    fleet = Fleet(resources)
    report = execute(p, s, fleet)
    assert not fleet.actions and report["counts"] == {"skipped": 4}
