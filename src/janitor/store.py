"""DynamoDB journal. All writes after acquisition are fenced by the scope lock.

Locks deliberately have no lease or TTL: EC2 cannot fence a paused old worker.
Never put lifecycle history, pending intents, or the budget on a TTL.
"""

import json

from botocore.exceptions import ClientError


class StateError(RuntimeError):
    pass


class RunLocked(StateError):
    pass


class Store:
    def __init__(self, client, table, scope):
        self.client, self.table, self.scope = client, table, scope
        self.owner = None

    def key(self, sk):
        return {"pk": {"S": self.scope}, "sk": {"S": sk}}

    def get(self, sk):
        try:
            item = self.client.get_item(
                TableName=self.table,
                Key=self.key(sk),
                ConsistentRead=True,
            ).get("Item")
            return json.loads(item["body"]["S"]) if item else None
        except Exception as exc:
            raise StateError(f"Cannot read durable state: {type(exc).__name__}") from exc

    def acquire(self, run_id, now):
        try:
            self.client.put_item(
                TableName=self.table,
                Item={**self.key("lock"), "owner": {"S": run_id}, "created_at": {"S": now}},
                ConditionExpression="attribute_not_exists(pk)",
            )
            self.owner = run_id
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise RunLocked("Scope is locked; inspect the owning run before recovery") from exc
            raise StateError("Lock acquisition failed; do not assume no lock was written") from exc
        except Exception as exc:
            raise StateError("Lock acquisition outcome unknown") from exc

    def lock_condition(self):
        if not self.owner:
            raise StateError("No acquired lock")
        return {
            "TableName": self.table,
            "Key": self.key("lock"),
            "ConditionExpression": "#owner = :owner",
            "ExpressionAttributeNames": {"#owner": "owner"},
            "ExpressionAttributeValues": {":owner": {"S": self.owner}},
        }

    def assert_owned(self):
        try:
            self.client.transact_write_items(
                TransactItems=[{"ConditionCheck": self.lock_condition()}],
            )
        except Exception as exc:
            raise StateError("Lock ownership could not be verified") from exc

    def write(self, records, *, retention=None, finish=False):
        """Atomically persist records (sk -> JSON body), optionally releasing the lock."""
        operation = "Delete" if finish else "ConditionCheck"
        items = [{operation: self.lock_condition()}]
        for sk, body in records.items():
            encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if len(encoded.encode()) > 350_000:
                raise StateError("State item exceeds safe DynamoDB size")
            item = {**self.key(sk), "body": {"S": encoded}}
            if retention and (sk.startswith("run#") or sk.startswith("plan#")):
                item["expires_at"] = {"N": str(retention)}
            items.append({"Put": {"TableName": self.table, "Item": item}})
        try:
            self.client.transact_write_items(TransactItems=items)
        except Exception as exc:
            # A transaction might have committed even when its response was lost.
            raise StateError("Journal write outcome uncertain; reconciliation required") from exc
        if finish:
            self.owner = None

    def plans(self, run_id):
        request = {
            "TableName": self.table,
            "ConsistentRead": True,
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
            "ExpressionAttributeValues": {
                ":pk": {"S": self.scope},
                ":prefix": {"S": f"plan#{run_id}#"},
            },
        }
        while True:
            response = self.client.query(**request)
            yield from (json.loads(item["body"]["S"]) for item in response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            request["ExclusiveStartKey"] = response["LastEvaluatedKey"]
