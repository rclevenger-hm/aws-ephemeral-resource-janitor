import copy
from dataclasses import replace
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from janitor.config import Policy
from janitor.store import Store

NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)


def instance(number=1, state="running", extra_tags=None):
    tags = {"JanitorManaged": "true", "ExpiresAt": "2026-01-01T00:00:00Z"}
    tags.update(extra_tags or {})
    return {
        "InstanceId": f"i-{number:017x}",
        "State": {"Name": state},
        "LaunchTime": NOW,
        "RootDeviceType": "ebs",
        "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
    }


class Fleet:
    def __init__(self, instances):
        self.instances = {i["InstanceId"]: copy.deepcopy(i) for i in instances}
        self.actions = []
        self.tokens = []
        self.errors = {}
        self.refresh_hook = None
        self.act_hook = None
        self.protections = {}
        self.discovery_error = None
        self.refreshes = 0
        self.notifications = []

    def verify_account(self):
        pass

    def discover(self, check_time):
        yield from copy.deepcopy(list(self.instances.values()))
        if self.discovery_error:
            raise self.discovery_error

    def refresh(self, iid):
        self.refreshes += 1
        if self.refresh_hook:
            self.refresh_hook(self, iid)
        return copy.deepcopy(self.instances[iid])

    def protection(self, iid, action, fresh):
        return self.protections.get(iid)

    def act(self, iid, action, fresh, run_id):
        if self.act_hook:
            self.act_hook(iid, action)
        self.actions.append((iid, action))
        self.tokens.append(run_id)
        if iid in self.errors:
            raise self.errors[iid]
        self.instances[iid]["State"]["Name"] = "stopped"
        return {"started": True, "request_id": "aws-request"}

    def notify(self, report):
        self.notifications.append(copy.deepcopy(report))


@pytest.fixture
def setup():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="janitor",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in ("pk", "sk")],
        )
        policy = Policy("123456789012", "us-east-1", "janitor", dry_run=False)

        def make(**changes):
            p = replace(policy, **changes)
            return p, Store(client, p.table_name, p.scope)

        yield make
