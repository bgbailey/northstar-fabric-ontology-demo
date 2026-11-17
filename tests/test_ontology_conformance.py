"""Offline unit tests for tools/check_ontology_conformance.py."""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import pytest

from tools import check_ontology_conformance

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "fabric" / "ontology" / "service-assurance.json"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "ontology_conformance"


@pytest.fixture
def isolated_definition(tmp_path: Path) -> Path:
    dest = tmp_path / "ontology_definition"
    shutil.copytree(FIXTURE_DIR, dest)
    return dest


def _run_cli(
    definition_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    output_format: str = "json",
) -> tuple[int, str]:
    code = check_ontology_conformance.main(
        [
            "--contract",
            str(CONTRACT_PATH),
            "--definition-dir",
            str(definition_path),
            "--format",
            output_format,
        ]
    )
    captured = capsys.readouterr()
    return code, captured.out


def test_exact_conformance_passes_in_text_and_json_formats(
    capsys: pytest.CaptureFixture[str],
) -> None:
    text_code, text_out = _run_cli(FIXTURE_DIR, capsys, output_format="text")
    assert text_code == 0
    assert "Ontology conformance: CONFORMANT" in text_out
    assert "Summary: missing=0, extra=0, different=0" in text_out

    json_code, json_out = _run_cli(FIXTURE_DIR, capsys, output_format="json")
    assert json_code == 0
    report = json.loads(json_out)
    assert report["conformant"] is True
    assert report["summary"] == {"missing": 0, "extra": 0, "different": 0}
    for section in ("entities", "relationships", "metrics", "rules"):
        assert report[section] == {"missing": [], "extra": [], "different": []}


def test_inline_base64_get_definition_envelope_is_supported(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parts = []
    for file_path in sorted(FIXTURE_DIR.rglob("*")):
        if file_path.is_file():
            rel = file_path.relative_to(FIXTURE_DIR).as_posix()
            encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
            parts.append({"path": rel, "payload": encoded, "payloadType": "InlineBase64"})

    envelope_dir = tmp_path / "exported_envelope"
    envelope_dir.mkdir()
    envelope_file = envelope_dir / "getDefinition.json"
    envelope_file.write_text(
        json.dumps({"definition": {"parts": parts}}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    code, out = _run_cli(envelope_dir, capsys, output_format="json")
    assert code == 0
    assert json.loads(out)["conformant"] is True


def test_namespaced_entity_file_matches_contract_entity(
    isolated_definition: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = isolated_definition / "entities" / "network_Incidents.tmdl"
    namespaced = isolated_definition / "entities" / "serviceassurance#network_Incidents.tmdl"
    text = original.read_text(encoding="utf-8").replace(
        "entity network_Incidents",
        "entity 'serviceassurance#network_Incidents'",
        1,
    )
    original.unlink()
    namespaced.write_text(text, encoding="utf-8", newline="\n")

    code, out = _run_cli(isolated_definition, capsys)
    assert code == 0
    assert json.loads(out)["conformant"] is True


def test_missing_entity_fails(
    isolated_definition: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (isolated_definition / "entities" / "care_CaseDailySnapshot.tmdl").unlink()
    code, out = _run_cli(isolated_definition, capsys)
    report = json.loads(out)
    assert code == 1
    assert report["conformant"] is False
    assert report["entities"]["missing"] == ["care_CaseDailySnapshot"]


def test_extra_entity_fails(
    isolated_definition: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    extra_entity = isolated_definition / "entities" / "network_ExtraEntity.tmdl"
    extra_entity.write_text(
        "entity network_ExtraEntity\n\tbackingTable: network_site\n\tkeyProperty: site_key\n",
        encoding="utf-8",
        newline="\n",
    )
    code, out = _run_cli(isolated_definition, capsys)
    report = json.loads(out)
    assert code == 1
    assert report["entities"]["extra"] == ["network_ExtraEntity"]


def test_different_entity_key_and_backing_table_fail(
    isolated_definition: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entity_file = isolated_definition / "entities" / "network_Incidents.tmdl"
    original = entity_file.read_text(encoding="utf-8")

    entity_file.write_text(
        original.replace("keyProperty: incident_key", "keyProperty: site_key", 1),
        encoding="utf-8",
        newline="\n",
    )
    code, out = _run_cli(isolated_definition, capsys)
    report = json.loads(out)
    assert code == 1
    assert len(report["entities"]["different"]) == 1
    assert report["entities"]["different"][0]["name"] == "network_Incidents"
    assert "key columns" in report["entities"]["different"][0]["reasons"][0]

    entity_file.write_text(
        original.replace("backingTable: network_incident", "backingTable: network_site", 1),
        encoding="utf-8",
        newline="\n",
    )
    code2, out2 = _run_cli(isolated_definition, capsys)
    report2 = json.loads(out2)
    assert code2 == 1
    assert any(item["name"] == "network_Incidents" for item in report2["entities"]["different"])


def test_missing_relationship_fails(
    isolated_definition: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    er_path = isolated_definition / "entityRelationships.tmdl"
    blocks = er_path.read_text(encoding="utf-8").strip().split("\n\n")
    filtered = [b for b in blocks if "entityRelationship network_Incidents_to_network_Sites" not in b]
    er_path.write_text("\n\n".join(filtered) + "\n", encoding="utf-8", newline="\n")

    code, out = _run_cli(isolated_definition, capsys)
    report = json.loads(out)
    assert code == 1
    assert report["relationships"]["missing"] == ["network_Incidents -> network_Sites"]
    assert report["relationships"]["extra"] == []


def test_extra_relationship_fails(
    isolated_definition: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    er_path = isolated_definition / "entityRelationships.tmdl"
    er_path.write_text(
        er_path.read_text(encoding="utf-8")
        + "\nentityRelationship extra_bridge\n\tfromEntity: finance_Accounts\n\ttoEntity: care_Accounts\n",
        encoding="utf-8",
        newline="\n",
    )
    code, out = _run_cli(isolated_definition, capsys)
    report = json.loads(out)
    assert code == 1
    assert report["relationships"]["missing"] == []
    assert report["relationships"]["extra"] == ["finance_Accounts -> care_Accounts (extra_bridge)"]


def test_different_relationship_target_and_join_column_fail(
    isolated_definition: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    er_path = isolated_definition / "entityRelationships.tmdl"
    original_er = er_path.read_text(encoding="utf-8")
    target_old = (
        "entityRelationship network_Incidents_to_network_Sites\n"
        "\tfromEntity: network_Incidents\n"
        "\ttoEntity: network_Sites"
    )
    target_new = (
        "entityRelationship network_Incidents_to_network_Sites\n"
        "\tfromEntity: network_Incidents\n"
        "\ttoEntity: network_Accounts"
    )
    er_path.write_text(original_er.replace(target_old, target_new, 1), encoding="utf-8", newline="\n")

    code, out = _run_cli(isolated_definition, capsys)
    report = json.loads(out)
    assert code == 1
    assert report["relationships"]["missing"] == []
    assert report["relationships"]["extra"] == []
    assert len(report["relationships"]["different"]) == 1
    assert report["relationships"]["different"][0]["relationship"] == "network_Incidents -> network_Sites"

    # Restore entityRelationships.tmdl and mutate join column in relationships.tmdl
    er_path.write_text(original_er, encoding="utf-8", newline="\n")
    rel_path = isolated_definition / "relationships.tmdl"
    rel_text = rel_path.read_text(encoding="utf-8").replace(
        "relationship rel_network_Incidents_to_network_Sites\n"
        "\tfromColumn: network_incident.site_key\n"
        "\ttoColumn: network_site.site_key",
        "relationship rel_network_Incidents_to_network_Sites\n"
        "\tfromColumn: network_incident.market_id\n"
        "\ttoColumn: network_site.market_id",
        1,
    )
    rel_path.write_text(rel_text, encoding="utf-8", newline="\n")

    code2, out2 = _run_cli(isolated_definition, capsys)
    report2 = json.loads(out2)
    assert code2 == 1
    assert len(report2["relationships"]["different"]) == 1
    assert "join columns" in report2["relationships"]["different"][0]["reasons"][0]


def test_missing_metric_link_fails(
    isolated_definition: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    table_path = isolated_definition / "tables" / "network_incident.tmdl"
    lines = [
        line
        for line in table_path.read_text(encoding="utf-8").splitlines()
        if "Major Incidents" not in line
    ]
    table_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    code, out = _run_cli(isolated_definition, capsys)
    report = json.loads(out)
    assert code == 1
    assert report["metrics"]["missing"] == ["Major Incidents"]


def test_extra_metric_link_fails(
    isolated_definition: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    table_path = isolated_definition / "tables" / "network_incident.tmdl"
    original = table_path.read_text(encoding="utf-8")
    injected = original.replace(
        "table network_incident\n",
        "table network_incident\n"
        '\tmeasure \'Unlisted Metric\' = EXTERNALMEASURE("Unlisted Metric", INTEGER, '
        '"DirectQuery to AS - SYNTHETIC_WORKSPACE/SYNTHETIC_NETWORK")\n',
        1,
    )
    table_path.write_text(injected, encoding="utf-8", newline="\n")

    code, out = _run_cli(isolated_definition, capsys)
    report = json.loads(out)
    assert code == 1
    assert report["metrics"]["extra"] == ["Unlisted Metric"]


def test_metric_with_custom_dax_or_explicit_metric_fails(
    isolated_definition: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    table_path = isolated_definition / "tables" / "network_incident.tmdl"
    original = table_path.read_text(encoding="utf-8")
    custom_dax = "\tmeasure 'Major Incidents' = COUNTROWS(network_incident)"
    replaced = "\n".join(
        custom_dax if "Major Incidents" in line else line for line in original.splitlines()
    )
    table_path.write_text(replaced + "\n", encoding="utf-8", newline="\n")

    code, out = _run_cli(isolated_definition, capsys, output_format="text")
    assert code == 1
    assert "DIFFERENT: Major Incidents" in out
    assert "EXTERNALMEASURE" in out

    # Also verify top-level explicit metric in metrics/*.tmdl is rejected when replacing a source link
    no_measure = "\n".join(
        line for line in original.splitlines() if "Major Incidents" not in line
    )
    table_path.write_text(no_measure + "\n", encoding="utf-8", newline="\n")
    metrics_dir = isolated_definition / "metrics"
    metrics_dir.mkdir()
    (metrics_dir / "Major Incidents.tmdl").write_text(
        "metric 'Major Incidents'\n\texpression\n\t\tdialect: SQL\n\t\texpression: COUNT(*)\n",
        encoding="utf-8",
        newline="\n",
    )
    code2, out2 = _run_cli(isolated_definition, capsys)
    report2 = json.loads(out2)
    assert code2 == 1
    assert report2["metrics"]["different"][0]["name"] == "Major Incidents"


def test_metric_source_model_binding_must_match_contract_alias(
    isolated_definition: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    table_path = isolated_definition / "tables" / "network_incident.tmdl"
    original = table_path.read_text(encoding="utf-8")
    table_path.write_text(
        original.replace("SYNTHETIC_NETWORK", "SYNTHETIC_FINANCE"),
        encoding="utf-8",
    )

    code, out = _run_cli(isolated_definition, capsys)
    report = json.loads(out)
    assert code == 1
    difference = next(
        item for item in report["metrics"]["different"] if item["name"] == "Major Incidents"
    )
    assert any("expected source model 'network'" in reason for reason in difference["reasons"])


def test_metric_source_model_annotation_must_not_allow_an_alias_prefix(
    isolated_definition: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    table_path = isolated_definition / "tables" / "network_incident.tmdl"
    original = table_path.read_text(encoding="utf-8")
    table_path.write_text(
        original.replace("ONT_ItemName = Synthetic network", "ONT_ItemName = Synthetic networkish"),
        encoding="utf-8",
    )

    code, out = _run_cli(isolated_definition, capsys)
    report = json.loads(out)
    assert code == 1
    difference = next(
        item for item in report["metrics"]["different"] if item["name"] == "Major Incidents"
    )
    assert any("table source model annotation" in reason for reason in difference["reasons"])


def test_exact_expected_model_evidence_rejects_identity_suffixes(
    isolated_definition: Path,
) -> None:
    original_network_tables = {
        path: path.read_text(encoding="utf-8")
        for path in (isolated_definition / "tables").glob("network_*.tmdl")
    }
    expected = {
        "network": {
            "id": "00000000-0000-0000-0000-000000000010",
            "displayName": "Northstar Network Service Assurance",
        },
    }

    def write_network_identity(annotation: str, external_source: str) -> None:
        for path, text in original_network_tables.items():
            path.write_text(
                text.replace("Synthetic network", annotation).replace(
                    "SYNTHETIC_NETWORK",
                    external_source,
                ),
                encoding="utf-8",
            )

    write_network_identity(
        "Northstar Network Service Assurance Copy",
        "00000000-0000-0000-0000-000000000010",
    )
    name_copy = check_ontology_conformance.check_conformance(
        CONTRACT_PATH,
        isolated_definition,
        expected,
    )
    assert name_copy["conformant"] is False
    difference = next(
        item
        for item in name_copy["metrics"]["different"]
        if item["name"] == "Major Incidents"
    )
    assert any("table source model annotation" in reason for reason in difference["reasons"])

    write_network_identity(
        "Northstar Network Service Assurance",
        "00000000-0000-0000-0000-000000000010-copy",
    )
    id_copy = check_ontology_conformance.check_conformance(
        CONTRACT_PATH,
        isolated_definition,
        expected,
    )
    assert id_copy["conformant"] is False
    difference = next(
        item for item in id_copy["metrics"]["different"] if item["name"] == "Major Incidents"
    )
    assert any("EXTERNALMEASURE source" in reason for reason in difference["reasons"])

    write_network_identity(
        "Northstar Network Service Assurance",
        "Northstar Network Service Assurance",
    )
    exact_name = check_ontology_conformance.check_conformance(
        CONTRACT_PATH,
        isolated_definition,
        expected,
    )
    assert exact_name["conformant"] is True

    write_network_identity(
        "Northstar Network Service Assurance",
        "00000000-0000-0000-0000-000000000010",
    )
    exact_id = check_ontology_conformance.check_conformance(
        CONTRACT_PATH,
        isolated_definition,
        expected,
    )
    assert exact_id["conformant"] is True


def test_missing_extra_and_modified_rules_fail(
    isolated_definition: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rule_path = isolated_definition / "rules" / "ExposureNeedsOverlap.tmdl"
    original = rule_path.read_text(encoding="utf-8")

    # 1. Missing rule
    rule_path.unlink()
    code_missing, out_missing = _run_cli(isolated_definition, capsys)
    assert code_missing == 1
    assert json.loads(out_missing)["rules"]["missing"] == ["ExposureNeedsOverlap"]

    # 2. Modified statement
    rule_path.write_text(
        original.replace("half-open UTC intervals.", "closed local-time intervals.", 1),
        encoding="utf-8",
        newline="\n",
    )
    code_stmt, out_stmt = _run_cli(isolated_definition, capsys)
    report_stmt = json.loads(out_stmt)
    assert code_stmt == 1
    assert report_stmt["rules"]["different"][0]["name"] == "ExposureNeedsOverlap"
    assert "rule statement differs" in report_stmt["rules"]["different"][0]["reasons"][0]

    # 3. Modified referenced entity
    rule_path.write_text(
        original.replace("\truleReferencedEntity enterprise_Contracts\n", "", 1),
        encoding="utf-8",
        newline="\n",
    )
    code_ref, out_ref = _run_cli(isolated_definition, capsys)
    report_ref = json.loads(out_ref)
    assert code_ref == 1
    assert report_ref["rules"]["different"][0]["name"] == "ExposureNeedsOverlap"
    assert "referenced entities" in report_ref["rules"]["different"][0]["reasons"][0]

    # 4. Extra rule
    rule_path.write_text(original, encoding="utf-8", newline="\n")
    (isolated_definition / "rules" / "ExtraPolicyRule.tmdl").write_text(
        "rule ExtraPolicyRule\n\tstatement: Extra synthetic rule.\n\truleReferencedEntity network_Sites\n",
        encoding="utf-8",
        newline="\n",
    )
    code_extra, out_extra = _run_cli(isolated_definition, capsys)
    assert code_extra == 1
    assert json.loads(out_extra)["rules"]["extra"] == ["ExtraPolicyRule"]
