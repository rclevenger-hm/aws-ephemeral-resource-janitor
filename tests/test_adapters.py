from dataclasses import replace
from unittest.mock import Mock

import boto3
import pytest
from botocore.stub import Stubber
from conftest import NOW, instance

from janitor.aws import AWS, SDK_CONFIG, ScopeError
from janitor.config import Policy
from janitor.services import Services

ACCOUNT = "123456789012"
REGION = "us-east-1"
PREFIX = "arn:aws:"


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    session = boto3.Session(region_name=REGION)
    policy = Policy(ACCOUNT, REGION, "janitor")
    return AWS(policy, session)


def test_account_mismatch_stops_before_discovery(adapter):
    with Stubber(adapter.sts) as stub:
        stub.add_response(
            "get_caller_identity",
            {
                "Account": "999999999999",
                "UserId": "test",
                "Arn": "arn:aws:iam::999999999999:user/test",
            },
        )
        with pytest.raises(ScopeError):
            adapter.verify_account()


def test_ec2_discovery_paginates_and_checks_owner(adapter):
    check = Mock()
    with Stubber(adapter.ec2) as stub:
        filters = [{"Name": "tag:JanitorManaged", "Values": ["true"]}]
        stub.add_response(
            "describe_instances",
            {
                "Reservations": [{"OwnerId": ACCOUNT, "Instances": [instance()]}],
                "NextToken": "next",
            },
            {"Filters": filters, "MaxResults": 100},
        )
        stub.add_response(
            "describe_instances",
            {"Reservations": [{"OwnerId": ACCOUNT, "Instances": [instance(2)]}]},
            {"Filters": filters, "MaxResults": 100, "NextToken": "next"},
        )
        assert len(list(adapter.discover_ec2(check))) == 2
        assert check.call_count == 2


def test_ec2_mutations_use_real_sdk_shape_no_force_or_fake_condition(adapter):
    iid = instance()["InstanceId"]
    with Stubber(adapter.ec2) as stub:
        stub.add_response(
            "stop_instances",
            {
                "StoppingInstances": [
                    {
                        "InstanceId": iid,
                        "CurrentState": {"Name": "stopping", "Code": 64},
                        "PreviousState": {"Name": "running", "Code": 16},
                    }
                ]
            },
            {"InstanceIds": [iid]},
        )
        assert adapter.act(iid, "stop", {}, "run")["started"]
        stub.add_response(
            "terminate_instances",
            {
                "TerminatingInstances": [
                    {
                        "InstanceId": iid,
                        "CurrentState": {"Name": "shutting-down", "Code": 32},
                        "PreviousState": {"Name": "stopped", "Code": 80},
                    }
                ]
            },
            {"InstanceIds": [iid]},
        )
        assert not adapter.act(iid, "terminate", {}, "run")["started"]
    assert adapter.ec2.meta.config.retries["total_max_attempts"] == 1


def test_ec2_asg_members_are_not_touched(adapter):
    iid = instance()["InstanceId"]
    with Stubber(adapter.asg) as stub:
        stub.add_response(
            "describe_auto_scaling_instances",
            {
                "AutoScalingInstances": [
                    {
                        "InstanceId": iid,
                        "AutoScalingGroupName": "group",
                        "AvailabilityZone": "us-east-1a",
                        "LifecycleState": "InService",
                        "HealthStatus": "HEALTHY",
                        "ProtectedFromScaleIn": False,
                    }
                ]
            },
            {"InstanceIds": [iid]},
        )
        assert adapter.protection(iid, "stop", {}) == "autoscaling_member"


def test_ec2_stop_protection_checked(adapter):
    iid = instance()["InstanceId"]
    with Stubber(adapter.asg) as asg, Stubber(adapter.ec2) as ec2:
        asg.add_response(
            "describe_auto_scaling_instances", {"AutoScalingInstances": []}, {"InstanceIds": [iid]}
        )
        ec2.add_response(
            "describe_instance_attribute",
            {"InstanceId": iid, "DisableApiStop": {"Value": True}},
            {"InstanceId": iid, "Attribute": "disableApiStop"},
        )
        assert adapter.protection(iid, "stop", {}) == "api_protected"


def test_ecs_scale_preserves_service_configuration(adapter):
    svc = adapter.services
    arn = f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/dev/web"
    cluster = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/dev"
    svc.refs[arn] = ("ecs", cluster)
    with Stubber(svc.clients["ecs"]) as stub:
        stub.add_response(
            "describe_services",
            {
                "services": [
                    {
                        "serviceArn": arn,
                        "status": "ACTIVE",
                        "desiredCount": 3,
                        "taskDefinition": "task:2",
                        "schedulingStrategy": "REPLICA",
                        "tags": [{"key": "JanitorManaged", "value": "true"}],
                    }
                ]
            },
            {"cluster": cluster, "services": [arn], "include": ["TAGS"]},
        )
        fresh = svc.refresh(arn)
        assert fresh["Snapshot"]["desiredCount"] == 3
        stub.add_response(
            "update_service",
            {"service": {"desiredCount": 0}},
            {"cluster": cluster, "service": arn, "desiredCount": 0},
        )
        svc.act(arn, "scale_service_zero", fresh, "run")


def test_ecs_autoscaling_must_be_suspended(adapter):
    svc = adapter.services
    arn = f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/dev/web"
    svc.refs[arn] = ("ecs", f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/dev")
    with Stubber(svc.clients["application-autoscaling"]) as stub:
        stub.add_response(
            "describe_scalable_targets",
            {
                "ScalableTargets": [
                    {
                        "ServiceNamespace": "ecs",
                        "ResourceId": "service/dev/web",
                        "ScalableDimension": "ecs:service:DesiredCount",
                        "MinCapacity": 1,
                        "MaxCapacity": 5,
                        "RoleARN": f"arn:aws:iam::{ACCOUNT}:role/scaling",
                        "CreationTime": NOW,
                        "SuspendedState": {"DynamicScalingOutSuspended": False},
                    }
                ]
            },
            {"ServiceNamespace": "ecs", "ResourceIds": ["service/dev/web"]},
        )
        assert (
            svc.protection(arn, {"Snapshot": {"schedulingStrategy": "REPLICA"}})
            == "autoscaling_active"
        )


def test_eks_requires_explicit_disruption_acknowledgement(adapter):
    svc = adapter.services
    arn = f"arn:aws:eks:{REGION}:{ACCOUNT}:nodegroup/dev/workers/id"
    svc.refs[arn] = ("eks", "dev", "workers")
    assert svc.protection(arn, {"Tags": []}) == "disruption_not_approved"


def test_eks_scaling_keeps_maximum_and_uses_idempotency_token(adapter):
    svc = adapter.services
    arn = f"arn:aws:eks:{REGION}:{ACCOUNT}:nodegroup/dev/workers/id"
    svc.refs[arn] = ("eks", "dev", "workers")
    with Stubber(svc.clients["eks"]) as stub:
        stub.add_response(
            "describe_nodegroup",
            {
                "nodegroup": {
                    "nodegroupArn": arn,
                    "nodegroupName": "workers",
                    "clusterName": "dev",
                    "status": "ACTIVE",
                    "scalingConfig": {"minSize": 1, "maxSize": 5, "desiredSize": 2},
                    "tags": {"JanitorManaged": "true", "JanitorAllowDisruption": "true"},
                }
            },
            {"clusterName": "dev", "nodegroupName": "workers"},
        )
        fresh = svc.refresh(arn)
        assert fresh["Snapshot"]["scalingConfig"]["maxSize"] == 5
        stub.add_response(
            "update_nodegroup_config",
            {"update": {"id": "update-1", "status": "InProgress"}},
            {
                "clusterName": "dev",
                "nodegroupName": "workers",
                "scalingConfig": {"minSize": 0, "desiredSize": 0},
                "clientRequestToken": "run-id",
            },
        )
        assert svc.act(arn, "scale_nodegroup_zero", fresh, "run-id")["operation_id"] == "update-1"


def test_lambda_quarantine_preserves_prior_concurrency_and_avoids_env_in_audit(adapter):
    svc = adapter.services
    arn = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:demo"
    svc.refs[arn] = ("lambda",)
    with Stubber(svc.clients["lambda"]) as stub:
        stub.add_response(
            "get_function_configuration",
            {
                "FunctionName": "demo",
                "RevisionId": "rev",
                "Environment": {"Variables": {"PRIVATE_SETTING": "do-not-journal"}},
            },
            {"FunctionName": arn},
        )
        stub.add_response(
            "get_function_concurrency", {"ReservedConcurrentExecutions": 5}, {"FunctionName": arn}
        )
        stub.add_response("list_tags", {"Tags": {"JanitorManaged": "true"}}, {"Resource": arn})
        fresh = svc.refresh(arn)
        assert fresh["Snapshot"]["reserved_concurrency"] == 5
        assert "do-not-journal" not in str(fresh)
        stub.add_response(
            "put_function_concurrency",
            {"ReservedConcurrentExecutions": 0},
            {"FunctionName": arn, "ReservedConcurrentExecutions": 0},
        )
        svc.act(arn, "quarantine_function", fresh, "run-id")


def test_lambda_provisioned_concurrency_is_protected(adapter):
    svc = adapter.services
    arn = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:demo"
    svc.refs[arn] = ("lambda",)
    with Stubber(svc.clients["lambda"]) as stub:
        stub.add_response(
            "list_provisioned_concurrency_configs",
            {"ProvisionedConcurrencyConfigs": [{}]},
            {"FunctionName": arn},
        )
        assert (
            svc.protection(arn, {"Snapshot": {"function_name": "demo"}})
            == "provisioned_concurrency"
        )


def test_eventbridge_disables_only_scheduled_unmanaged_rules(adapter):
    svc = adapter.services
    arn = f"arn:aws:events:{REGION}:{ACCOUNT}:rule/demo"
    svc.refs[arn] = ("eventbridge", "demo")
    with Stubber(svc.clients["events"]) as stub:
        stub.add_response(
            "describe_rule",
            {"Name": "demo", "Arn": arn, "State": "ENABLED", "ScheduleExpression": "rate(1 hour)"},
            {"Name": "demo"},
        )
        stub.add_response(
            "list_tags_for_resource",
            {"Tags": [{"Key": "JanitorManaged", "Value": "true"}]},
            {"ResourceARN": arn},
        )
        fresh = svc.refresh(arn)
        assert svc.protection(arn, fresh) is None
        stub.add_response("disable_rule", {}, {"Name": "demo"})
        svc.act(arn, "disable_rule", fresh, "run-id")
        fresh["Snapshot"]["schedule"] = None
        assert svc.protection(arn, fresh) == "not_a_user_schedule"


def test_scheduler_preserves_all_optional_configuration(adapter):
    policy = replace(
        adapter.policy,
        scheduler_group="ephemeral",
        scheduler_role_arns=[f"arn:aws:iam::{ACCOUNT}:role/target"],
    )
    svc = Services(boto3.Session(region_name=REGION), policy, SDK_CONFIG)
    arn = f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule/ephemeral/demo"
    group_arn = f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule-group/ephemeral"
    svc.refs[arn] = ("scheduler", "demo")
    original = {
        "Name": "demo",
        "GroupName": "ephemeral",
        "State": "ENABLED",
        "ScheduleExpression": "rate(1 hour)",
        "ScheduleExpressionTimezone": "America/Chicago",
        "StartDate": NOW,
        "EndDate": NOW,
        "Description": "keep me",
        "ActionAfterCompletion": "NONE",
        "KmsKeyArn": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/test",
        "FlexibleTimeWindow": {"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 5},
        "Target": {
            "Arn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:target",
            "RoleArn": policy.scheduler_role_arns[0],
            "Input": '{"private":"value"}',
            "RetryPolicy": {"MaximumRetryAttempts": 3, "MaximumEventAgeInSeconds": 3600},
        },
    }
    with Stubber(svc.clients["scheduler"]) as stub:
        stub.add_response(
            "get_schedule",
            {**original, "Arn": arn, "CreationDate": NOW, "LastModificationDate": NOW},
            {"Name": "demo", "GroupName": "ephemeral"},
        )
        stub.add_response(
            "list_tags_for_resource",
            {"Tags": [{"Key": "JanitorManaged", "Value": "true"}]},
            {"ResourceArn": group_arn},
        )
        fresh = svc.refresh(arn)
        assert "private" not in str(fresh["Snapshot"])
        assert svc.protection(arn, fresh) is None
        stub.add_response(
            "update_schedule",
            {"ScheduleArn": arn},
            {**original, "State": "DISABLED", "ClientToken": "run-id"},
        )
        svc.act(arn, "disable_schedule", fresh, "run-id")


def test_scheduler_disallows_unapproved_pass_role(adapter):
    svc = adapter.services
    arn = f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule/ephemeral/demo"
    svc.refs[arn] = ("scheduler", "demo")
    assert (
        svc.protection(arn, {"Snapshot": {"target_role": "unapproved"}})
        == "target_role_not_allowed"
    )


def test_resource_scope_verified_even_on_refresh(adapter):
    with pytest.raises(ValueError, match="scope"):
        adapter.services.refresh("arn:aws:lambda:us-west-2:999999999999:function:other")


def test_all_discovery_operations_have_real_paginators(adapter):
    for service, operations in {
        "ecs": ["list_clusters", "list_services"],
        "eks": ["list_clusters", "list_nodegroups"],
        "lambda": ["list_functions"],
        "events": ["list_rules"],
        "scheduler": ["list_schedules"],
    }.items():
        for operation in operations:
            assert adapter.services.clients[service].can_paginate(operation)
