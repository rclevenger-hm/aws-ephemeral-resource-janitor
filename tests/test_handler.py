import json
from unittest.mock import Mock

import pytest

from janitor import handler


def test_failed_run_raises_for_lambda_async_destination(monkeypatch, capsys):
    monkeypatch.setattr(handler, "load_policy", lambda: object())
    monkeypatch.setattr(handler, "AWS", Mock())
    monkeypatch.setattr(
        handler,
        "run",
        lambda *a, **kw: {
            "run_id": "run-123",
            "status": "partial",
            "counts": {"submitted": 1, "failed": 1},
            "scope": "scope#123456789012#us-east-1",
        },
    )
    with pytest.raises(handler.RunFailed, match="run-123"):
        handler.lambda_handler({}, Mock())
    event = json.loads(capsys.readouterr().out)
    assert event["RunFailure"] == 1 and event["ActionsSubmitted"] == 1


def test_success_is_not_wrapped_in_an_http_status(monkeypatch):
    monkeypatch.setattr(handler, "load_policy", lambda: object())
    monkeypatch.setattr(handler, "AWS", Mock())
    monkeypatch.setattr(handler, "run", lambda *a, **kw: {"run_id": "one", "status": "complete"})
    assert handler.lambda_handler({}, Mock()) == {"run_id": "one", "status": "complete"}


def test_duplicate_does_not_double_count_actions(capsys):
    handler.emit({"status": "complete", "duplicate": True, "counts": {"submitted": 5}})
    assert json.loads(capsys.readouterr().out)["ActionsSubmitted"] == 0


def test_invalid_policy_fails_closed_before_aws_construction(monkeypatch):
    monkeypatch.setenv("JANITOR_POLICY", '{"dry_run":false}')
    aws = Mock()
    monkeypatch.setattr(handler, "AWS", aws)
    with pytest.raises(ValueError):
        handler.lambda_handler({}, Mock())
    aws.assert_not_called()
