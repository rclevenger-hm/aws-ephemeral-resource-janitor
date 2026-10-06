"""AWS API boundary. Mutations are single-resource calls with no automatic retry."""

import boto3
from botocore.config import Config

from .services import Services
from .store import Store

SDK_CONFIG = Config(
    connect_timeout=2,
    read_timeout=5,
    retries={"total_max_attempts": 1, "mode": "standard"},
)


class Deadline(RuntimeError):
    pass


class ScopeError(RuntimeError):
    pass


class AWS:
    def __init__(self, policy, session=None):
        session = session or boto3.Session(region_name=policy.region)
        self.policy = policy
        self.services = Services(session, policy, SDK_CONFIG)
        self.ec2 = session.client("ec2", region_name=policy.region, config=SDK_CONFIG)
        self.asg = session.client("autoscaling", region_name=policy.region, config=SDK_CONFIG)
        self.sts = session.client("sts", region_name=policy.region, config=SDK_CONFIG)
        self.ddb = session.client("dynamodb", region_name=policy.region, config=SDK_CONFIG)
        self.sns = session.client("sns", region_name=policy.region, config=SDK_CONFIG)

    def verify_account(self):
        if self.sts.get_caller_identity()["Account"] != self.policy.account_id:
            raise ScopeError("Caller account does not match the configured account")

    def store(self):
        return Store(self.ddb, self.policy.table_name, self.policy.scope)

    def discover(self, check_time):
        if "ec2" in self.policy.resource_types:
            yield from self.discover_ec2(check_time)
        yield from self.services.discover(check_time)

    def discover_ec2(self, check_time):
        request = {
            "Filters": [{"Name": "tag:JanitorManaged", "Values": ["true"]}],
            "MaxResults": 100,
        }
        while True:
            check_time()
            response = self.ec2.describe_instances(**request)
            for reservation in response.get("Reservations", []):
                if reservation.get("OwnerId") != self.policy.account_id:
                    raise ScopeError("Discovery returned a different resource owner")
                yield from reservation.get("Instances", [])
            if not response.get("NextToken"):
                return
            request["NextToken"] = response["NextToken"]

    def refresh(self, instance_id):
        if instance_id.startswith("arn:"):
            return self.services.refresh(instance_id)
        response = self.ec2.describe_instances(InstanceIds=[instance_id])
        instances = []
        for reservation in response.get("Reservations", []):
            if reservation.get("OwnerId") != self.policy.account_id:
                raise ScopeError("Resource owner changed")
            instances.extend(reservation.get("Instances", []))
        if len(instances) != 1 or instances[0]["InstanceId"] != instance_id:
            raise ScopeError("Fresh instance lookup did not return the requested instance")
        return instances[0]

    def protection(self, instance_id, action, fresh):
        if instance_id.startswith("arn:"):
            return self.services.protection(instance_id, fresh)
        if self.asg.describe_auto_scaling_instances(InstanceIds=[instance_id]).get(
            "AutoScalingInstances"
        ):
            return "autoscaling_member"
        attribute = "disableApiStop" if action == "stop" else "disableApiTermination"
        response = self.ec2.describe_instance_attribute(InstanceId=instance_id, Attribute=attribute)
        result_key = attribute[0].upper() + attribute[1:]
        if response.get(result_key, {}).get("Value", True):
            return "api_protected"
        return None

    def act(self, instance_id, action, fresh, run_id):
        if instance_id.startswith("arn:"):
            return self.services.act(instance_id, action, fresh, run_id)
        # Do not force shutdown, bypass OS shutdown, or modify protection attributes.
        method = self.ec2.stop_instances if action == "stop" else self.ec2.terminate_instances
        response = method(InstanceIds=[instance_id])
        key = "StoppingInstances" if action == "stop" else "TerminatingInstances"
        change = next(c for c in response[key] if c["InstanceId"] == instance_id)
        expected = ("stopping", "stopped") if action == "stop" else ("shutting-down", "terminated")
        if change["CurrentState"]["Name"] not in expected:
            raise ValueError("Unexpected EC2 response state")
        return {
            "started": change["PreviousState"]["Name"] == "running",
            "request_id": response.get("ResponseMetadata", {}).get("RequestId"),
        }

    def notify(self, summary):
        if self.policy.notification_topic:
            import json

            self.sns.publish(
                TopicArn=self.policy.notification_topic,
                Subject=f"AWS janitor: {summary['status']}",
                Message=json.dumps(summary, sort_keys=True),
            )
