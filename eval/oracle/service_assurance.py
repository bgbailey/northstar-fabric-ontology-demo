"""Independent DuckDB oracle and trap evaluator for the service-assurance golden suite.

Reads generated Parquet tables directly with DuckDB and never imports runtime modules
from ``src/telco_iq`` or generator modules from ``tools``.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

VALID_LEVELS: tuple[str, ...] = ("L1", "L2", "L3", "L4", "N", "P")
VALID_MODELS: tuple[str, ...] = ("network", "enterprise", "finance", "care")
VALID_PERSONAS: tuple[str, ...] = ("default", "east_region")
VALID_RULES: tuple[str, ...] = ("R1", "R2", "R3", "R4", "R5")
VALID_TRAPS: tuple[str, ...] = (
    "site-key-only",
    "credit-per-incident",
    "revenue-fan-out",
    "display-name-or-billing-key",
    "missing-as-zero",
)
PARQUET_TABLES: tuple[str, ...] = (
    "incident",
    "site",
    "site_hour",
    "service_site_map",
    "service",
    "service_month",
    "contract",
    "sla_evaluation",
    "applied_credit",
    "account",
    "account_month",
    "billing_account_map",
    "case",
    "case_daily_snapshot",
    "market",
)
EAST_REGION_MARKET_TABLES: frozenset[str] = frozenset({
    "incident",
    "site_hour",
    "service_site_map",
    "service_month",
    "contract",
    "sla_evaluation",
    "applied_credit",
    "account_month",
    "case",
    "case_daily_snapshot",
})
EAST_REGION_DIM_TABLES: frozenset[str] = frozenset({"account", "market", "site"})
EAST_REGION_ACCOUNT_LINKED: frozenset[str] = frozenset({"service", "billing_account_map"})
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_question_record(question: dict[str, Any]) -> None:
    """Validate a golden question record against the Appendix B schema."""
    required = {
        "id", "level", "models", "question", "parameters", "persona", "answer", "rules", "traps",
    }
    missing = required - question.keys()
    if missing:
        raise ValueError(f"Question missing required fields: {sorted(missing)}")
    qid = question["id"]
    if not isinstance(qid, str) or not qid.startswith("SA-"):
        raise ValueError(f"Invalid question id: {qid!r}")
    level = question["level"]
    if level not in VALID_LEVELS:
        raise ValueError(f"{qid}: invalid level {level!r}")
    models = question["models"]
    if not isinstance(models, list) or not models or any(m not in VALID_MODELS for m in models):
        raise ValueError(f"{qid}: invalid models {models!r}")
    if len(set(models)) != len(models):
        raise ValueError(f"{qid}: duplicate models in {models!r}")
    expected_counts = {"L1": {1}, "L2": {2}, "L3": {3}, "L4": {4}, "P": {4}, "N": {2, 3, 4}}
    if len(models) not in expected_counts[level]:
        raise ValueError(f"{qid}: level {level} disallow model count {len(models)}")
    persona = question["persona"]
    if persona not in VALID_PERSONAS:
        raise ValueError(f"{qid}: invalid persona {persona!r}")
    if level == "P" and persona != "east_region":
        raise ValueError(f"{qid}: level P requires persona 'east_region'")
    if not isinstance(question["question"], str) or not question["question"].strip():
        raise ValueError(f"{qid}: question text must be non-empty")
    if not isinstance(question["parameters"], dict) or "month" not in question["parameters"]:
        raise ValueError(f"{qid}: parameters must include 'month'")
    answer = question["answer"]
    if not isinstance(answer, dict):
        raise ValueError(f"{qid}: answer must be an object")
    for key in ("keys", "columns", "orderBy", "exclusionsExpected"):
        if key not in answer:
            raise ValueError(f"{qid}: answer missing {key!r}")
    if not isinstance(answer["keys"], list) or not answer["keys"]:
        raise ValueError(f"{qid}: answer.keys must be non-empty")
    if not isinstance(answer["columns"], list) or not answer["columns"]:
        raise ValueError(f"{qid}: answer.columns must be non-empty")
    for col in answer["columns"]:
        if not isinstance(col, dict) or {"name", "metric", "type"} - col.keys():
            raise ValueError(f"{qid}: invalid column spec {col!r}")
        if not IDENTIFIER_RE.match(col["name"]):
            raise ValueError(f"{qid}: invalid column identifier {col['name']!r}")
    if not isinstance(answer["exclusionsExpected"], bool):
        raise ValueError(f"{qid}: exclusionsExpected must be boolean")
    if any(rule not in VALID_RULES for rule in question["rules"]):
        raise ValueError(f"{qid}: invalid rule in {question['rules']!r}")
    if any(trap not in VALID_TRAPS for trap in question["traps"]):
        raise ValueError(f"{qid}: invalid trap in {question['traps']!r}")


def load_questions(path: Path) -> list[dict[str, Any]]:
    """Load and validate JSONL golden question records."""
    questions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        record = json.loads(stripped)
        if not isinstance(record, dict):
            raise ValueError(f"{path}:{line_no}: expected JSON object")
        validate_question_record(record)
        if record["id"] in seen_ids:
            raise ValueError(f"{path}:{line_no}: duplicate question id {record['id']!r}")
        seen_ids.add(record["id"])
        questions.append(record)
    return questions


def _month_bounds(month_str: str) -> tuple[str, str, str]:
    start_day = date.fromisoformat(f"{month_str}-01")
    end_day = date(start_day.year + (start_day.month == 12), start_day.month % 12 + 1, 1)
    return (
        start_day.isoformat(),
        f"{start_day.isoformat()}T00:00:00Z",
        f"{end_day.isoformat()}T00:00:00Z",
    )


def _scalar(value: object) -> str | int | float | bool | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported DuckDB result scalar type: {type(value).__name__}")


def _fetch_rows(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    arrow_table = cursor.to_arrow_table()
    cast_fields = [
        field.with_type(pa.timestamp(field.type.unit, tz="+00:00"))
        if pa.types.is_timestamp(field.type) and field.type.tz
        else field
        for field in arrow_table.schema
    ]
    normalized = arrow_table.cast(pa.schema(cast_fields))
    return [
        {key: _scalar(val) for key, val in row.items()}
        for row in normalized.to_pylist()
    ]


def _parquet_literal(data_dir: Path, table_name: str) -> str:
    file_path = (data_dir / f"{table_name}.parquet").resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Missing required Parquet file: {file_path.name}")
    escaped = file_path.as_posix().replace("'", "''")
    return f"'{escaped}'"


_TEMP_TABLES: tuple[str, ...] = (
    "incidents_in_period",
    "exposed_links",
    "candidate_contracts",
    "complete_contracts",
    "contract_sla",
    "account_fin",
    "account_care",
    "candidate_accounts",
    "complete_accounts",
    "account_sla",
    "account_breach",
    "account_cases",
    "account_open_eod",
)
_CONN_CACHE_LOCK = threading.Lock()
_CONN_CACHE: dict[
    tuple[str, str, tuple[tuple[int, int], ...]],
    tuple[duckdb.DuckDBPyConnection, threading.Lock],
] = {}


def _dir_fingerprint(data_dir: Path) -> tuple[tuple[int, int], ...]:
    stats: list[tuple[int, int]] = []
    for name in PARQUET_TABLES:
        file_path = (data_dir / f"{name}.parquet").resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"Missing required Parquet file: {file_path.name}")
        st = file_path.stat()
        stats.append((st.st_mtime_ns, st.st_size))
    return tuple(stats)


def _get_cached_connection(
    data_dir: Path, persona: str
) -> tuple[duckdb.DuckDBPyConnection, threading.Lock]:
    if persona not in VALID_PERSONAS:
        raise ValueError(f"Unsupported persona: {persona!r}")
    fingerprint = _dir_fingerprint(data_dir)
    cache_key = (str(data_dir.resolve()), persona, fingerprint)
    with _CONN_CACHE_LOCK:
        cached = _CONN_CACHE.get(cache_key)
        if cached is not None:
            return cached
        con = duckdb.connect()
        con.execute("SET TimeZone = 'UTC'")
        account_lit = _parquet_literal(data_dir, "account")
        for name in PARQUET_TABLES:
            lit = _parquet_literal(data_dir, name)
            if persona == "default":
                con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM read_parquet({lit})')
            elif name in EAST_REGION_DIM_TABLES:
                con.execute(
                    f'CREATE TABLE "{name}" AS SELECT * FROM read_parquet({lit}) '
                    f"WHERE region = 'East'"
                )
            elif name in EAST_REGION_MARKET_TABLES:
                con.execute(
                    f'CREATE TABLE "{name}" AS SELECT * FROM read_parquet({lit}) '
                    f"WHERE market_id = 'M01'"
                )
            elif name in EAST_REGION_ACCOUNT_LINKED:
                con.execute(
                    f'CREATE TABLE "{name}" AS SELECT * FROM read_parquet({lit}) '
                    f"WHERE account_key IN ("
                    f"SELECT account_key FROM read_parquet({account_lit}) WHERE region = 'East')"
                )
            else:
                con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM read_parquet({lit})')
        entry = (con, threading.Lock())
        _CONN_CACHE[cache_key] = entry
        return entry


def _order_by_sql(order_specs: list[str], keys: list[str], valid_cols: set[str]) -> str:
    clauses: list[str] = []
    seen: set[str] = set()
    for item in order_specs:
        parts = item.strip().split()
        if len(parts) not in (1, 2):
            raise ValueError(f"Invalid orderBy entry: {item!r}")
        col = parts[0]
        direction = parts[1].upper() if len(parts) == 2 else "ASC"
        if col not in valid_cols or direction not in ("ASC", "DESC"):
            raise ValueError(f"Invalid orderBy column or direction: {item!r}")
        clauses.append(f'"{col}" {direction}')
        seen.add(col)
    for key_col in keys:
        if key_col not in seen:
            clauses.append(f'"{key_col}" ASC')
    return ", ".join(clauses)


def _evaluate_l1(
    con: duckdb.DuckDBPyConnection,
    question: dict[str, Any],
    *,
    trap_name: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    model = question["models"][0]
    params = question["parameters"]
    month_date, _, _ = _month_bounds(params["month"])
    keys = question["answer"]["keys"]
    cols = [c["name"] for c in question["answer"]["columns"]]
    order_sql = _order_by_sql(question["answer"]["orderBy"], keys, set(keys) | set(cols))

    if model == "network" and keys == ["market_id"]:
        severity = params.get("severity", "major")
        sql = f"""
            SELECT market_id, CAST(COUNT(*) AS BIGINT) AS major_incidents
            FROM "incident"
            WHERE month = ?::DATE AND severity = ?
            GROUP BY market_id
            ORDER BY {order_sql}
        """
        return _fetch_rows(con.execute(sql, [month_date, severity])), []

    if model == "network" and keys == ["site_key"]:
        having = "HAVING SUM(unavailable_minutes) > 0" if params.get("unavailable_only") else ""
        sql = f"""
            SELECT site_key, CAST(SUM(unavailable_minutes) AS BIGINT) AS unavailable_site_minutes
            FROM "site_hour"
            WHERE month = ?::DATE
            GROUP BY site_key
            {having}
            ORDER BY {order_sql}
        """
        return _fetch_rows(con.execute(sql, [month_date])), []

    if model == "enterprise" and keys == ["contract_key"]:
        having = "HAVING SUM(amount_cents) > 0" if params.get("positive_applied_only") else ""
        sql = f"""
            SELECT contract_key, CAST(SUM(amount_cents) AS BIGINT) AS applied_credits_cents
            FROM "applied_credit"
            WHERE month = ?::DATE
            GROUP BY contract_key
            {having}
            ORDER BY {order_sql}
        """
        return _fetch_rows(con.execute(sql, [month_date])), []

    if model == "finance" and keys == ["market_id"]:
        sql = f"""
            SELECT
                market_id,
                CAST(SUM(revenue_cents) AS BIGINT) AS recognized_service_revenue_cents,
                CAST(SUM(revenue_cents - credit_cents - service_cost_cents) AS BIGINT)
                    AS contribution_margin_cents
            FROM "account_month"
            WHERE month = ?::DATE
            GROUP BY market_id
            ORDER BY {order_sql}
        """
        return _fetch_rows(con.execute(sql, [month_date])), []

    if model == "care" and keys == ["market_id"] and "escalated_cases" in cols:
        sql = f"""
            SELECT
                market_id,
                CAST(SUM(escalated) AS BIGINT) AS escalated_cases,
                CAST(SUM(repeat_contact) AS BIGINT) AS repeat_contacts
            FROM "case"
            WHERE month = ?::DATE
            GROUP BY market_id
            ORDER BY {order_sql}
        """
        return _fetch_rows(con.execute(sql, [month_date])), []

    if model == "care" and keys == ["market_id"] and "open_cases_eod" in cols:
        if trap_name == "missing-as-zero":
            sql = f"""
                SELECT market_id, CAST(SUM(open_eod) AS BIGINT) AS open_cases_eod
                FROM "case_daily_snapshot"
                WHERE month = ?::DATE
                GROUP BY market_id
                ORDER BY {order_sql}
            """
            return _fetch_rows(con.execute(sql, [month_date])), []
        snapshot_date = params["snapshot_date"]
        sql = f"""
            SELECT market_id, CAST(SUM(open_eod) AS BIGINT) AS open_cases_eod
            FROM "case_daily_snapshot"
            WHERE snapshot_date = ?::DATE
            GROUP BY market_id
            ORDER BY {order_sql}
        """
        return _fetch_rows(con.execute(sql, [snapshot_date])), []

    raise ValueError(f"Unsupported L1 question shape for {question['id']}")


def _install_incident_views(
    con: duckdb.DuckDBPyConnection,
    params: dict[str, Any],
    *,
    site_key_only: bool,
) -> tuple[int, list[dict[str, Any]]]:
    month_str = params["month"]
    month_date, month_start, month_end = _month_bounds(month_str)
    severity = params.get("severity")
    incident_keys = params.get("incident_keys")

    clauses = ["start < ?::TIMESTAMPTZ", "?::TIMESTAMPTZ < \"end\""]
    bind: list[Any] = [month_end, month_start]
    if severity and severity != "all":
        clauses.append("severity = ?")
        bind.append(severity)
    if incident_keys:
        placeholders = ", ".join(str(int(k)) for k in incident_keys)
        clauses.append(f"incident_key IN ({placeholders})")

    where_sql = " AND ".join(clauses)
    con.execute(
        f"CREATE TEMP TABLE incidents_in_period AS "
        f'SELECT incident_key, site_key, start, "end", severity '
        f'FROM "incident" WHERE {where_sql}',
        bind,
    )
    inc_count_row = con.execute("SELECT COUNT(*) FROM incidents_in_period").fetchone()
    inc_count = int(inc_count_row[0]) if inc_count_row else 0
    if inc_count == 0:
        con.execute(
            "CREATE TEMP TABLE exposed_links ("
            "incident_key BIGINT, service_key BIGINT, site_key BIGINT, "
            "contract_key BIGINT, account_key BIGINT)"
        )
        return 0, [
            {
                "entity": "incident",
                "month": month_str,
                "reason": "no_recorded_incidents_in_period",
            }
        ]

    if site_key_only:
        con.execute(
            """
            CREATE TEMP TABLE exposed_links AS
            SELECT DISTINCT
                i.incident_key,
                m.service_key,
                m.site_key,
                c.contract_key,
                c.account_key
            FROM incidents_in_period i
            JOIN "site" s ON i.site_key = s.site_key
            JOIN "service_site_map" m ON s.site_key = m.site_key
            JOIN "contract" c ON m.contract_key = c.contract_key
            JOIN "account" a ON c.account_key = a.account_key
            """
        )
    else:
        con.execute(
            """
            CREATE TEMP TABLE exposed_links AS
            SELECT DISTINCT
                i.incident_key,
                m.service_key,
                m.site_key,
                c.contract_key,
                c.account_key
            FROM incidents_in_period i
            JOIN "site" s ON i.site_key = s.site_key
            JOIN "service_site_map" m ON s.site_key = m.site_key
            JOIN "contract" c ON m.contract_key = c.contract_key
            JOIN "account" a ON c.account_key = a.account_key
            WHERE GREATEST(?::TIMESTAMPTZ, i.start, m.valid_from, c.valid_from)
                < LEAST(?::TIMESTAMPTZ, i."end", m.valid_to, c.valid_to)
            """,
            [month_start, month_end],
        )

    exclusions: list[dict[str, Any]] = []
    unmatched = _fetch_rows(
        con.execute(
            """
            SELECT i.incident_key, i.site_key
            FROM incidents_in_period i
            JOIN "site" s ON i.site_key = s.site_key
            WHERE EXISTS (
                SELECT 1 FROM "service_site_map" m
                JOIN "contract" c ON m.contract_key = c.contract_key
                WHERE m.site_key = i.site_key
            )
            AND NOT EXISTS (
                SELECT 1 FROM exposed_links e WHERE e.incident_key = i.incident_key
            )
            ORDER BY i.incident_key
            """
        )
    )
    for item in unmatched:
        exclusions.append(
            {
                "entity": "incident",
                "incident_key": int(item["incident_key"]),
                "site_key": int(item["site_key"]),
                "month": month_str,
                "reason": "unmapped_incident_window",
            }
        )
    gap_services = _fetch_rows(
        con.execute(
            """
            SELECT DISTINCT m.service_key, m.contract_key, c.account_key, i.site_key
            FROM incidents_in_period i
            JOIN "service_site_map" m ON i.site_key = m.site_key
            JOIN "contract" c ON m.contract_key = c.contract_key
            JOIN "service_month" sm
              ON sm.service_key = m.service_key AND sm.month = ?::DATE
            WHERE sm.breached IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM exposed_links e
                  WHERE e.incident_key = i.incident_key AND e.service_key = m.service_key
              )
            ORDER BY m.service_key
            """,
            [month_date],
        )
    )
    for gap in gap_services:
        exclusions.append(
            {
                "entity": "service",
                "service_key": int(gap["service_key"]),
                "contract_key": int(gap["contract_key"]),
                "account_key": int(gap["account_key"]),
                "site_key": int(gap["site_key"]),
                "month": month_str,
                "reason": "incomplete_mapping_coverage",
            }
        )
    return inc_count, exclusions


def _evaluate_service_grain(
    con: duckdb.DuckDBPyConnection,
    question: dict[str, Any],
    *,
    trap_name: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    params = question["parameters"]
    month_date, _, _ = _month_bounds(params["month"])
    keys = question["answer"]["keys"]
    cols = [c["name"] for c in question["answer"]["columns"]]
    order_sql = _order_by_sql(question["answer"]["orderBy"], keys, set(keys) | set(cols))

    inc_count, exclusions = _install_incident_views(
        con, params, site_key_only=(trap_name == "site-key-only")
    )
    if inc_count == 0:
        return [], ([] if trap_name == "missing-as-zero" else exclusions)

    rows = _fetch_rows(
        con.execute(
            f"""
            SELECT
                service_key,
                CAST(COUNT(DISTINCT incident_key) AS BIGINT) AS major_incidents
            FROM exposed_links
            GROUP BY service_key
            ORDER BY {order_sql}
            """
        )
    )
    if trap_name == "missing-as-zero":
        if not rows:
            zero_rows = _fetch_rows(
                con.execute(
                    f"""
                    SELECT
                        m.service_key,
                        CAST(0 AS BIGINT) AS major_incidents
                    FROM (SELECT DISTINCT m.service_key
                          FROM incidents_in_period i
                          JOIN "service_site_map" m ON i.site_key = m.site_key
                          JOIN "service_month" sm
                            ON sm.service_key = m.service_key AND sm.month = ?::DATE) m
                    ORDER BY {order_sql}
                    """,
                    [month_date],
                )
            )
            return zero_rows, []
        return rows, []
    return rows, exclusions


def _evaluate_contract_grain(
    con: duckdb.DuckDBPyConnection,
    question: dict[str, Any],
    *,
    trap_name: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    params = question["parameters"]
    month_str = params["month"]
    month_date, _, _ = _month_bounds(month_str)
    keys = question["answer"]["keys"]
    cols = [c["name"] for c in question["answer"]["columns"]]
    order_sql = _order_by_sql(question["answer"]["orderBy"], keys, set(keys) | set(cols))

    inc_count, exclusions = _install_incident_views(
        con, params, site_key_only=(trap_name == "site-key-only")
    )
    if inc_count == 0:
        return [], ([] if trap_name == "missing-as-zero" else exclusions)

    if trap_name == "missing-as-zero":
        con.execute(
            """
            CREATE TEMP TABLE candidate_contracts AS
            SELECT DISTINCT contract_key, account_key FROM exposed_links
            UNION
            SELECT DISTINCT c.contract_key, c.account_key
            FROM incidents_in_period i
            JOIN "service_site_map" m ON i.site_key = m.site_key
            JOIN "contract" c ON m.contract_key = c.contract_key
            """
        )
    else:
        con.execute(
            """
            CREATE TEMP TABLE candidate_contracts AS
            SELECT DISTINCT contract_key, account_key FROM exposed_links
            """
        )

    if params.get("require_applied_row"):
        if trap_name == "missing-as-zero":
            rows = _fetch_rows(
                con.execute(
                    f"""
                    SELECT
                        cc.contract_key,
                        CAST(COALESCE(SUM(ac.amount_cents), 0) AS BIGINT) AS applied_credits_cents
                    FROM candidate_contracts cc
                    LEFT JOIN "applied_credit" ac
                      ON ac.contract_key = cc.contract_key AND ac.month = ?::DATE
                    GROUP BY cc.contract_key
                    ORDER BY {order_sql}
                    """,
                    [month_date],
                )
            )
            return rows, []

        incomplete = _fetch_rows(
            con.execute(
                """
                SELECT cc.contract_key, cc.account_key
                FROM candidate_contracts cc
                LEFT JOIN "sla_evaluation" se
                  ON se.contract_key = cc.contract_key AND se.month = ?::DATE
                WHERE se.eligible_credit_cents IS NULL
                ORDER BY cc.contract_key
                """,
                [month_date],
            )
        )
        seen_inc_contracts = {
            e.get("contract_key") for e in exclusions if e.get("reason") == "incomplete_mapping_coverage"
        }
        for item in incomplete:
            if int(item["contract_key"]) not in seen_inc_contracts:
                exclusions.append(
                    {
                        "entity": "contract",
                        "contract_key": int(item["contract_key"]),
                        "account_key": int(item["account_key"]),
                        "month": month_str,
                        "reason": "incomplete_mapping_coverage",
                    }
                )
        missing_applied = _fetch_rows(
            con.execute(
                """
                SELECT cc.contract_key, cc.account_key
                FROM candidate_contracts cc
                JOIN "sla_evaluation" se
                  ON se.contract_key = cc.contract_key AND se.month = ?::DATE
                LEFT JOIN "applied_credit" ac
                  ON ac.contract_key = cc.contract_key AND ac.month = ?::DATE
                WHERE se.eligible_credit_cents IS NOT NULL
                  AND ac.contract_key IS NULL
                ORDER BY cc.contract_key
                """,
                [month_date, month_date],
            )
        )
        for item in missing_applied:
            exclusions.append(
                {
                    "entity": "contract",
                    "contract_key": int(item["contract_key"]),
                    "account_key": int(item["account_key"]),
                    "month": month_str,
                    "reason": "missing_applied_credit_row",
                }
            )
        rows = _fetch_rows(
            con.execute(
                f"""
                SELECT
                    cc.contract_key,
                    CAST(SUM(ac.amount_cents) AS BIGINT) AS applied_credits_cents
                FROM candidate_contracts cc
                JOIN "sla_evaluation" se
                  ON se.contract_key = cc.contract_key AND se.month = ?::DATE
                JOIN "applied_credit" ac
                  ON ac.contract_key = cc.contract_key AND ac.month = ?::DATE
                WHERE se.eligible_credit_cents IS NOT NULL
                GROUP BY cc.contract_key
                ORDER BY {order_sql}
                """,
                [month_date, month_date],
            )
        )
        return rows, exclusions

    if trap_name != "missing-as-zero":
        incomplete_contracts = _fetch_rows(
            con.execute(
                """
                SELECT cc.contract_key, cc.account_key
                FROM candidate_contracts cc
                LEFT JOIN "sla_evaluation" se
                  ON se.contract_key = cc.contract_key AND se.month = ?::DATE
                WHERE se.eligible_credit_cents IS NULL
                ORDER BY cc.contract_key
                """,
                [month_date],
            )
        )
        already = {
            e.get("contract_key") for e in exclusions if e.get("reason") == "incomplete_mapping_coverage"
        }
        for item in incomplete_contracts:
            if int(item["contract_key"]) not in already:
                exclusions.append(
                    {
                        "entity": "contract",
                        "contract_key": int(item["contract_key"]),
                        "account_key": int(item["account_key"]),
                        "month": month_str,
                        "reason": "incomplete_mapping_coverage",
                    }
                )
        con.execute(
            """
            CREATE TEMP TABLE complete_contracts AS
            SELECT cc.contract_key, cc.account_key
            FROM candidate_contracts cc
            JOIN "sla_evaluation" se
              ON se.contract_key = cc.contract_key AND se.month = ?::DATE
            WHERE se.eligible_credit_cents IS NOT NULL
            """,
            [month_date],
        )
    else:
        con.execute(
            "CREATE TEMP TABLE complete_contracts AS SELECT * FROM candidate_contracts"
        )

    if trap_name == "credit-per-incident":
        con.execute(
            """
            CREATE TEMP TABLE contract_sla AS
            SELECT
                cc.contract_key,
                cc.account_key,
                CAST(SUM(COALESCE(se.eligible_credit_cents, 0)) AS BIGINT) AS eligible_credits_cents,
                CAST(SUM(
                    COALESCE(se.eligible_credit_cents, 0) - COALESCE(se.applied_credit_cents, 0)
                ) AS BIGINT) AS credit_exposure_cents
            FROM (SELECT DISTINCT incident_key, contract_key, account_key FROM exposed_links) el
            JOIN complete_contracts cc ON cc.contract_key = el.contract_key
            LEFT JOIN "sla_evaluation" se
              ON se.contract_key = cc.contract_key AND se.month = ?::DATE
            GROUP BY cc.contract_key, cc.account_key
            """,
            [month_date],
        )
    else:
        con.execute(
            """
            CREATE TEMP TABLE contract_sla AS
            SELECT
                cc.contract_key,
                cc.account_key,
                CAST(COALESCE(se.eligible_credit_cents, 0) AS BIGINT) AS eligible_credits_cents,
                CAST(
                    COALESCE(se.eligible_credit_cents, 0) - COALESCE(se.applied_credit_cents, 0)
                    AS BIGINT
                ) AS credit_exposure_cents
            FROM complete_contracts cc
            LEFT JOIN "sla_evaluation" se
              ON se.contract_key = cc.contract_key AND se.month = ?::DATE
            """,
            [month_date],
        )

    persona = question["persona"]
    if trap_name == "display-name-or-billing-key":
        if persona == "east_region":
            con.execute(
                """
                CREATE TEMP TABLE account_fin AS
                SELECT
                    a.account_key,
                    CAST(COALESCE(SUM(am.revenue_cents), 0) AS BIGINT)
                        AS recognized_service_revenue_cents,
                    CAST(COALESCE(
                        SUM(am.revenue_cents - am.credit_cents - am.service_cost_cents), 0
                    ) AS BIGINT) AS contribution_margin_cents
                FROM (SELECT DISTINCT account_key FROM complete_contracts) a
                LEFT JOIN "account_month" am
                  ON am.billing_key = a.account_key AND am.month = ?::DATE
                GROUP BY a.account_key
                """,
                [month_date],
            )
        else:
            con.execute(
                """
                CREATE TEMP TABLE account_fin AS
                SELECT
                    src.account_key,
                    CAST(COALESCE(SUM(am.revenue_cents), 0) AS BIGINT)
                        AS recognized_service_revenue_cents,
                    CAST(COALESCE(
                        SUM(am.revenue_cents - am.credit_cents - am.service_cost_cents), 0
                    ) AS BIGINT) AS contribution_margin_cents
                FROM (SELECT DISTINCT account_key FROM complete_contracts) k
                JOIN "account" src ON src.account_key = k.account_key
                LEFT JOIN "account" fin_a ON fin_a.display_name = src.display_name
                LEFT JOIN "account_month" am
                  ON am.account_key = fin_a.account_key AND am.month = ?::DATE
                GROUP BY src.account_key
                """,
                [month_date],
            )
        con.execute(
            """
            CREATE TEMP TABLE account_care AS
            SELECT
                src.account_key,
                CAST(COALESCE(SUM(ca.escalated), 0) AS BIGINT) AS escalated_cases
            FROM (SELECT DISTINCT account_key FROM complete_contracts) k
            JOIN "account" src ON src.account_key = k.account_key
            LEFT JOIN "account" care_a ON care_a.display_name = src.display_name
            LEFT JOIN "case" ca
              ON ca.account_key = care_a.account_key AND ca.month = ?::DATE
            GROUP BY src.account_key
            """,
            [month_date],
        )
    else:
        con.execute(
            """
            CREATE TEMP TABLE account_fin AS
            SELECT
                k.account_key,
                CAST(am.revenue_cents AS BIGINT) AS recognized_service_revenue_cents,
                CAST(am.revenue_cents - am.credit_cents - am.service_cost_cents AS BIGINT)
                    AS contribution_margin_cents
            FROM (SELECT DISTINCT account_key FROM complete_contracts) k
            JOIN "account_month" am
              ON am.account_key = k.account_key AND am.month = ?::DATE
            """,
            [month_date],
        )
        con.execute(
            """
            CREATE TEMP TABLE account_care AS
            SELECT
                k.account_key,
                CAST(COALESCE(SUM(ca.escalated), 0) AS BIGINT) AS escalated_cases
            FROM (SELECT DISTINCT account_key FROM complete_contracts) k
            LEFT JOIN "case" ca
              ON ca.account_key = k.account_key AND ca.month = ?::DATE
            GROUP BY k.account_key
            """,
            [month_date],
        )

    select_map = {
        "contract_key": "cs.contract_key",
        "account_key": "cs.account_key",
        "eligible_credits_cents": "cs.eligible_credits_cents",
        "credit_exposure_cents": "cs.credit_exposure_cents",
        "recognized_service_revenue_cents": "af.recognized_service_revenue_cents",
        "contribution_margin_cents": "af.contribution_margin_cents",
        "escalated_cases": "ac.escalated_cases",
    }
    selected_exprs = [f"{select_map[name]} AS \"{name}\"" for name in [*keys, *cols]]
    joins = ["FROM contract_sla cs"]
    if "finance" in question["models"]:
        joins.append("LEFT JOIN account_fin af ON af.account_key = cs.account_key")
    if "care" in question["models"]:
        joins.append("LEFT JOIN account_care ac ON ac.account_key = cs.account_key")

    rows = _fetch_rows(
        con.execute(
            f"SELECT {', '.join(selected_exprs)} {' '.join(joins)} ORDER BY {order_sql}"
        )
    )
    return rows, ([] if trap_name == "missing-as-zero" else exclusions)


def _evaluate_account_grain(
    con: duckdb.DuckDBPyConnection,
    question: dict[str, Any],
    *,
    trap_name: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    params = question["parameters"]
    month_str = params["month"]
    month_date, month_start, month_end = _month_bounds(month_str)
    models = set(question["models"])
    keys = question["answer"]["keys"]
    cols = [c["name"] for c in question["answer"]["columns"]]
    col_set = set(cols)
    order_sql = _order_by_sql(question["answer"]["orderBy"], keys, set(keys) | col_set)
    exclusions: list[dict[str, Any]] = []
    empty_incident_zero_mode = False

    if "network" in models:
        inc_count, inc_exclusions = _install_incident_views(
            con, params, site_key_only=(trap_name == "site-key-only")
        )
        exclusions.extend(inc_exclusions)
        if inc_count == 0:
            if trap_name != "missing-as-zero":
                return [], exclusions
            empty_incident_zero_mode = True
            con.execute(
                """
                CREATE TEMP TABLE candidate_accounts AS
                SELECT DISTINCT account_key FROM "account" WHERE enterprise = TRUE
                """
            )
        else:
            con.execute(
                """
                CREATE TEMP TABLE candidate_accounts AS
                SELECT DISTINCT account_key FROM exposed_links
                """
            )
    else:
        acct_clauses = ["a.enterprise = TRUE"]
        bind_acct: list[Any] = []
        if params.get("account_keys"):
            key_list = ", ".join(str(int(k)) for k in params["account_keys"])
            acct_clauses.append(f"a.account_key IN ({key_list})")
        if params.get("max_account_key") is not None:
            acct_clauses.append("a.account_key <= ?")
            bind_acct.append(int(params["max_account_key"]))
        if params.get("breached_only"):
            con.execute(
                f"""
                CREATE TEMP TABLE candidate_accounts AS
                SELECT DISTINCT a.account_key
                FROM "account" a
                JOIN "service_month" sm
                  ON sm.account_key = a.account_key AND sm.month = ?::DATE
                WHERE {' AND '.join(acct_clauses)}
                  AND sm.contracted > 0
                """,
                [month_date, *bind_acct],
            )
        else:
            con.execute(
                f"""
                CREATE TEMP TABLE candidate_accounts AS
                SELECT DISTINCT a.account_key
                FROM "account" a
                JOIN "contract" c ON c.account_key = a.account_key
                WHERE {' AND '.join(acct_clauses)}
                  AND c.valid_from < ?::TIMESTAMPTZ
                  AND ?::TIMESTAMPTZ < c.valid_to
                """,
                [*bind_acct, month_end, month_start],
            )

    excluded_accounts: set[int] = set()
    needs_sla = bool(col_set & {"credit_exposure_cents", "eligible_credits_cents"})
    needs_breach = "breached_services" in col_set

    if trap_name != "missing-as-zero":
        if needs_sla:
            inc_sla = _fetch_rows(
                con.execute(
                    """
                    SELECT DISTINCT ca.account_key, c.contract_key
                    FROM candidate_accounts ca
                    JOIN "contract" c ON c.account_key = ca.account_key
                    LEFT JOIN "sla_evaluation" se
                      ON se.contract_key = c.contract_key AND se.month = ?::DATE
                    WHERE c.valid_from < ?::TIMESTAMPTZ
                      AND ?::TIMESTAMPTZ < c.valid_to
                      AND se.eligible_credit_cents IS NULL
                    ORDER BY ca.account_key, c.contract_key
                    """,
                    [month_date, month_end, month_start],
                )
            )
            for item in inc_sla:
                ak = int(item["account_key"])
                excluded_accounts.add(ak)
                exclusions.append(
                    {
                        "entity": "account",
                        "account_key": ak,
                        "contract_key": int(item["contract_key"]),
                        "month": month_str,
                        "reason": "incomplete_mapping_coverage",
                    }
                )
        if needs_breach:
            inc_sm = _fetch_rows(
                con.execute(
                    """
                    SELECT DISTINCT ca.account_key, sm.service_key
                    FROM candidate_accounts ca
                    JOIN "service_month" sm
                      ON sm.account_key = ca.account_key AND sm.month = ?::DATE
                    WHERE sm.contracted > 0 AND sm.breached IS NULL
                    ORDER BY ca.account_key, sm.service_key
                    """,
                    [month_date],
                )
            )
            for item in inc_sm:
                ak = int(item["account_key"])
                excluded_accounts.add(ak)
                exclusions.append(
                    {
                        "entity": "account",
                        "account_key": ak,
                        "service_key": int(item["service_key"]),
                        "month": month_str,
                        "reason": "incomplete_service_month_coverage",
                    }
                )

    if excluded_accounts:
        excl_sql = ", ".join(str(k) for k in sorted(excluded_accounts))
        con.execute(
            f"CREATE TEMP TABLE complete_accounts AS "
            f"SELECT account_key FROM candidate_accounts WHERE account_key NOT IN ({excl_sql})"
        )
    else:
        con.execute("CREATE TEMP TABLE complete_accounts AS SELECT account_key FROM candidate_accounts")

    if needs_sla:
        if empty_incident_zero_mode:
            con.execute(
                """
                CREATE TEMP TABLE account_sla AS
                SELECT
                    account_key,
                    CAST(0 AS BIGINT) AS eligible_credits_cents,
                    CAST(0 AS BIGINT) AS credit_exposure_cents
                FROM complete_accounts
                """
            )
        elif "network" in models and trap_name == "credit-per-incident":
            con.execute(
                """
                CREATE TEMP TABLE account_sla AS
                SELECT
                    ca.account_key,
                    CAST(COALESCE(SUM(COALESCE(se.eligible_credit_cents, 0)), 0) AS BIGINT)
                        AS eligible_credits_cents,
                    CAST(COALESCE(SUM(
                        COALESCE(se.eligible_credit_cents, 0) - COALESCE(se.applied_credit_cents, 0)
                    ), 0) AS BIGINT) AS credit_exposure_cents
                FROM complete_accounts ca
                JOIN (SELECT DISTINCT incident_key, contract_key, account_key FROM exposed_links) el
                  ON el.account_key = ca.account_key
                LEFT JOIN "sla_evaluation" se
                  ON se.contract_key = el.contract_key AND se.month = ?::DATE
                GROUP BY ca.account_key
                """,
                [month_date],
            )
        elif "network" in models:
            con.execute(
                """
                CREATE TEMP TABLE account_sla AS
                SELECT
                    ca.account_key,
                    CAST(COALESCE(SUM(COALESCE(se.eligible_credit_cents, 0)), 0) AS BIGINT)
                        AS eligible_credits_cents,
                    CAST(COALESCE(SUM(
                        COALESCE(se.eligible_credit_cents, 0) - COALESCE(se.applied_credit_cents, 0)
                    ), 0) AS BIGINT) AS credit_exposure_cents
                FROM complete_accounts ca
                JOIN (SELECT DISTINCT contract_key, account_key FROM exposed_links) ec
                  ON ec.account_key = ca.account_key
                LEFT JOIN "sla_evaluation" se
                  ON se.contract_key = ec.contract_key AND se.month = ?::DATE
                GROUP BY ca.account_key
                """,
                [month_date],
            )
        else:
            having_sql = (
                "HAVING SUM(COALESCE(se.eligible_credit_cents, 0)) > 0"
                if params.get("positive_eligible_only")
                else ""
            )
            con.execute(
                f"""
                CREATE TEMP TABLE account_sla AS
                SELECT
                    ca.account_key,
                    CAST(COALESCE(SUM(COALESCE(se.eligible_credit_cents, 0)), 0) AS BIGINT)
                        AS eligible_credits_cents,
                    CAST(COALESCE(SUM(
                        COALESCE(se.eligible_credit_cents, 0) - COALESCE(se.applied_credit_cents, 0)
                    ), 0) AS BIGINT) AS credit_exposure_cents
                FROM complete_accounts ca
                JOIN "contract" c
                  ON c.account_key = ca.account_key
                 AND c.valid_from < ?::TIMESTAMPTZ
                 AND ?::TIMESTAMPTZ < c.valid_to
                LEFT JOIN "sla_evaluation" se
                  ON se.contract_key = c.contract_key AND se.month = ?::DATE
                GROUP BY ca.account_key
                {having_sql}
                """,
                [month_end, month_start, month_date],
            )
            if params.get("positive_eligible_only"):
                con.execute(
                    "CREATE OR REPLACE TEMP TABLE complete_accounts AS "
                    "SELECT account_key FROM account_sla"
                )

    if needs_breach:
        having_breach = (
            "HAVING SUM(COALESCE(sm.breached, 0)) > 0" if params.get("breached_only") else ""
        )
        con.execute(
            f"""
            CREATE TEMP TABLE account_breach AS
            SELECT
                ca.account_key,
                CAST(COALESCE(SUM(COALESCE(sm.breached, 0)), 0) AS BIGINT) AS breached_services
            FROM complete_accounts ca
            LEFT JOIN "service_month" sm
              ON sm.account_key = ca.account_key AND sm.month = ?::DATE
            GROUP BY ca.account_key
            {having_breach}
            """,
            [month_date],
        )
        if params.get("breached_only"):
            con.execute(
                "CREATE OR REPLACE TEMP TABLE complete_accounts AS "
                "SELECT account_key FROM account_breach"
            )

    if "finance" in models:
        persona = question["persona"]
        if trap_name == "revenue-fan-out":
            if "network" in models and not empty_incident_zero_mode:
                con.execute(
                    """
                    CREATE TEMP TABLE account_fin AS
                    SELECT
                        ca.account_key,
                        CAST(SUM(am.revenue_cents) AS BIGINT) AS recognized_service_revenue_cents,
                        CAST(SUM(
                            am.revenue_cents - am.credit_cents - am.service_cost_cents
                        ) AS BIGINT) AS contribution_margin_cents
                    FROM complete_accounts ca
                    JOIN (
                        SELECT DISTINCT service_key, contract_key, account_key FROM exposed_links
                    ) fan ON fan.account_key = ca.account_key
                    JOIN "account_month" am
                      ON am.account_key = ca.account_key AND am.month = ?::DATE
                    GROUP BY ca.account_key
                    """,
                    [month_date],
                )
            else:
                con.execute(
                    """
                    CREATE TEMP TABLE account_fin AS
                    SELECT
                        ca.account_key,
                        CAST(SUM(am.revenue_cents) AS BIGINT) AS recognized_service_revenue_cents,
                        CAST(SUM(
                            am.revenue_cents - am.credit_cents - am.service_cost_cents
                        ) AS BIGINT) AS contribution_margin_cents
                    FROM complete_accounts ca
                    JOIN "contract" c
                      ON c.account_key = ca.account_key
                     AND c.valid_from < ?::TIMESTAMPTZ
                     AND ?::TIMESTAMPTZ < c.valid_to
                    JOIN (
                        SELECT DISTINCT service_key, contract_key
                        FROM "service_site_map"
                        WHERE valid_from < ?::TIMESTAMPTZ AND ?::TIMESTAMPTZ < valid_to
                    ) svc ON svc.contract_key = c.contract_key
                    JOIN "account_month" am
                      ON am.account_key = ca.account_key AND am.month = ?::DATE
                    GROUP BY ca.account_key
                    """,
                    [month_end, month_start, month_end, month_start, month_date],
                )
        elif trap_name == "display-name-or-billing-key":
            if params.get("use_billing_map") or persona == "east_region":
                con.execute(
                    """
                    CREATE TEMP TABLE account_fin AS
                    SELECT
                        ca.account_key,
                        CAST(COALESCE(SUM(am.revenue_cents), 0) AS BIGINT)
                            AS recognized_service_revenue_cents,
                        CAST(COALESCE(
                            SUM(am.revenue_cents - am.credit_cents - am.service_cost_cents), 0
                        ) AS BIGINT) AS contribution_margin_cents
                    FROM complete_accounts ca
                    LEFT JOIN "account_month" am
                      ON am.billing_key = ca.account_key AND am.month = ?::DATE
                    GROUP BY ca.account_key
                    """,
                    [month_date],
                )
            else:
                con.execute(
                    """
                    CREATE TEMP TABLE account_fin AS
                    SELECT
                        ca.account_key,
                        CAST(COALESCE(SUM(am.revenue_cents), 0) AS BIGINT)
                            AS recognized_service_revenue_cents,
                        CAST(COALESCE(
                            SUM(am.revenue_cents - am.credit_cents - am.service_cost_cents), 0
                        ) AS BIGINT) AS contribution_margin_cents
                    FROM complete_accounts ca
                    JOIN "account" src ON src.account_key = ca.account_key
                    LEFT JOIN "account" fin_a ON fin_a.display_name = src.display_name
                    LEFT JOIN "account_month" am
                      ON am.account_key = fin_a.account_key AND am.month = ?::DATE
                    GROUP BY ca.account_key
                    """,
                    [month_date],
                )
        elif params.get("use_billing_map"):
            con.execute(
                """
                CREATE TEMP TABLE account_fin AS
                SELECT
                    ca.account_key,
                    CAST(am.revenue_cents AS BIGINT) AS recognized_service_revenue_cents,
                    CAST(am.revenue_cents - am.credit_cents - am.service_cost_cents AS BIGINT)
                        AS contribution_margin_cents
                FROM complete_accounts ca
                JOIN "billing_account_map" bm
                  ON bm.account_key = ca.account_key
                 AND bm.valid_from <= ?::TIMESTAMPTZ
                 AND ?::TIMESTAMPTZ < bm.valid_to
                JOIN "account_month" am
                  ON am.billing_key = bm.billing_key
                 AND am.account_key = bm.account_key
                 AND am.month = ?::DATE
                """,
                [month_start, month_start, month_date],
            )
        else:
            con.execute(
                """
                CREATE TEMP TABLE account_fin AS
                SELECT
                    ca.account_key,
                    CAST(am.revenue_cents AS BIGINT) AS recognized_service_revenue_cents,
                    CAST(am.revenue_cents - am.credit_cents - am.service_cost_cents AS BIGINT)
                        AS contribution_margin_cents
                FROM complete_accounts ca
                JOIN "account_month" am
                  ON am.account_key = ca.account_key AND am.month = ?::DATE
                """,
                [month_date],
            )

    if "care" in models:
        if trap_name == "display-name-or-billing-key":
            con.execute(
                """
                CREATE TEMP TABLE account_cases AS
                SELECT
                    ca.account_key,
                    CAST(COALESCE(SUM(c.escalated), 0) AS BIGINT) AS escalated_cases,
                    CAST(COALESCE(SUM(c.repeat_contact), 0) AS BIGINT) AS repeat_contacts
                FROM complete_accounts ca
                JOIN "account" src ON src.account_key = ca.account_key
                LEFT JOIN "account" care_a ON care_a.display_name = src.display_name
                LEFT JOIN "case" c
                  ON c.account_key = care_a.account_key AND c.month = ?::DATE
                GROUP BY ca.account_key
                """,
                [month_date],
            )
        else:
            con.execute(
                """
                CREATE TEMP TABLE account_cases AS
                SELECT
                    ca.account_key,
                    CAST(COALESCE(SUM(c.escalated), 0) AS BIGINT) AS escalated_cases,
                    CAST(COALESCE(SUM(c.repeat_contact), 0) AS BIGINT) AS repeat_contacts
                FROM complete_accounts ca
                LEFT JOIN "case" c
                  ON c.account_key = ca.account_key AND c.month = ?::DATE
                GROUP BY ca.account_key
                """,
                [month_date],
            )

        if "open_cases_eod" in col_set:
            if trap_name == "missing-as-zero":
                con.execute(
                    """
                    CREATE TEMP TABLE account_open_eod AS
                    SELECT
                        ca.account_key,
                        CAST(COALESCE(SUM(cds.open_eod), 0) AS BIGINT) AS open_cases_eod
                    FROM complete_accounts ca
                    LEFT JOIN "case_daily_snapshot" cds
                      ON cds.account_key = ca.account_key AND cds.month = ?::DATE
                    GROUP BY ca.account_key
                    """,
                    [month_date],
                )
            elif trap_name == "display-name-or-billing-key":
                con.execute(
                    """
                    CREATE TEMP TABLE account_open_eod AS
                    SELECT
                        ca.account_key,
                        CAST(COALESCE(SUM(cds.open_eod), 0) AS BIGINT) AS open_cases_eod
                    FROM complete_accounts ca
                    JOIN "account" src ON src.account_key = ca.account_key
                    LEFT JOIN "account" care_a ON care_a.display_name = src.display_name
                    LEFT JOIN "case_daily_snapshot" cds
                      ON cds.account_key = care_a.account_key AND cds.snapshot_date = ?::DATE
                    GROUP BY ca.account_key
                    """,
                    [params["snapshot_date"]],
                )
            else:
                con.execute(
                    """
                    CREATE TEMP TABLE account_open_eod AS
                    SELECT
                        ca.account_key,
                        CAST(COALESCE(SUM(cds.open_eod), 0) AS BIGINT) AS open_cases_eod
                    FROM complete_accounts ca
                    LEFT JOIN "case_daily_snapshot" cds
                      ON cds.account_key = ca.account_key AND cds.snapshot_date = ?::DATE
                    GROUP BY ca.account_key
                    """,
                    [params["snapshot_date"]],
                )

    select_map = {
        "account_key": "ca.account_key",
        "eligible_credits_cents": "asla.eligible_credits_cents",
        "credit_exposure_cents": "asla.credit_exposure_cents",
        "breached_services": "abr.breached_services",
        "recognized_service_revenue_cents": "afin.recognized_service_revenue_cents",
        "contribution_margin_cents": "afin.contribution_margin_cents",
        "escalated_cases": "acase.escalated_cases",
        "repeat_contacts": "acase.repeat_contacts",
        "open_cases_eod": "aoeod.open_cases_eod",
    }
    selected_exprs = [f"{select_map[name]} AS \"{name}\"" for name in [*keys, *cols]]
    joins = ["FROM complete_accounts ca"]
    if needs_sla:
        joins.append("JOIN account_sla asla ON asla.account_key = ca.account_key")
    if needs_breach:
        joins.append("JOIN account_breach abr ON abr.account_key = ca.account_key")
    if "finance" in models:
        joins.append("JOIN account_fin afin ON afin.account_key = ca.account_key")
    if "care" in models:
        joins.append("JOIN account_cases acase ON acase.account_key = ca.account_key")
        if "open_cases_eod" in col_set:
            joins.append("JOIN account_open_eod aoeod ON aoeod.account_key = ca.account_key")

    rows = _fetch_rows(
        con.execute(
            f"SELECT {', '.join(selected_exprs)} {' '.join(joins)} ORDER BY {order_sql}"
        )
    )
    return rows, ([] if trap_name == "missing-as-zero" else exclusions)


def _clear_temp_tables(con: duckdb.DuckDBPyConnection) -> None:
    for temp_name in _TEMP_TABLES:
        con.execute(f'DROP TABLE IF EXISTS temp."{temp_name}"')


def _run_evaluation(
    question: dict[str, Any],
    data_dir: Path,
    *,
    trap_name: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    validate_question_record(question)
    con, lock = _get_cached_connection(data_dir, question["persona"])
    with lock:
        _clear_temp_tables(con)
        try:
            if len(question["models"]) == 1:
                return _evaluate_l1(con, question, trap_name=trap_name)
            keys = question["answer"]["keys"]
            if keys == ["service_key"]:
                return _evaluate_service_grain(con, question, trap_name=trap_name)
            if keys[0] == "contract_key":
                return _evaluate_contract_grain(con, question, trap_name=trap_name)
            if keys == ["account_key"]:
                return _evaluate_account_grain(con, question, trap_name=trap_name)
            raise ValueError(f"Unsupported key grain {keys!r} in question {question['id']}")
        finally:
            _clear_temp_tables(con)


def evaluate_question(question: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    """Evaluate a golden question with rules R1-R5 using DuckDB directly on Parquet files."""
    rows, exclusions = _run_evaluation(question, data_dir, trap_name=None)
    return {
        "question_id": question["id"],
        "level": question["level"],
        "persona": question["persona"],
        "rows": rows,
        "exclusions": exclusions,
        "status": "PARTIAL" if exclusions else "COMPLETE",
    }


def evaluate_trap(trap_name: str, question: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    """Evaluate the named anti-pattern trap variant for a golden question."""
    if trap_name not in VALID_TRAPS:
        raise ValueError(f"Unknown trap {trap_name!r}; expected one of {VALID_TRAPS}")
    rows, exclusions = _run_evaluation(question, data_dir, trap_name=trap_name)
    return {
        "question_id": question["id"],
        "level": question["level"],
        "persona": question["persona"],
        "trap": trap_name,
        "rows": rows,
        "exclusions": exclusions,
        "status": "PARTIAL" if exclusions else "COMPLETE",
    }


def evaluate_suite(
    questions: list[dict[str, Any]],
    data_dir: Path,
    *,
    include_traps: bool = True,
) -> list[dict[str, Any]]:
    """Evaluate a list of golden questions and optionally attach their trap answers."""
    results: list[dict[str, Any]] = []
    for question in questions:
        oracle_result = evaluate_question(question, data_dir)
        if include_traps:
            oracle_result["trap_answers"] = {
                trap: evaluate_trap(trap, question, data_dir)
                for trap in question["traps"]
            }
        results.append(oracle_result)
    return results
