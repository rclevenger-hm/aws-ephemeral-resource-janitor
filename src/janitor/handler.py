"""Lambda entry point: failures raise so Lambda alarms and destinations can see them."""

import json
import time

from .aws import AWS
from .config import load_policy
from .engine import run


class RunFailed(RuntimeError):
    pass


def emit(summary):
    failed = int(summary.get("status") not in ("complete",))
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "EphemeralJanitor",
                            "Dimensions": [["Scope"]],
                            "Metrics": [
                                {"Name": "RunFailure", "Unit": "Count"},
                                {"Name": "ActionsSubmitted", "Unit": "Count"},
                            ],
                        }
                    ],
                },
                "Scope": summary.get("scope", "configuration"),
                "RunFailure": failed,
                "ActionsSubmitted": 0
                if summary.get("duplicate")
                else summary.get("counts", {}).get("submitted", 0),
                "run": summary,
            },
            sort_keys=True,
        )
    )


def lambda_handler(event, context):
    try:
        policy = load_policy()
        aws = AWS(policy)
        summary = run(
            policy, event, aws, aws.store(), remaining_ms=context.get_remaining_time_in_millis
        )
    except Exception as exc:
        emit({"status": "failed", "error": type(exc).__name__})
        raise
    emit(summary)
    if summary["status"] != "complete":
        raise RunFailed(f"Run {summary['run_id']} is {summary['status']}; inspect DynamoDB journal")
    return summary
