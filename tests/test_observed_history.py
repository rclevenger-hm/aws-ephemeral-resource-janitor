"""Safety observations must survive runs that cannot finish executing their plans."""

import copy
from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import NOW, Fleet, instance

from janitor.engine import run


def stopped_fleet(setup):
    policy, store = setup(mode="lifecycle", grace_hours=24)
    resource = instance(extra_tags={"JanitorAllowTerminate": "true"})
    fleet = Fleet([resource])
    run(policy, {}, fleet, store, clock=lambda: NOW)
    run(policy, {}, fleet, store, clock=lambda: NOW + timedelta(hours=1))
    return policy, store, fleet, resource["InstanceId"]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("interruption", ["discovery", "refresh", "budget"])
def test_observed_opt_out_survives_interrupted_run(setup, dry_run, interruption):
    policy, store, fleet, iid = stopped_fleet(setup)
    original_tags = copy.deepcopy(fleet.instances[iid]["Tags"])
    fleet.instances[iid]["Tags"].append({"Key": "DoNotCleanup", "Value": "true"})
    if interruption == "discovery":
        fleet.discovery_error = TimeoutError()
    elif interruption == "refresh":

        def fail_refresh(*_):
            raise TimeoutError()

        fleet.refresh_hook = fail_refresh
    else:
        # A changed rolling-budget policy aborts execution after complete discovery.
        policy = replace(policy, max_actions_per_window=20)

    report = run(policy, {"dry_run": dry_run}, fleet, store, clock=lambda: NOW + timedelta(days=2))
    assert report["status"] == "failed"
    assert "stop" not in store.get(f"resource#{iid}")
    assert fleet.actions == [(iid, "stop")]

    fleet.discovery_error = None
    fleet.refresh_hook = None
    fleet.instances[iid]["Tags"] = original_tags
    if interruption == "budget":
        policy = replace(policy, max_actions_per_window=10)
    report = run(policy, {}, fleet, store, clock=lambda: NOW + timedelta(days=3))
    assert report["reasons"] == {"no_janitor_stop_history": 1}
    assert fleet.actions == [(iid, "stop")]


@pytest.mark.parametrize("change", ["expiry", "attachment", "launch", "restart"])
def test_discovery_preserves_other_invalidations(setup, change):
    policy, store, fleet, iid = stopped_fleet(setup)
    resource = fleet.instances[iid]
    if change == "expiry":
        resource["Tags"] = [
            {**tag, "Value": "2027-01-01T00:00:00Z"} if tag["Key"] == "ExpiresAt" else tag
            for tag in resource["Tags"]
        ]
    elif change == "attachment":
        resource["BlockDeviceMappings"] = [
            {"DeviceName": "/dev/sdf", "Ebs": {"VolumeId": "vol-123", "DeleteOnTermination": True}}
        ]
    elif change == "launch":
        resource["LaunchTime"] += timedelta(hours=1)
    else:
        resource["State"]["Name"] = "running"
    fleet.discovery_error = TimeoutError()

    report = run(policy, {}, fleet, store, clock=lambda: NOW + timedelta(days=2))
    assert report["status"] == "failed"
    assert "stop" not in store.get(f"resource#{iid}")
    assert fleet.actions == [(iid, "stop")]


def test_discovery_does_not_start_recovery_grace(setup):
    policy, store = setup(mode="lifecycle", grace_hours=24)
    resource = instance(extra_tags={"JanitorAllowTerminate": "true"})
    iid = resource["InstanceId"]
    fleet = Fleet([resource])
    run(policy, {}, fleet, store, clock=lambda: NOW)
    before = store.get(f"resource#{iid}")
    assert "observed_stopped_at" not in before["stop"]
    fleet.discovery_error = TimeoutError()

    run(policy, {}, fleet, store, clock=lambda: NOW + timedelta(hours=1))
    assert store.get(f"resource#{iid}") == before

    fleet.discovery_error = None
    report = run(policy, {}, fleet, store, clock=lambda: NOW + timedelta(days=2))
    assert report["reasons"] == {"recovery_grace": 1}
    assert fleet.actions == [(iid, "stop")]


def test_invalidating_history_keeps_pending_intent_and_budget(setup):
    policy, store, fleet, iid = stopped_fleet(setup)
    before = store.get(f"resource#{iid}")
    before["pending"] = {"action": "terminate", "run_id": "unresolved-run"}
    budget = store.get("budget")
    store.acquire("seed-pending", NOW.isoformat())
    store.write({f"resource#{iid}": before}, finish=True)
    fleet.instances[iid]["Tags"].append({"Key": "DoNotCleanup", "Value": "true"})
    fleet.discovery_error = TimeoutError()

    run(policy, {}, fleet, store, clock=lambda: NOW + timedelta(days=2))
    assert store.get(f"resource#{iid}") == {
        key: value for key, value in before.items() if key != "stop"
    }
    assert store.get("budget") == budget
    assert fleet.actions == [(iid, "stop")]


@pytest.mark.parametrize("failure", ["protection", "final_refresh"])
def test_first_refresh_invalidation_survives_later_read_failure(setup, monkeypatch, failure):
    policy, store = setup(mode="lifecycle", grace_hours=24)
    resource = instance(extra_tags={"JanitorAllowTerminate": "true"})
    iid = resource["InstanceId"]
    fleet = Fleet([resource])
    run(policy, {}, fleet, store, clock=lambda: NOW)
    # The accepted stop has not yet been observed stopped; discovery still sees
    # the old running snapshot. Only the first execution refresh sees new tags.
    fleet.instances[iid]["State"]["Name"] = "running"
    original_tags = copy.deepcopy(fleet.instances[iid]["Tags"])
    fleet.refreshes = 0

    def change_then_fail(fleet, resource_id):
        if fleet.refreshes == 1:
            fleet.instances[resource_id]["Tags"].append({"Key": "Name", "Value": "rescued"})
        elif failure == "final_refresh":
            raise TimeoutError()

    def protection_failure(*_):
        raise TimeoutError()

    fleet.refresh_hook = change_then_fail
    with monkeypatch.context() as patch:
        if failure == "protection":
            patch.setattr(fleet, "protection", protection_failure)
        report = run(policy, {}, fleet, store, clock=lambda: NOW + timedelta(hours=1))
    assert report["status"] == "failed"
    assert "stop" not in store.get(f"resource#{iid}")

    fleet.refresh_hook = None
    fleet.instances[iid]["Tags"] = original_tags
    fleet.instances[iid]["State"]["Name"] = "stopped"
    for days in (2, 4):
        report = run(policy, {}, fleet, store, clock=lambda days=days: NOW + timedelta(days=days))
        assert report["reasons"] == {"no_janitor_stop_history": 1}
    assert fleet.actions == [(iid, "stop")]


def test_final_refresh_opt_out_invalidates_stop_authority(setup):
    policy, store, fleet, iid = stopped_fleet(setup)
    original_tags = copy.deepcopy(fleet.instances[iid]["Tags"])
    fleet.refreshes = 0

    def opt_out(fleet, resource_id):
        if fleet.refreshes == 2:
            fleet.instances[resource_id]["Tags"].append({"Key": "DoNotCleanup", "Value": "true"})

    fleet.refresh_hook = opt_out
    report = run(policy, {}, fleet, store, clock=lambda: NOW + timedelta(days=2))
    assert report["reasons"] == {"changed_before_action": 1}
    assert "stop" not in store.get(f"resource#{iid}")

    fleet.refresh_hook = None
    fleet.instances[iid]["Tags"] = original_tags
    report = run(policy, {}, fleet, store, clock=lambda: NOW + timedelta(days=3))
    assert report["reasons"] == {"no_janitor_stop_history": 1}
    assert fleet.actions == [(iid, "stop")]


def test_final_refresh_changed_stop_evidence_restarts_grace(setup):
    policy, store, fleet, iid = stopped_fleet(setup)
    fleet.refreshes = 0

    def change_stop_evidence(fleet, resource_id):
        if fleet.refreshes == 2:
            fleet.instances[resource_id]["StateTransitionReason"] = "User initiated stop"

    fleet.refresh_hook = change_stop_evidence
    observed_at = NOW + timedelta(days=2)
    report = run(policy, {}, fleet, store, clock=lambda: observed_at)
    assert report["reasons"] == {"changed_before_action": 1}
    assert store.get(f"resource#{iid}")["stop"]["observed_stopped_at"] == observed_at.isoformat()
    assert fleet.actions == [(iid, "stop")]

    fleet.refresh_hook = None
    report = run(policy, {}, fleet, store, clock=lambda: observed_at + timedelta(hours=23))
    assert report["reasons"] == {"recovery_grace": 1}
    report = run(policy, {}, fleet, store, clock=lambda: observed_at + timedelta(hours=24))
    assert report["reasons"] == {"api_accepted": 1}
    assert fleet.actions == [(iid, "stop"), (iid, "terminate")]
