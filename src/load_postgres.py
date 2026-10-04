"""Load the prepared project data into PostgreSQL and enforce quality gates.

The loader performs a reproducible full refresh of the ``analytics`` schema.
It verifies source manifests before changing the database, loads tables in
foreign-key order, reconciles database totals to the synthetic-data manifest,
and executes the SQL validation suite in the same transaction as the load.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

DEFAULT_DATABASE_URL = "dbname=orlando_demand_revenue"
COPY_CHUNK_SIZE = 1024 * 1024


class DataLoadError(RuntimeError):
    """Raised when source verification, loading, or reconciliation fails."""


@dataclass(frozen=True)
class LoadSpec:
    """Describe one CSV-to-PostgreSQL table mapping."""

    table_name: str
    relative_path: str
    columns: tuple[str, ...]
    manifest_name: str
    manifest_group: str


LOAD_SPECS = (
    LoadSpec(
        table_name="dim_date",
        relative_path="data/processed/public/dim_date.csv",
        columns=(
            "date_key",
            "calendar_date",
            "day_of_week",
            "day_name",
            "day_of_year",
            "week_start",
            "week_of_year",
            "month_number",
            "month_name",
            "quarter_number",
            "year_number",
            "is_weekend",
            "holiday_flag",
            "holiday_name",
            "school_break_flag",
            "season",
        ),
        manifest_name="dim_date",
        manifest_group="public",
    ),
    LoadSpec(
        table_name="dim_product",
        relative_path="data/processed/synthetic/dim_product.csv",
        columns=(
            "product_key",
            "product_code",
            "product_name",
            "product_type",
            "ticket_tier",
            "base_price",
            "valid_from",
            "valid_to",
            "is_active",
        ),
        manifest_name="dim_product",
        manifest_group="synthetic",
    ),
    LoadSpec(
        table_name="dim_channel",
        relative_path="data/processed/synthetic/dim_channel.csv",
        columns=(
            "channel_key",
            "channel_code",
            "channel_name",
            "channel_type",
            "is_active",
        ),
        manifest_name="dim_channel",
        manifest_group="synthetic",
    ),
    LoadSpec(
        table_name="dim_campaign",
        relative_path="data/processed/synthetic/dim_campaign.csv",
        columns=(
            "campaign_key",
            "campaign_code",
            "campaign_name",
            "campaign_type",
            "target_segment",
            "start_date",
            "end_date",
            "discount_type",
            "planned_discount_value",
            "is_no_campaign",
        ),
        manifest_name="dim_campaign",
        manifest_group="synthetic",
    ),
    LoadSpec(
        table_name="fact_weather",
        relative_path="data/processed/public/fact_weather.csv",
        columns=(
            "date_key",
            "min_temperature_f",
            "avg_temperature_f",
            "max_temperature_f",
            "precipitation_in",
            "severe_weather_flag",
            "source_name",
            "source_station_id",
        ),
        manifest_name="fact_weather",
        manifest_group="public",
    ),
    LoadSpec(
        table_name="fact_daily_plan",
        relative_path="data/processed/synthetic/fact_daily_plan.csv",
        columns=(
            "date_key",
            "available_capacity",
            "demand_target",
            "revenue_target",
            "planned_staff_hours",
            "plan_version",
        ),
        manifest_name="fact_daily_plan",
        manifest_group="synthetic",
    ),
    LoadSpec(
        table_name="fact_campaign_daily",
        relative_path="data/processed/synthetic/fact_campaign_daily.csv",
        columns=(
            "date_key",
            "campaign_key",
            "spend",
            "impressions",
            "clicks",
            "conversions",
            "attributed_revenue",
        ),
        manifest_name="fact_campaign_daily",
        manifest_group="synthetic",
    ),
    LoadSpec(
        table_name="fact_ticket_sales",
        relative_path="data/processed/synthetic/fact_ticket_sales.csv",
        columns=(
            "order_id",
            "order_line_number",
            "purchase_date_key",
            "visit_date_key",
            "refund_date_key",
            "product_key",
            "channel_key",
            "campaign_key",
            "units_sold",
            "units_refunded",
            "unit_list_price",
            "gross_revenue",
            "discount_amount",
            "refund_amount",
            "net_revenue",
            "sale_status",
            "currency_code",
            "booked_at",
        ),
        manifest_name="fact_ticket_sales",
        manifest_group="synthetic",
    ),
)

REFRESH_TABLES = (
    "fact_forecast",
    "fact_ticket_sales",
    "fact_campaign_daily",
    "fact_daily_plan",
    "fact_weather",
    "dim_campaign",
    "dim_channel",
    "dim_product",
    "dim_date",
)

IDENTITY_DIMENSIONS = (
    ("dim_product", "product_key"),
    ("dim_channel", "channel_key"),
    ("dim_campaign", "campaign_key"),
)


# Purpose: Parse command-line settings without exposing connection credentials.
# Used by: main.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load prepared Orlando planning data into PostgreSQL."
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root containing data/ and sql/.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help=(
            "PostgreSQL connection string. Defaults to DATABASE_URL or "
            f"'{DEFAULT_DATABASE_URL}'."
        ),
    )
    return parser.parse_args()


# Purpose: Read JSON while preserving decimal values exactly for reconciliation.
# Used by: load_manifests.
def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DataLoadError(f"Required manifest is missing: {path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle, parse_float=Decimal)


# Purpose: Load the public and synthetic manifests that define source contracts.
# Used by: run_load and unit tests.
def load_manifests(project_root: Path) -> dict[str, dict[str, Any]]:
    return {
        "public": read_json(
            project_root
            / "data"
            / "processed"
            / "public"
            / "public_data_manifest.json"
        ),
        "synthetic": read_json(
            project_root
            / "data"
            / "processed"
            / "synthetic"
            / "synthetic_data_manifest.json"
        ),
    }


# Purpose: Compute a file hash so generated sources can be checked for changes.
# Used by: verify_source_files.
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(COPY_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


# Purpose: Read only the CSV header and preserve its declared column order.
# Used by: verify_source_files.
def read_csv_header(path: Path) -> tuple[str, ...]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            return tuple(next(reader))
        except StopIteration as error:
            raise DataLoadError(f"CSV is empty: {path}") from error


# Purpose: Return the manifest entry that owns a CSV load specification.
# Used by: verify_source_files, expected_row_counts.
def manifest_entry(
    spec: LoadSpec, manifests: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    outputs = manifests[spec.manifest_group].get("outputs", {})
    entry = outputs.get(spec.manifest_name)
    if not isinstance(entry, dict):
        raise DataLoadError(
            f"Manifest entry is missing for {spec.manifest_group}:{spec.manifest_name}"
        )
    return entry


# Purpose: Verify paths, CSV headers, manifest paths, and available checksums.
# Used by: run_load before any database table is changed.
def verify_source_files(
    project_root: Path,
    manifests: dict[str, dict[str, Any]],
    specs: Sequence[LoadSpec] = LOAD_SPECS,
) -> None:
    problems: list[str] = []
    for spec in specs:
        path = project_root / spec.relative_path
        if not path.is_file():
            problems.append(f"Missing CSV: {spec.relative_path}")
            continue

        actual_header = read_csv_header(path)
        if actual_header != spec.columns:
            problems.append(
                f"Header mismatch for {spec.relative_path}: "
                f"expected {list(spec.columns)}, received {list(actual_header)}"
            )

        entry = manifest_entry(spec, manifests)
        if entry.get("path") != spec.relative_path:
            problems.append(
                f"Manifest path mismatch for {spec.table_name}: {entry.get('path')}"
            )

        expected_hash = entry.get("sha256")
        if expected_hash and sha256_file(path) != expected_hash:
            problems.append(f"Checksum mismatch for {spec.relative_path}")

    if problems:
        raise DataLoadError("Source verification failed:\n- " + "\n- ".join(problems))


# Purpose: Execute a complete SQL file without unsafe semicolon splitting.
# Used by: apply_schema and run_validation_script.
def execute_sql_script(
    cursor: psycopg.Cursor[Any], script: str
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    cursor.execute(script, prepare=False)
    while True:
        if cursor.description is not None:
            rows.extend(cursor.fetchall())
        if not cursor.nextset():
            break
    return rows


# Purpose: Create the analytics schema before the transactional data refresh.
# Used by: run_load.
def apply_schema(database_url: str, schema_path: Path) -> None:
    if not schema_path.is_file():
        raise DataLoadError(f"Schema SQL is missing: {schema_path}")
    with (
        psycopg.connect(database_url, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        execute_sql_script(cursor, schema_path.read_text(encoding="utf-8"))


# Purpose: Remove the validation file's outer transaction for nested execution.
# Used by: run_validation_script and unit tests.
def strip_outer_transaction(script: str) -> str:
    lines = script.strip().splitlines()
    begin_indexes = [
        index for index, line in enumerate(lines) if line.strip().upper() == "BEGIN;"
    ]
    if len(begin_indexes) != 1 or lines[-1].strip().upper() != "COMMIT;":
        raise DataLoadError(
            "Validation SQL must contain one BEGIN statement and a final COMMIT."
        )
    del lines[begin_indexes[0]]
    lines.pop()
    return "\n".join(lines).strip() + "\n"


# Purpose: Delete prior loaded data so the full refresh is repeatable.
# Used by: run_load inside the load transaction.
def truncate_analytics_tables(cursor: psycopg.Cursor[Any]) -> None:
    tables = sql.SQL(", ").join(
        sql.SQL("analytics.{}").format(sql.Identifier(table))
        for table in REFRESH_TABLES
    )
    cursor.execute(
        sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(tables)
    )


# Purpose: Stream one CSV into PostgreSQL using its explicit column contract.
# Used by: load_all_tables.
def copy_csv_to_table(
    cursor: psycopg.Cursor[Any], project_root: Path, spec: LoadSpec
) -> None:
    copy_statement = sql.SQL(
        "COPY analytics.{} ({}) FROM STDIN "
        "WITH (FORMAT CSV, HEADER TRUE, NULL '')"
    ).format(
        sql.Identifier(spec.table_name),
        sql.SQL(", ").join(sql.Identifier(column) for column in spec.columns),
    )
    path = project_root / spec.relative_path
    with (
        cursor.copy(copy_statement) as copy,
        path.open(encoding="utf-8", newline="") as handle,
    ):
        while chunk := handle.read(COPY_CHUNK_SIZE):
            copy.write(chunk)


# Purpose: Load all dimensions and facts in foreign-key-safe order.
# Used by: run_load.
def load_all_tables(
    cursor: psycopg.Cursor[Any],
    project_root: Path,
    specs: Sequence[LoadSpec] = LOAD_SPECS,
) -> None:
    for spec in specs:
        copy_csv_to_table(cursor, project_root, spec)


# Purpose: Advance identity sequences after dimensions are loaded with fixed keys.
# Used by: run_load after COPY completes.
def reset_dimension_sequences(cursor: psycopg.Cursor[Any]) -> None:
    for table_name, key_column in IDENTITY_DIMENSIONS:
        statement = sql.SQL(
            "SELECT setval("
            "pg_get_serial_sequence(%s, %s), "
            "COALESCE(max({key}), 1), "
            "max({key}) IS NOT NULL"
            ") FROM analytics.{table}"
        ).format(
            key=sql.Identifier(key_column),
            table=sql.Identifier(table_name),
        )
        cursor.execute(
            statement,
            (f"analytics.{table_name}", key_column),
        )


# Purpose: Build the expected PostgreSQL row count for every loaded table.
# Used by: compare_reconciliation.
def expected_row_counts(
    manifests: dict[str, dict[str, Any]],
    specs: Sequence[LoadSpec] = LOAD_SPECS,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for spec in specs:
        entry = manifest_entry(spec, manifests)
        counts[spec.table_name] = int(entry["rows"])
    return counts


# Purpose: Query loaded row counts and financial controls from PostgreSQL.
# Used by: run_load and reconciliation tests through compare_reconciliation.
def database_reconciliation(cursor: psycopg.Cursor[Any]) -> dict[str, Any]:
    row_counts: dict[str, int] = {}
    for spec in LOAD_SPECS:
        cursor.execute(
            sql.SQL("SELECT count(*) FROM analytics.{}").format(
                sql.Identifier(spec.table_name)
            )
        )
        row_counts[spec.table_name] = int(cursor.fetchone()[0])

    cursor.execute(
        """
        SELECT
            count(*)::bigint AS order_lines,
            COALESCE(sum(units_sold), 0)::bigint AS units_sold,
            COALESCE(sum(units_refunded), 0)::bigint AS units_refunded,
            COALESCE(sum(gross_revenue), 0)::numeric(20, 2) AS gross_revenue,
            COALESCE(sum(discount_amount), 0)::numeric(20, 2) AS discounts,
            COALESCE(sum(refund_amount), 0)::numeric(20, 2) AS refunds,
            COALESCE(sum(net_revenue), 0)::numeric(20, 2) AS net_revenue,
            CASE
                WHEN COALESCE(sum(units_sold), 0) = 0 THEN 0
                ELSE round(
                    sum(units_refunded)::numeric / sum(units_sold),
                    6
                )
            END AS refund_rate
        FROM analytics.fact_ticket_sales
        """
    )
    financial_row = cursor.fetchone()
    financial_columns = [description.name for description in cursor.description]
    financials = dict(zip(financial_columns, financial_row, strict=True))

    cursor.execute(
        """
        WITH daily_actuals AS (
            SELECT visit_date_key AS date_key, sum(net_units)::integer AS demand
            FROM analytics.fact_ticket_sales
            GROUP BY visit_date_key
        )
        SELECT count(*)::integer
        FROM daily_actuals AS actuals
        JOIN analytics.fact_daily_plan AS plan USING (date_key)
        WHERE actuals.demand >= plan.available_capacity
        """
    )
    financials["capacity_constrained_days"] = int(cursor.fetchone()[0])
    return {"row_counts": row_counts, "financials": financials}


# Purpose: Compare database results with both source manifests using exact controls.
# Used by: run_load and unit tests.
def compare_reconciliation(
    actual: dict[str, Any], manifests: dict[str, dict[str, Any]]
) -> None:
    problems: list[str] = []
    expected_counts = expected_row_counts(manifests)
    for table_name, expected in expected_counts.items():
        received = actual["row_counts"].get(table_name)
        if received != expected:
            problems.append(
                f"{table_name} row count: expected {expected}, received {received}"
            )

    expected_financials = manifests["synthetic"].get("validation", {})
    exact_metrics = (
        "order_lines",
        "units_sold",
        "units_refunded",
        "capacity_constrained_days",
    )
    for metric in exact_metrics:
        expected = int(expected_financials[metric])
        received = int(actual["financials"][metric])
        if received != expected:
            problems.append(f"{metric}: expected {expected}, received {received}")

    money_metrics = (
        "gross_revenue",
        "discounts",
        "refunds",
        "net_revenue",
    )
    for metric in money_metrics:
        expected = Decimal(expected_financials[metric]).quantize(Decimal("0.01"))
        received = Decimal(actual["financials"][metric]).quantize(Decimal("0.01"))
        if received != expected:
            problems.append(f"{metric}: expected {expected}, received {received}")

    expected_rate = Decimal(expected_financials["refund_rate"]).quantize(
        Decimal("0.000001")
    )
    received_rate = Decimal(actual["financials"]["refund_rate"]).quantize(
        Decimal("0.000001")
    )
    if received_rate != expected_rate:
        problems.append(
            f"refund_rate: expected {expected_rate}, received {received_rate}"
        )

    if problems:
        raise DataLoadError("Database reconciliation failed:\n- " + "\n- ".join(problems))


# Purpose: Execute the repository SQL quality gate inside the load transaction.
# Used by: run_load after manifest reconciliation passes.
def run_validation_script(
    cursor: psycopg.Cursor[Any], validation_path: Path
) -> list[tuple[Any, ...]]:
    if not validation_path.is_file():
        raise DataLoadError(f"Validation SQL is missing: {validation_path}")
    script = strip_outer_transaction(validation_path.read_text(encoding="utf-8"))
    return execute_sql_script(cursor, script)


# Purpose: Format the final database controls without printing connection secrets.
# Used by: main.
def format_summary(summary: dict[str, Any]) -> str:
    financials = summary["financials"]
    return "\n".join(
        [
            "PostgreSQL load completed and all validations passed.",
            f"Order lines: {int(financials['order_lines']):,}",
            f"Units sold: {int(financials['units_sold']):,}",
            f"Units refunded: {int(financials['units_refunded']):,}",
            f"Net revenue: ${Decimal(financials['net_revenue']):,.2f}",
            (
                "Capacity-constrained days: "
                f"{int(financials['capacity_constrained_days']):,}"
            ),
        ]
    )


# Purpose: Orchestrate schema creation, atomic loading, reconciliation, and SQL tests.
# Used by: main and integration workflows.
def run_load(project_root: Path, database_url: str) -> dict[str, Any]:
    project_root = project_root.resolve()
    manifests = load_manifests(project_root)
    verify_source_files(project_root, manifests)

    apply_schema(database_url, project_root / "sql" / "schema.sql")
    with (
        psycopg.connect(database_url) as connection,
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL search_path TO analytics, public")
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (2_026_100_4,))
        truncate_analytics_tables(cursor)
        load_all_tables(cursor, project_root)
        reset_dimension_sequences(cursor)
        summary = database_reconciliation(cursor)
        compare_reconciliation(summary, manifests)
        run_validation_script(
            cursor,
            project_root / "sql" / "tests" / "validation_queries.sql",
        )
    return summary


# Purpose: Provide the command-line entry point for the PostgreSQL loading step.
# Used by: Direct execution with ``python -m src.load_postgres``.
def main() -> None:
    args = parse_args()
    try:
        summary = run_load(args.project_root, args.database_url)
    except (DataLoadError, psycopg.Error) as error:
        raise SystemExit(f"PostgreSQL load failed: {error}") from error
    print(format_summary(summary))


if __name__ == "__main__":
    main()
