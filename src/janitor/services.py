"""Service adapters for reversible capacity and scheduling changes."""

import hashlib
import json
import os

from .policy import tags


def normalized(kind, arn, resource_tags, snapshot, active, raw=None):
    if isinstance(resource_tags, dict):
        resource_tags = [{"Key": k, "Value": v} for k, v in resource_tags.items()]
    else:
        resource_tags = [
            {"Key": t.get("Key", t.get("key")), "Value": t.get("Value", t.get("value"))}
            for t in resource_tags
        ]
    return {
        "Kind": kind,
        "InstanceId": arn,
        "Tags": resource_tags,
        "Snapshot": snapshot,
        "State": {"Name": active},
        "Raw": raw or {},
    }


class Services:
    def __init__(self, session, policy, sdk_config):
        self.policy = policy
        self.clients = {
            kind: session.client(kind, region_name=policy.region, config=sdk_config)
            for kind in (
                "ecs",
                "eks",
                "lambda",
                "events",
                "scheduler",
                "application-autoscaling",
                "autoscaling",
            )
        }
        self.refs = {}

    def pages(self, service, operation, key, check_time, **kwargs):
        for page in self.clients[service].get_paginator(operation).paginate(**kwargs):
            check_time()
            yield from page.get(key, [])

    def discover(self, check_time):
        def enrolled(resource):
            return tags(resource).get("JanitorManaged") == "true"

        if "ecs" in self.policy.resource_types:
            for cluster in self.pages("ecs", "list_clusters", "clusterArns", check_time):
                for arn in self.pages(
                    "ecs", "list_services", "serviceArns", check_time, cluster=cluster
                ):
                    check_time()
                    self.refs[arn] = ("ecs", cluster)
                    resource = self.refresh(arn)
                    if enrolled(resource):
                        yield resource
        if "eks" in self.policy.resource_types:
            for cluster in self.pages("eks", "list_clusters", "clusters", check_time):
                for group in self.pages(
                    "eks", "list_nodegroups", "nodegroups", check_time, clusterName=cluster
                ):
                    check_time()
                    data = self.clients["eks"].describe_nodegroup(
                        clusterName=cluster, nodegroupName=group
                    )["nodegroup"]
                    arn = data["nodegroupArn"]
                    self.refs[arn] = ("eks", cluster, group)
                    resource = self.nodegroup(data)
                    if enrolled(resource):
                        yield resource
        if "lambda" in self.policy.resource_types:
            for function in self.pages("lambda", "list_functions", "Functions", check_time):
                check_time()
                arn = function["FunctionArn"]
                self.refs[arn] = ("lambda",)
                resource = self.refresh(arn)
                if enrolled(resource):
                    yield resource
        if "eventbridge" in self.policy.resource_types:
            for rule in self.pages("events", "list_rules", "Rules", check_time):
                check_time()
                arn = rule["Arn"]
                self.refs[arn] = ("eventbridge", rule["Name"])
                resource = self.refresh(arn)
                if enrolled(resource):
                    yield resource
        if "scheduler" in self.policy.resource_types and self.policy.scheduler_group:
            for schedule in self.pages(
                "scheduler",
                "list_schedules",
                "Schedules",
                check_time,
                GroupName=self.policy.scheduler_group,
            ):
                check_time()
                arn = schedule["Arn"]
                self.refs[arn] = ("scheduler", schedule["Name"])
                resource = self.refresh(arn)
                if enrolled(resource):
                    yield resource

    def nodegroup(self, data):
        scaling = data["scalingConfig"]
        state = (
            "updating"
            if data["status"] != "ACTIVE"
            else ("running" if scaling["desiredSize"] or scaling["minSize"] else "stopped")
        )
        return normalized(
            "eks",
            data["nodegroupArn"],
            data.get("tags", {}),
            {
                "scalingConfig": scaling,
                "status": data["status"],
                "autoScalingGroups": data.get("resources", {}).get("autoScalingGroups", []),
            },
            state,
            data,
        )

    def refresh(self, arn):
        parts = arn.split(":", 5)
        if len(parts) != 6 or parts[3:5] != [self.policy.region, self.policy.account_id]:
            raise ValueError("Service resource is outside the configured scope")
        ref = self.refs[arn]
        kind = ref[0]
        if kind == "ecs":
            response = self.clients["ecs"].describe_services(
                cluster=ref[1], services=[arn], include=["TAGS"]
            )
            if response.get("failures") or len(response.get("services", [])) != 1:
                raise ValueError("ECS service cannot be refreshed")
            data = response["services"][0]
            state = "running" if data["desiredCount"] else "stopped"
            if data["status"] != "ACTIVE" or len(data.get("deployments", [])) > 1:
                state = "updating"
            return normalized(
                kind,
                arn,
                data.get("tags", []),
                {
                    "desiredCount": data["desiredCount"],
                    "taskDefinition": data["taskDefinition"],
                    "schedulingStrategy": data.get("schedulingStrategy", "REPLICA"),
                    "status": data["status"],
                },
                state,
                data,
            )
        if kind == "eks":
            return self.nodegroup(
                self.clients["eks"].describe_nodegroup(clusterName=ref[1], nodegroupName=ref[2])[
                    "nodegroup"
                ]
            )
        if kind == "lambda":
            client = self.clients["lambda"]
            data = client.get_function_configuration(FunctionName=arn)
            concurrency = client.get_function_concurrency(FunctionName=arn)
            reserved = concurrency.get("ReservedConcurrentExecutions")
            state = "stopped" if reserved == 0 else "running"
            if (
                data.get("State", "Active") != "Active"
                or data.get("LastUpdateStatus", "Successful") != "Successful"
            ):
                state = "updating"
            return normalized(
                kind,
                arn,
                client.list_tags(Resource=arn).get("Tags", {}),
                {
                    "reserved_concurrency": reserved,
                    "revision": data["RevisionId"],
                    "function_name": data["FunctionName"],
                },
                state,
            )
        if kind == "eventbridge":
            client = self.clients["events"]
            data = client.describe_rule(Name=ref[1])
            return normalized(
                kind,
                arn,
                client.list_tags_for_resource(ResourceARN=arn)["Tags"],
                {
                    "state": data["State"],
                    "schedule": data.get("ScheduleExpression"),
                    "managed_by": data.get("ManagedBy"),
                },
                "stopped" if data["State"] == "DISABLED" else "running",
                data,
            )
        client = self.clients["scheduler"]
        group = self.policy.scheduler_group
        data = client.get_schedule(Name=ref[1], GroupName=group)
        group_arn = arn.split(":schedule/")[0] + ":schedule-group/" + group
        group_tags = client.list_tags_for_resource(ResourceArn=group_arn).get("Tags", [])
        inputs = client.meta.service_model.operation_model("UpdateSchedule").input_shape.members
        original = {k: v for k, v in data.items() if k in inputs}
        digest = hashlib.sha256(
            json.dumps(original, sort_keys=True, default=str).encode()
        ).hexdigest()
        return normalized(
            kind,
            arn,
            group_tags,
            {
                "state": data["State"],
                "configuration_hash": digest,
                "target_role": data["Target"]["RoleArn"],
                "group": group,
            },
            "stopped" if data["State"] == "DISABLED" else "running",
            original,
        )

    def protection(self, arn, fresh):
        ref = self.refs[arn]
        kind = ref[0]
        if kind == "ecs":
            if fresh["Snapshot"]["schedulingStrategy"] != "REPLICA":
                return "daemon_service"
            resource_id = "service/" + ref[1].split("/")[-1] + "/" + arn.split("/")[-1]
            targets = self.clients["application-autoscaling"].describe_scalable_targets(
                ServiceNamespace="ecs", ResourceIds=[resource_id]
            )["ScalableTargets"]
            if any(
                not all(
                    t.get("SuspendedState", {}).get(k, False)
                    for k in (
                        "DynamicScalingInSuspended",
                        "DynamicScalingOutSuspended",
                        "ScheduledScalingSuspended",
                    )
                )
                for t in targets
            ):
                return "autoscaling_active"
        elif kind == "eks":
            # EKS desired-size changes do NOT honor Kubernetes PDBs.
            if tags(fresh).get("JanitorAllowDisruption") != "true":
                return "disruption_not_approved"
            names = [g["name"] for g in fresh["Snapshot"]["autoScalingGroups"]]
            if names:
                groups = self.clients["autoscaling"].describe_auto_scaling_groups(
                    AutoScalingGroupNames=names
                )["AutoScalingGroups"]
                if len(groups) != len(names):
                    return "autoscaling_state_unknown"
                if any(
                    t["Key"].startswith("k8s.io/cluster-autoscaler/")
                    and t.get("Value", "").lower() in ("true", "owned", "shared")
                    for g in groups
                    for t in g.get("Tags", [])
                ):
                    return "autoscaling_active"
        elif kind == "lambda":
            if fresh["Snapshot"]["function_name"] == os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
                return "janitor_infrastructure"
            if (
                self.clients["lambda"]
                .list_provisioned_concurrency_configs(FunctionName=arn)
                .get("ProvisionedConcurrencyConfigs")
            ):
                return "provisioned_concurrency"
        elif kind == "eventbridge":
            if fresh["Snapshot"]["managed_by"] or not fresh["Snapshot"]["schedule"]:
                return "not_a_user_schedule"
        elif kind == "scheduler":
            if fresh["Snapshot"]["target_role"] not in self.policy.scheduler_role_arns:
                return "target_role_not_allowed"
        return None

    def act(self, arn, action, fresh, run_id):
        ref = self.refs[arn]
        if action == "scale_service_zero":
            response = self.clients["ecs"].update_service(
                cluster=ref[1], service=arn, desiredCount=0
            )
            if response["service"]["desiredCount"] != 0:
                raise ValueError("ECS did not acknowledge desiredCount=0")
        elif action == "scale_nodegroup_zero":
            response = self.clients["eks"].update_nodegroup_config(
                clusterName=ref[1],
                nodegroupName=ref[2],
                scalingConfig={"minSize": 0, "desiredSize": 0},
                clientRequestToken=run_id,
            )
            if response["update"]["status"] not in ("InProgress", "Successful"):
                raise ValueError("EKS did not accept the update")
        elif action == "quarantine_function":
            response = self.clients["lambda"].put_function_concurrency(
                FunctionName=arn, ReservedConcurrentExecutions=0
            )
            if response["ReservedConcurrentExecutions"] != 0:
                raise ValueError("Lambda did not acknowledge quarantine")
        elif action == "disable_rule":
            response = self.clients["events"].disable_rule(Name=ref[1])
        elif action == "disable_schedule":
            response = self.clients["scheduler"].update_schedule(
                **{**fresh["Raw"], "State": "DISABLED", "ClientToken": run_id}
            )
            if response["ScheduleArn"] != arn:
                raise ValueError("Scheduler returned an unexpected ARN")
        else:
            raise ValueError("Unsupported action")
        return {
            "request_id": response.get("ResponseMetadata", {}).get("RequestId"),
            "operation_id": response.get("update", {}).get("id"),
        }
