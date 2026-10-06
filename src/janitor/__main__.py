"""Local CLI uses the same operator policy and durable state as Lambda."""

import argparse
import json

from .aws import AWS
from .config import load_policy
from .engine import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Only preview workload actions")
    parser.add_argument("--instance-id", action="append", help="Narrow scope to these IDs")
    parser.add_argument("--resource-id", action="append", help="Narrow scope to these IDs/ARNs")
    parser.add_argument("--run-id", help="Read a saved run and its per-resource decisions")
    args = parser.parse_args()
    policy = load_policy()
    aws = AWS(policy)
    store = aws.store()
    if args.run_id:
        aws.verify_account()
        report = store.get(f"run#{args.run_id}")
        if report is None:
            parser.error("Run does not exist or its retention period has elapsed")
        print(
            json.dumps({"summary": report, "resources": list(store.plans(args.run_id))}, indent=2)
        )
        return 0
    event = {}
    if args.dry_run:
        event["dry_run"] = True
    if args.instance_id is not None:
        event["instance_ids"] = args.instance_id
    if args.resource_id is not None:
        event["resource_ids"] = args.resource_id
    report = run(policy, event, aws, store)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
