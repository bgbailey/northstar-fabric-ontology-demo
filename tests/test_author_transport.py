import importlib.util
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location("p0_author_test", ROOT / "infra" / "fabric_p0.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_read_transport_retries_but_never_duplicates_mutation(tmp_path, monkeypatch):
    loaded = module()
    monkeypatch.setattr(loaded.time, "sleep", lambda _: None)
    calls = []

    def handler(request):
        calls.append(request.method)
        if len(calls) < 3:
            raise httpx.ConnectError("synthetic connection reset")
        return httpx.Response(200, json={"value": []})

    client = object.__new__(loaded.Author)
    client.private = tmp_path
    client.client = httpx.Client(base_url="https://api.fabric.microsoft.com", transport=httpx.MockTransport(handler))
    try:
        assert client.request("GET", "/v1/workspaces").status_code == 200
        assert calls == ["GET", "GET", "GET"]
        calls.clear()
        with pytest.raises(httpx.ConnectError):
            client.request("POST", "/v1/workspaces", json={"displayName": "synthetic"})
        assert calls == ["POST"]
    finally:
        client.client.close()
