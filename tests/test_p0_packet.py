import ast
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]


def builder():
    spec = importlib.util.spec_from_file_location("p0_builder", ROOT / "tools" / "build_p0_packet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packet_contains_only_two_model_synthetic_sources_and_valid_code(tmp_path):
    module = builder()
    packet = module.build(ROOT / "data" / "fixtures" / "miniature", tmp_path)
    assert packet["models"] == ["finance", "subscriptions"]
    assert len(packet["tables"]) == 10
    notebook = json.loads((tmp_path / "Load_TelcoIQ_Miniature.ipynb").read_text())
    for cell in notebook["cells"]:
        ast.parse("".join(cell["source"]))
        assert cell["execution_count"] is None and cell["outputs"] == []
    statement = ast.parse("".join(notebook["cells"][0]["source"])).body[1]
    assets = json.loads(ast.literal_eval(statement.value.args[0]))
    for asset in assets:
        raw = base64.b64decode(asset["payload"])
        assert hashlib.sha256(raw).hexdigest() == asset["file_sha256"]
        table = pq.read_table(io.BytesIO(raw))
        assert table.num_rows == asset["rows"]
        assert table.column_names == list(asset["columns"])
    assert not notebook["metadata"].get("dependencies"), "No invented cloud IDs"
    loader = "".join(notebook["cells"][1]["source"])
    assert '`ws-telco-iq-p0-20260929`.`lh_telcoiq_p0`.`dbo`' in loader
    assert 'spark.sql("SHOW TABLES IN " + schema_name)' in loader
    assert 'spark.sql("SHOW SCHEMAS")' not in loader
    for model in packet["modelPackets"]:
        payload = json.loads((tmp_path / (model["model"] + "-template.json")).read_text())
        assert payload["definition"]["format"] == "TMDL"
        paths = [p["path"] for p in payload["definition"]["parts"]]
        assert "definition.pbism" in paths
        assert "definition/expressions.tmdl" in paths
        assert all("\\" not in p for p in paths)
        assert all(p == "definition.pbism" or p.endswith(".tmdl") for p in paths)


def test_packet_is_deterministic_and_requires_miniature(tmp_path):
    module = builder()
    source = ROOT / "data" / "fixtures" / "miniature"
    one = module.build(source, tmp_path / "one")
    two = module.build(source, tmp_path / "two")
    assert one == two
    bad = tmp_path / "not-miniature"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps({"profile": "standard"}))
    with pytest.raises(ValueError, match="miniature only"):
        module.build(bad, tmp_path / "out")
