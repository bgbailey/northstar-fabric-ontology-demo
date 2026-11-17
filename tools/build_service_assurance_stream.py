"""Deterministically build the four-model service_assurance stream in the recorded demo library."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa

from telco_iq.demo_library import (
    RecordedDemoLibrary,
    RecordedServiceAssuranceDemo,
    ServiceAssuranceAccountRow,
    ServiceAssuranceExclusion,
    ServiceAssuranceHop,
    ServiceAssuranceModelEvidence,
)
from telco_iq.recorded_ontology import (
    RecordedEntity,
    RecordedMetric,
    RecordedOntology,
    RecordedRelationship,
    RecordedRule,
)

try:
    from .build_agent_surface_captures import build_agent_surfaces, write_surface_files
    from .build_data import SEED, TABLE_BY_ID
    from .build_data import build as build_data
    from .build_models import metrics as build_catalog_metrics
except ImportError:
    from build_agent_surface_captures import build_agent_surfaces, write_surface_files
    from build_data import SEED, TABLE_BY_ID
    from build_data import build as build_data
    from build_models import metrics as build_catalog_metrics

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "fabric" / "ontology" / "service-assurance.json"
DEFAULT_LIBRARY = ROOT / "recordings" / "northstar-demo-library-v2-2026-10-28.json"
DEFAULT_WORK_DIR = ROOT / ".local" / "service-assurance-stream-build"
GENERATED_AT = datetime(2026, 11, 16, 0, 0, 0, tzinfo=timezone.utc)
PROFILE = "service-assurance"
PERIOD = "2026-07"

MODEL_LABELS: dict[str, str] = {
    "network": "Network",
    "enterprise": "Enterprise Services",
    "finance": "Finance",
    "care": "Care",
}

MODEL_GRAINS: dict[str, str] = {
    "network": "Incident and site-hour",
    "enterprise": "Service-site interval, service-month, and contract-month",
    "finance": "Account and service month",
    "care": "Case and daily case snapshot",
}


def _query_rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    table = con.execute(sql).to_arrow_table()
    schema = pa.schema([
        field.with_type(pa.timestamp(field.type.unit, tz="+00:00"))
        if pa.types.is_timestamp(field.type) and field.type.tz
        else field
        for field in table.schema
    ])
    return table.cast(schema).to_pylist()


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_ontology(contract: dict, contract_bytes: bytes) -> RecordedOntology:
    snapshot_hash = hashlib.sha256(contract_bytes).hexdigest()
    catalog_by_key = {
        (item["model"], item["table"], item["name"]): item
        for item in build_catalog_metrics()
    }

    entities: list[RecordedEntity] = []
    entity_table_by_name: dict[str, str] = {}
    for entry in contract["entities"]:
        entity_name = entry["name"]
        table_id = entry["table"]
        entity_table_by_name[entity_name] = table_id
        base_desc = TABLE_BY_ID[table_id]["description"]
        window_note = ""
        if "interval" in entry:
            span = entry["interval"]
            window_note = f" Event interval columns: {span['from']} to {span['to']} (UTC)."
        elif "validity" in entry:
            span = entry["validity"]
            window_note = f" Half-open UTC validity columns: {span['from']} to {span['to']}."
        entities.append(
            RecordedEntity(
                id=entity_name,
                model=entry["model"],
                table=table_id,
                name=entry["modelTable"],
                description=(
                    f"{entry['logical']} in {MODEL_LABELS[entry['model']]} ({entry['modelTable']}). "
                    f"{base_desc}{window_note}"
                ),
                keys=list(entry["key"]),
            )
        )

    relationships: list[RecordedRelationship] = []
    for rel in contract["relationships"]:
        rel_id = (
            f"hop{rel['hop']}_{rel['from']}_to_{rel['to']}"
            if "hop" in rel
            else f"{rel['from']}_to_{rel['to']}"
        )
        parts: list[str] = []
        if "hop" in rel:
            parts.append(f"Hop {rel['hop']}")
        parts.append(f"Join on {', '.join(rel['on'])}")
        if rel.get("crossesModels"):
            parts.append("crosses semantic models")
        if "validity" in rel:
            parts.append(f"validity: {rel['validity']}")
        if "period" in rel:
            parts.append(f"period: {rel['period']}")
        description = f"{' · '.join(parts)}. {rel['meaning']}"
        relationships.append(
            RecordedRelationship(
                id=rel_id,
                source=rel["from"],
                target=rel["to"],
                description=description,
            )
        )

    metrics: list[RecordedMetric] = []
    for item in contract["metrics"]:
        entity_id = item["entity"]
        table_id = entity_table_by_name[entity_id]
        catalog_entry = catalog_by_key[(item["model"], table_id, item["name"])]
        metrics.append(
            RecordedMetric(
                id=catalog_entry["id"],
                model=item["model"],
                table=table_id,
                entity=entity_id,
                name=item["name"],
                description=catalog_entry["description"],
                representation="EXTERNALMEASURE",
            )
        )

    rules = [
        RecordedRule(
            id=rule["id"],
            statement=rule["statement"],
            referenced_entities=list(rule["linkedTo"]),
            property_scope="None",
        )
        for rule in contract["rules"]
    ]

    return RecordedOntology(
        captured_at=GENERATED_AT,
        native_snapshot_hash=snapshot_hash,
        provenance="ACTUAL_NATIVE_DEFINITION_AND_METADATA_CAPTURE",
        sources=list(contract["sourceModels"]),
        entities=entities,
        relationships=relationships,
        metrics=metrics,
        rules=rules,
        execution_relationships={},
        limitations=[
            "Projected offline from fabric/ontology/service-assurance.json for deterministic playback; live verification in a demo tenant is tracked in docs/architecture/platform-status.md.",
            "Definition graph over four Direct Lake semantic-model templates, not a materialized instance graph.",
            "All 14 ontology metrics link to source semantic-model DAX measures; calculations remain in their home models.",
            "Rules R1 through R5 are plain-language guidance read by agents, not executable data constraints or access policies.",
        ],
    )


def build_hops() -> list[ServiceAssuranceHop]:
    return [
        ServiceAssuranceHop(
            hop=1,
            from_entity="Incident",
            to_entity="Site",
            join_keys=["site_key"],
            validity="Incident interval start to end overlaps July 2026",
            crosses_models=False,
            models_label="Network",
            summary="Locate physical network sites hit by outages overlapping July 2026.",
        ),
        ServiceAssuranceHop(
            hop=2,
            from_entity="Site",
            to_entity="Service-site assignment",
            join_keys=["site_key"],
            validity="Assignment valid_from to valid_to overlaps the incident",
            crosses_models=True,
            models_label="Network -> Enterprise Services",
            summary="Bridge from Network sites to enterprise service-site assignments active during the outage.",
        ),
        ServiceAssuranceHop(
            hop=3,
            from_entity="Service-site assignment",
            to_entity="Contract",
            join_keys=["contract_key"],
            validity="Contract valid_from to valid_to overlaps the incident",
            crosses_models=False,
            models_label="Enterprise Services",
            summary="Retain only contracts whose half-open UTC validity also overlaps the incident window.",
        ),
        ServiceAssuranceHop(
            hop=4,
            from_entity="Contract",
            to_entity="SLA evaluation",
            join_keys=["contract_key", "month"],
            validity="Contract-month (2026-07-01)",
            crosses_models=False,
            models_label="Enterprise Services",
            summary="Read whole-month capped Credit Exposure once per contract-month without multiplying across incidents.",
        ),
        ServiceAssuranceHop(
            hop=5,
            from_entity="Contract",
            to_entity="Account",
            join_keys=["account_key"],
            validity="Registered customer account key",
            crosses_models=False,
            models_label="Enterprise Services",
            summary="Deduplicate exposed contracts to canonical account_key identities rather than display names.",
        ),
        ServiceAssuranceHop(
            hop=6,
            from_entity="Account",
            to_entity="Account month",
            join_keys=["account_key", "month"],
            validity="Account-month (2026-07-01)",
            crosses_models=True,
            models_label="Enterprise Services -> Finance",
            summary="Bridge to Finance at one row per account-month for Recognized Service Revenue and Contribution Margin.",
        ),
        ServiceAssuranceHop(
            hop=7,
            from_entity="Account",
            to_entity="Case",
            join_keys=["account_key", "month"],
            validity="Cases opened in July 2026 (2026-07-01)",
            crosses_models=True,
            models_label="Enterprise Services -> Care",
            summary="Bridge to Care on account_key to count July Escalated Cases and Repeat Contacts.",
        ),
    ]


def build_service_assurance_demo(
    contract_path: Path = DEFAULT_CONTRACT,
    work_dir: Path = DEFAULT_WORK_DIR,
) -> RecordedServiceAssuranceDemo:
    contract_bytes = contract_path.read_bytes()
    contract = json.loads(contract_bytes.decode("utf-8"))

    if work_dir.exists():
        shutil.rmtree(work_dir)
    try:
        build_data(PROFILE, SEED, work_dir)
        with duckdb.connect() as con:
            con.execute("SET TimeZone='UTC'")
            parquet_tables = {entry["table"] for entry in contract["entities"]}
            for table_id in sorted(parquet_tables):
                parquet_posix = (work_dir / f"{table_id}.parquet").as_posix()
                con.execute(f"CREATE VIEW v_{table_id} AS SELECT * FROM read_parquet('{parquet_posix}')")

            ranked_raw = _query_rows(
                con,
                """
                WITH july_inc AS (
                    SELECT *
                    FROM v_incident
                    WHERE start < TIMESTAMPTZ '2026-08-01T00:00:00Z'
                      AND "end" > TIMESTAMPTZ '2026-07-01T00:00:00Z'
                ),
                exposed AS (
                    SELECT DISTINCT
                        c.account_key,
                        m.site_key,
                        c.contract_key
                    FROM july_inc i
                    JOIN v_site st ON st.site_key = i.site_key
                    JOIN v_service_site_map m ON m.site_key = st.site_key
                    JOIN v_contract c ON c.contract_key = m.contract_key
                    WHERE GREATEST(i.start, m.valid_from, c.valid_from, TIMESTAMPTZ '2026-07-01T00:00:00Z')
                        < LEAST(i."end", m.valid_to, c.valid_to, TIMESTAMPTZ '2026-08-01T00:00:00Z')
                ),
                incomplete_accounts AS (
                    SELECT DISTINCT account_key
                    FROM v_sla_evaluation
                    WHERE month = DATE '2026-07-01' AND coverage_complete IS NOT TRUE
                ),
                exposed_contracts AS (
                    SELECT DISTINCT account_key, contract_key
                    FROM exposed
                    WHERE account_key NOT IN (SELECT account_key FROM incomplete_accounts)
                ),
                account_sla AS (
                    SELECT
                        ec.account_key,
                        LIST(ec.contract_key ORDER BY ec.contract_key) AS affected_contracts,
                        SUM(se.eligible_credit_cents - se.applied_credit_cents)::BIGINT AS credit_exposure_cents
                    FROM exposed_contracts ec
                    JOIN v_sla_evaluation se
                      ON se.contract_key = ec.contract_key
                     AND se.month = DATE '2026-07-01'
                    GROUP BY ec.account_key
                ),
                account_sites AS (
                    SELECT
                        account_key,
                        LIST(DISTINCT site_key ORDER BY site_key) AS affected_sites
                    FROM exposed
                    WHERE account_key NOT IN (SELECT account_key FROM incomplete_accounts)
                    GROUP BY account_key
                ),
                account_care AS (
                    SELECT
                        account_key,
                        COALESCE(SUM(escalated), 0)::BIGINT AS escalated_cases,
                        COALESCE(SUM(repeat_contact), 0)::BIGINT AS repeat_contacts
                    FROM v_case
                    WHERE month = DATE '2026-07-01'
                    GROUP BY account_key
                )
                SELECT
                    a.account_key,
                    a.account_id,
                    a.display_name,
                    a.region,
                    s.affected_sites,
                    sla.affected_contracts,
                    sla.credit_exposure_cents,
                    am.revenue_cents AS recognized_service_revenue_cents,
                    (am.revenue_cents - am.credit_cents - am.service_cost_cents)::BIGINT AS contribution_margin_cents,
                    COALESCE(cr.escalated_cases, 0)::BIGINT AS escalated_cases,
                    COALESCE(cr.repeat_contacts, 0)::BIGINT AS repeat_contacts
                FROM account_sla sla
                JOIN v_account a ON a.account_key = sla.account_key
                JOIN account_sites s ON s.account_key = sla.account_key
                JOIN v_account_month am
                  ON am.account_key = sla.account_key
                 AND am.month = DATE '2026-07-01'
                LEFT JOIN account_care cr ON cr.account_key = sla.account_key
                WHERE a.enterprise IS TRUE
                ORDER BY
                    sla.credit_exposure_cents DESC,
                    contribution_margin_cents DESC,
                    a.account_key ASC
                """,
            )
            ranked_accounts = [
                ServiceAssuranceAccountRow(
                    account_key=int(row["account_key"]),
                    account_id=str(row["account_id"]),
                    display_name=str(row["display_name"]),
                    region=str(row["region"]),
                    affected_sites=[int(site) for site in row["affected_sites"]],
                    affected_contracts=[int(ck) for ck in row["affected_contracts"]],
                    credit_exposure_cents=int(row["credit_exposure_cents"]),
                    recognized_service_revenue_cents=int(row["recognized_service_revenue_cents"]),
                    contribution_margin_cents=int(row["contribution_margin_cents"]),
                    escalated_cases=int(row["escalated_cases"]),
                    repeat_contacts=int(row["repeat_contacts"]),
                )
                for row in ranked_raw
            ]

            incomplete_raw = _query_rows(
                con,
                """
                SELECT
                    se.account_key,
                    a.account_id,
                    se.contract_key,
                    sm.service_key,
                    sm.monitored_minutes,
                    sm.expected_minutes
                FROM v_sla_evaluation se
                JOIN v_account a ON a.account_key = se.account_key
                JOIN v_service_month sm
                  ON sm.contract_key = se.contract_key
                 AND sm.month = se.month
                 AND sm.coverage_complete IS NOT TRUE
                WHERE se.month = DATE '2026-07-01'
                  AND se.coverage_complete IS NOT TRUE
                ORDER BY se.account_key, se.contract_key, sm.service_key
                """,
            )
            incomplete_services = {int(row["service_key"]) for row in incomplete_raw}
            exclusions: list[ServiceAssuranceExclusion] = [
                ServiceAssuranceExclusion(
                    category="unknown_coverage",
                    subject=(
                        f"account_key={row['account_key']} ({row['account_id']}) · "
                        f"contract_key={row['contract_key']} · service_key={row['service_key']}"
                    ),
                    rule_id="R5",
                    reason=(
                        f"Service {row['service_key']} has incomplete July 2026 serving-site coverage "
                        f"({int(row['monitored_minutes']):,} of {int(row['expected_minutes']):,} expected minutes mapped; "
                        f"coverage_complete = false), so contract {row['contract_key']} and account {row['account_key']} "
                        "remain unknown rather than zero exposure."
                    ),
                )
                for row in incomplete_raw
            ]

            validity_raw = _query_rows(
                con,
                """
                WITH july_inc AS (
                    SELECT *
                    FROM v_incident
                    WHERE start < TIMESTAMPTZ '2026-08-01T00:00:00Z'
                      AND "end" > TIMESTAMPTZ '2026-07-01T00:00:00Z'
                )
                SELECT
                    c.account_key,
                    a.account_id,
                    c.contract_key,
                    m.service_key,
                    m.site_key,
                    LIST(DISTINCT i.incident_key ORDER BY i.incident_key) AS incident_keys,
                    MIN(i.start) AS inc_start,
                    MAX(i."end") AS inc_end,
                    m.valid_from AS map_from,
                    m.valid_to AS map_to,
                    c.valid_from AS con_from,
                    c.valid_to AS con_to,
                    BOOL_OR(i.start < m.valid_to AND m.valid_from < i."end") AS map_overlaps,
                    BOOL_OR(i.start < c.valid_to AND c.valid_from < i."end") AS con_overlaps
                FROM july_inc i
                JOIN v_service_site_map m ON m.site_key = i.site_key
                JOIN v_contract c ON c.contract_key = m.contract_key
                JOIN v_account a ON a.account_key = c.account_key
                WHERE NOT (
                    GREATEST(i.start, m.valid_from, c.valid_from, TIMESTAMPTZ '2026-07-01T00:00:00Z')
                    < LEAST(i."end", m.valid_to, c.valid_to, TIMESTAMPTZ '2026-08-01T00:00:00Z')
                )
                GROUP BY
                    c.account_key, a.account_id, c.contract_key, m.service_key, m.site_key,
                    m.valid_from, m.valid_to, c.valid_from, c.valid_to
                ORDER BY m.service_key, c.contract_key
                """,
            )
            for row in validity_raw:
                service_key = int(row["service_key"])
                if service_key in incomplete_services:
                    continue
                inc_label = ", ".join(str(k) for k in row["incident_keys"])
                if not row["map_overlaps"]:
                    reason = (
                        f"Assignment of service {service_key} to site {row['site_key']} ended {_utc_iso(row['map_to'])}, "
                        f"before July incident(s) {inc_label} started at {_utc_iso(row['inc_start'])}."
                    )
                else:
                    reason = (
                        f"Contract {row['contract_key']} for service {service_key} started {_utc_iso(row['con_from'])}, "
                        f"after July incident(s) {inc_label} on site {row['site_key']} ended at {_utc_iso(row['inc_end'])}."
                    )
                exclusions.append(
                    ServiceAssuranceExclusion(
                        category="non_overlapping_validity",
                        subject=(
                            f"account_key={row['account_key']} ({row['account_id']}) · "
                            f"contract_key={row['contract_key']} · service_key={service_key} · site_key={row['site_key']}"
                        ),
                        rule_id="R1",
                        reason=reason,
                    )
                )

            counts = {
                table_id: int(_query_rows(con, f"SELECT COUNT(*)::BIGINT AS n FROM v_{table_id}")[0]["n"])
                for table_id in sorted(parquet_tables)
            }
            july_stats = _query_rows(
                con,
                """
                SELECT
                    (SELECT COUNT(*)::BIGINT FROM v_incident
                     WHERE start < TIMESTAMPTZ '2026-08-01T00:00:00Z'
                       AND "end" > TIMESTAMPTZ '2026-07-01T00:00:00Z') AS july_incidents,
                    (SELECT COUNT(*)::BIGINT FROM v_incident
                     WHERE month = DATE '2026-07-01' AND severity = 'major') AS july_major_incidents,
                    (SELECT COUNT(DISTINCT site_key)::BIGINT FROM v_incident
                     WHERE start < TIMESTAMPTZ '2026-08-01T00:00:00Z'
                       AND "end" > TIMESTAMPTZ '2026-07-01T00:00:00Z') AS affected_sites,
                    (SELECT COUNT(*)::BIGINT FROM v_case
                     WHERE month = DATE '2026-07-01') AS july_cases
                """,
            )[0]
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    tables_by_model: dict[str, list[str]] = {m: [] for m in contract["sourceModels"]}
    tables_ids_by_model: dict[str, list[str]] = {m: [] for m in contract["sourceModels"]}
    for entity in contract["entities"]:
        model_id = entity["model"]
        if entity["modelTable"] not in tables_by_model[model_id]:
            tables_by_model[model_id].append(entity["modelTable"])
        if entity["table"] not in tables_ids_by_model[model_id]:
            tables_ids_by_model[model_id].append(entity["table"])

    measures_by_model: dict[str, list[str]] = {m: [] for m in contract["sourceModels"]}
    for metric in contract["metrics"]:
        measures_by_model[metric["model"]].append(metric["name"])

    exposed_contract_count = len({ck for row in ranked_accounts for ck in row.affected_contracts})
    total_escalated = sum(row.escalated_cases for row in ranked_accounts)
    total_repeats = sum(row.repeat_contacts for row in ranked_accounts)

    summaries: dict[str, str] = {
        "network": (
            f"{int(july_stats['july_incidents'])} outages overlap July 2026 across "
            f"{int(july_stats['affected_sites'])} physical sites "
            f"({int(july_stats['july_major_incidents'])} major incidents in July 2026)."
        ),
        "enterprise": (
            f"{exposed_contract_count} exposed contracts settled once per July 2026 contract-month across "
            f"{len(ranked_accounts)} complete enterprise accounts; {len(incomplete_raw)} incomplete contracts excluded."
        ),
        "finance": (
            f"{len(ranked_accounts)} affected enterprise accounts deduplicated to one row per July 2026 "
            "account-month so multi-contract accounts never multiply revenue or margin."
        ),
        "care": (
            f"{int(july_stats['july_cases'])} July 2026 care cases across the population, including "
            f"{total_escalated} escalated cases and {total_repeats} repeat contacts on ranked affected accounts."
        ),
    }

    model_evidence = [
        ServiceAssuranceModelEvidence(
            model_id=model_id,
            model_name=MODEL_LABELS[model_id],
            home_tables=tables_by_model[model_id],
            measures=measures_by_model[model_id],
            grain=MODEL_GRAINS[model_id],
            summary=summaries[model_id],
            row_count=sum(counts[table_id] for table_id in tables_ids_by_model[model_id]),
        )
        for model_id in contract["sourceModels"]
    ]

    ontology = build_ontology(contract, contract_bytes)
    return RecordedServiceAssuranceDemo(
        generated_at=GENERATED_AT,
        profile=PROFILE,
        period=PERIOD,
        question=str(contract["businessQuestion"]),
        ontology=ontology,
        hops=build_hops(),
        model_evidence=model_evidence,
        ranked_accounts=ranked_accounts,
        exclusions=exclusions,
        agent_surfaces=build_agent_surfaces(),
        limitations=[
            "Offline synthetic playback generated deterministically from fabric/ontology/service-assurance.json and the seeded service-assurance dataset (seed 20260929).",
            "Does not execute live DAX, call Microsoft Fabric endpoints, or verify row-level security; live demo tenant checks are tracked in docs/architecture/platform-status.md.",
            "Partial result (accounts 6 and 9 with incomplete July 2026 serving-site mapping coverage and non-overlapping validity records are excluded rather than treated as zero).",
            "Contract-month SLA credit exposure and account-month finance totals remain at their home semantic-model grains and are review guidance only, not automated customer credits.",
        ],
    )


def update_library(
    library_path: Path = DEFAULT_LIBRARY,
    contract_path: Path = DEFAULT_CONTRACT,
    work_dir: Path = DEFAULT_WORK_DIR,
    scorecard_path: Path | None = None,
) -> RecordedDemoLibrary:
    original_raw = library_path.read_text(encoding="utf-8")
    original = json.loads(original_raw)
    sa_demo = build_service_assurance_demo(contract_path=contract_path, work_dir=work_dir)
    scorecard_obj = (
        json.loads(scorecard_path.read_text(encoding="utf-8"))
        if scorecard_path is not None
        else original.get("scorecard")
    )
    updated: dict = {
        "schema_version": original["schema_version"],
        "mode": original["mode"],
        "exported_at": original["exported_at"],
        "executive": original["executive"],
        "customer360": original["customer360"],
        "service_assurance": sa_demo.model_dump(mode="json"),
        "limitations": original["limitations"],
    }
    if scorecard_obj is not None:
        updated["scorecard"] = scorecard_obj
    validated = RecordedDemoLibrary.model_validate(updated)
    if updated["executive"] != original["executive"] or updated["customer360"] != original["customer360"]:
        raise RuntimeError("Existing executive or customer360 stream changed unexpectedly")
    serialized = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    library_path.write_text(serialized, encoding="utf-8", newline="\n")
    readback = RecordedDemoLibrary.read(library_path)
    if readback != validated:
        raise RuntimeError("Updated demo library failed exact typed readback")
    return readback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY, help="Path to recorded demo library JSON")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT, help="Path to service-assurance.json")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR, help="Workspace-local temporary build folder")
    parser.add_argument("--scorecard", type=Path, default=None, help="Optional scorecard.json to embed in the library")
    parser.add_argument(
        "--write-surfaces",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Also write the four standalone surface-*.json captures into the library's directory",
    )
    args = parser.parse_args(argv)
    library = update_library(
        library_path=args.library,
        contract_path=args.contract,
        work_dir=args.work_dir,
        scorecard_path=args.scorecard,
    )
    surface_files = write_surface_files(args.library.parent) if args.write_surfaces else []
    assert library.service_assurance is not None
    print(
        json.dumps(
            {
                "status": "SERVICE_ASSURANCE_STREAM_UPDATED",
                "profile": library.service_assurance.profile,
                "period": library.service_assurance.period,
                "sources": library.service_assurance.ontology.sources,
                "entities": len(library.service_assurance.ontology.entities),
                "relationships": len(library.service_assurance.ontology.relationships),
                "metrics": len(library.service_assurance.ontology.metrics),
                "rules": len(library.service_assurance.ontology.rules),
                "hops": len(library.service_assurance.hops),
                "ranked_accounts": len(library.service_assurance.ranked_accounts),
                "exclusions": len(library.service_assurance.exclusions),
                "agent_surfaces": len(library.service_assurance.agent_surfaces),
                "surface_files": [path.name for path in surface_files],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
