import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from telco_iq.api import create_app
from telco_iq.contracts import ExecutionResult, Status
from telco_iq.events import ExecutionEvent
from telco_iq.executor import Executor

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"X-Telco-Local": "submitted-plan"}


def plan(client):
    health = client.get("/api/health").json()
    return {
        "request_id": "synthetic-stream-test", "question": "Transport test, not a live source result.",
        "principal_scope": health["scope"], "as_of": "2026-10-01T00:00:00Z",
        "ontology_revision": health["context_revision"], "metric_selections": [],
        "nodes": [{"id": "answer", "kind": "SynthesizeAnswer", "source": "not-executed"}],
        "result_node": "answer",
    }


def frames(text):
    return [
        (lines[0][len("event: "):], json.loads(lines[1][len("data: "):]))
        for chunk in text.strip().split("\n\n") if (lines := chunk.splitlines())
    ]


def test_stream_preserves_actual_callback_events_and_failure_result(monkeypatch, tmp_path):
    async def execute(self, submitted, scope, *, on_event=None):
        await on_event(ExecutionEvent(
            run_id=submitted.request_id, node_id="answer", operation="SynthesizeAnswer",
            phase="failed", lane="SYNTHETIC_UNIT_TRANSPORT", status=Status.FAILED,
            message="Synthetic transport fixture, not source execution.",
        ))
        return ExecutionResult(
            request_id=submitted.request_id, status=Status.FAILED, mode="SYNTHETIC_UNIT_TRANSPORT",
            errors=["Synthetic failure"], elapsed_ms=1,
        )

    saved = []
    monkeypatch.setattr(Executor, "execute", execute)
    monkeypatch.setattr("telco_iq.api.write_receipt", lambda path, value: saved.append(value))
    client = TestClient(create_app(ROOT), base_url="http://localhost:8765")
    response = client.post("/api/execute/stream", json=plan(client), headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    messages = frames(response.text)
    assert [name for name, _ in messages] == ["trace", "result"]
    assert messages[0][1]["phase"] == "failed"
    assert messages[1][1]["status"] == "FAILED" and messages[1][1]["answer"] is None
    assert saved[0]["result"]["status"] == "FAILED"


def test_stream_unexpected_error_is_logged_and_never_a_complete_answer(monkeypatch, caplog):
    async def execute(self, submitted, scope, *, on_event=None):
        raise RuntimeError("synthetic internal diagnostic")

    monkeypatch.setattr(Executor, "execute", execute)
    client = TestClient(create_app(ROOT), base_url="http://localhost:8765")
    with caplog.at_level(logging.ERROR, logger="telco_iq.api"):
        response = client.post("/api/execute/stream", json=plan(client), headers=HEADERS)
    messages = frames(response.text)
    assert [name for name, _ in messages] == ["error"]
    assert messages[0][1]["status"] == "FAILED"
    assert "synthetic internal diagnostic" not in response.text
    assert "Submitted-plan execution stream failed" in caplog.text


def test_stream_requires_explicit_local_boundary_and_correct_origin():
    client = TestClient(create_app(ROOT, local_port=8766), base_url="http://localhost:8766")
    assert client.post("/api/execute/stream", json=plan(client)).status_code == 403
    response = client.post("/api/execute/stream", json=plan(client),
                           headers=HEADERS | {"Origin": "http://localhost:8765"})
    assert response.status_code == 403
    with pytest.raises(ValueError, match="Local port"):
        create_app(ROOT, local_port=0)
