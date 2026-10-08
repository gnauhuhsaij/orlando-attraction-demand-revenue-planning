"""Build the portfolio Tableau workbook from validated PostgreSQL views.

The builder exports small dashboard-ready CSV snapshots, creates an editable
Tableau workbook, and packages the workbook with its data as a portable TWBX.
"""

from __future__ import annotations

import argparse
import os
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd
from sqlalchemy import Engine, create_engine, text

WORKBOOK_NAME = "orlando_demand_revenue_planning"
DEFAULT_DATABASE_URL = "postgresql+psycopg:///orlando_demand_revenue"
TABLEAU_VERSION = "18.1"
TABLEAU_BUILD = "2026.2.3 (20262.26.0912.1023)"
NAVY = "#17324D"
TEAL = "#1F8A89"
ORANGE = "#E07A3F"
RED = "#C84A4A"
LIGHT_BLUE = "#EAF2F8"
LIGHT_GRAY = "#F4F6F7"


@dataclass(frozen=True)
class DataSourceSpec:
    """Describe one CSV-backed Tableau data source."""

    key: str
    caption: str
    filename: str
    frame: pd.DataFrame


@dataclass(frozen=True)
class FieldSpec:
    """Describe a field instance used by one worksheet."""

    name: str
    operation: str = "none"


# Purpose: Parse repeatable workbook output and database settings.
# Used by: main.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the Orlando Tableau workbook and packaged TWBX."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy PostgreSQL URL.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root containing tableau/.",
    )
    return parser.parse_args()


# Purpose: Execute one dashboard query and normalize date columns for Tableau.
# Used by: load_dashboard_data.
def read_dashboard_query(
    engine: Engine,
    query: str,
    date_columns: tuple[str, ...] = (),
) -> pd.DataFrame:
    with engine.connect() as connection:
        frame = pd.read_sql_query(text(query), connection)
    for column in date_columns:
        frame[column] = pd.to_datetime(frame[column]).dt.strftime("%Y-%m-%d")
    return frame


# Purpose: Load the validated views at the grains needed by the dashboards.
# Used by: main before CSV export and workbook generation.
def load_dashboard_data(engine: Engine) -> dict[str, pd.DataFrame]:
    planning = read_dashboard_query(
        engine,
        """
        WITH demand_accuracy AS (
            SELECT mae, mape_pct, interval_coverage_pct
            FROM analytics.vw_forecast_accuracy
            WHERE model_role = 'selected'
              AND horizon_bucket = 'All Horizons'
            LIMIT 1
        ), revenue_accuracy AS (
            SELECT mae, wape_pct, interval_coverage_pct
            FROM analytics.vw_revenue_forecast_accuracy
            WHERE model_role = 'selected'
              AND horizon_bucket = 'All Horizons'
            LIMIT 1
        )
        SELECT
            action.*,
            to_char(action.target_date, 'Mon FMDD') AS short_date,
            CASE
                WHEN action.forecast_horizon_days <= 7 THEN 'Days 1-7'
                WHEN action.forecast_horizon_days <= 14 THEN 'Days 8-14'
                WHEN action.forecast_horizon_days <= 21 THEN 'Days 15-21'
                ELSE 'Days 22-30'
            END AS horizon_bucket,
            demand_accuracy.mae AS selected_demand_mae,
            demand_accuracy.mape_pct AS selected_demand_mape_pct,
            demand_accuracy.interval_coverage_pct
                AS selected_demand_coverage_pct,
            revenue_accuracy.mae AS selected_revenue_mae,
            revenue_accuracy.wape_pct AS selected_revenue_wape_pct,
            revenue_accuracy.interval_coverage_pct
                AS selected_revenue_coverage_pct,
            MAX(action.forecast_capacity_utilization) OVER ()
                AS peak_capacity_utilization,
            CASE
                WHEN action.recommended_action_code <> 'maintain_plan'
                  OR action.revenue_status <> 'revenue_on_plan'
                    THEN 'Review'
                ELSE 'No change'
            END AS decision_scope,
            CASE
                WHEN action.recommended_action_code = 'protect_capacity'
                    THEN 'Protect capacity: pause broad promotion and review price.'
                WHEN action.recommended_action_code = 'monitor_high_demand'
                    THEN 'Prepare for high demand: confirm staffing and inventory.'
                WHEN action.recommended_action_code = 'revenue_recovery_review'
                    THEN 'Review revenue recovery: check price, mix, and conversion.'
                WHEN action.recommended_action_code = 'targeted_promotion_review'
                    THEN 'Consider a targeted offer after checking margin.'
                WHEN action.revenue_status = 'revenue_upside'
                    THEN concat(
                        'Protect price: revenue is ',
                        to_char(action.forecast_revenue_variance_pct * 100, 'FM990.0'),
                        '% above plan.'
                    )
                WHEN action.revenue_status = 'below_target_watch'
                    THEN concat(
                        'Monitor revenue: forecast is ',
                        to_char(abs(action.forecast_revenue_variance_pct) * 100, 'FM990.0'),
                        '% below plan, but the target remains within the forecast range.'
                    )
                ELSE 'Keep the current plan.'
            END AS decision_label,
            concat(
                to_char(action.target_date, 'Mon DD'), ' | ',
                replace(initcap(action.revenue_status), '_', ' '),
                ' | Demand gap ',
                to_char(action.forecast_demand_variance_pct * 100, 'FM990.0'),
                '% | Revenue gap ',
                to_char(action.forecast_revenue_variance_pct * 100, 'FM990.0'),
                '%'
            ) AS watchlist_label,
            CASE
                WHEN action.business_action_rank <= 5 THEN 'Top 5'
                ELSE 'Other'
            END AS action_watch_scope,
            CASE
                WHEN action.demand_watch_rank <= 5 THEN 'Top 5'
                ELSE 'Other'
            END AS demand_watch_scope
        FROM analytics.vw_daily_business_action_plan AS action
        CROSS JOIN demand_accuracy
        CROSS JOIN revenue_accuracy
        ORDER BY action.target_date
        """,
        ("target_date", "forecast_created_date", "training_end_date"),
    )

    demand_series = read_dashboard_query(
        engine,
        """
        SELECT
            action.target_date,
            to_char(action.target_date, 'Mon FMDD') AS forecast_date,
            action.forecast_horizon_days,
            series.series_name,
            series.series_order,
            series.tickets,
            CASE
                WHEN series.series_name IN ('Forecast', 'Demand plan', 'Capacity')
                    THEN 'Planning'
                ELSE 'Uncertainty'
            END AS series_scope
        FROM analytics.vw_daily_business_action_plan AS action
        CROSS JOIN LATERAL (
            VALUES
                ('Forecast', 1, action.predicted_demand),
                ('Low estimate', 2, action.demand_lower_bound),
                ('High estimate', 3, action.demand_upper_bound),
                ('Demand plan', 4, action.demand_target::numeric),
                ('Capacity', 5, action.available_capacity::numeric)
        ) AS series (series_name, series_order, tickets)
        ORDER BY action.target_date, series.series_order
        """,
        ("target_date",),
    )

    revenue_series = read_dashboard_query(
        engine,
        """
        SELECT
            action.target_date,
            to_char(action.target_date, 'Mon FMDD') AS forecast_date,
            action.forecast_horizon_days,
            series.series_name,
            series.series_order,
            series.net_revenue_dollars
        FROM analytics.vw_daily_business_action_plan AS action
        CROSS JOIN LATERAL (
            VALUES
                ('Forecast', 1, action.predicted_net_revenue),
                ('Low estimate', 2, action.revenue_lower_bound),
                ('High estimate', 3, action.revenue_upper_bound),
                ('Revenue plan', 4, action.revenue_target::numeric)
        ) AS series (series_name, series_order, net_revenue_dollars)
        ORDER BY action.target_date, series.series_order
        """,
        ("target_date",),
    )

    accuracy = read_dashboard_query(
        engine,
        """
        WITH accuracy_metrics AS (
            SELECT
                'Demand'::text AS forecast_domain,
                horizon_bucket,
                observations,
                mae,
                mape_pct AS percentage_error,
                interval_coverage_pct
            FROM analytics.vw_forecast_accuracy
            WHERE model_role = 'selected'
              AND horizon_bucket <> 'All Horizons'
            UNION ALL
            SELECT
                'Revenue'::text AS forecast_domain,
                horizon_bucket,
                observations,
                mae,
                wape_pct AS percentage_error,
                interval_coverage_pct
            FROM analytics.vw_revenue_forecast_accuracy
            WHERE model_role = 'selected'
              AND horizon_bucket <> 'All Horizons'
        )
        SELECT
            forecast_domain,
            CASE horizon_bucket
                WHEN 'Days 1-7' THEN '01-07 days'
                WHEN 'Days 8-14' THEN '08-14 days'
                WHEN 'Days 15-21' THEN '15-21 days'
                ELSE '22-30 days'
            END AS forecast_range,
            observations,
            CASE WHEN forecast_domain = 'Demand' THEN mae END
                AS demand_mae_tickets,
            CASE WHEN forecast_domain = 'Revenue' THEN percentage_error END
                / 100.0 AS revenue_wape_ratio,
            interval_coverage_pct
        FROM accuracy_metrics
        ORDER BY forecast_domain, forecast_range
        """,
    )

    campaign = read_dashboard_query(
        engine,
        """
        SELECT
            evaluation.outcome_name,
            replace(initcap(evaluation.outcome_name), '_', ' ')
                AS outcome_label,
            evaluation.estimator_name,
            CASE evaluation.estimator_name
                WHEN 'matched_block_bootstrap'
                    THEN 'Similar non-campaign dates'
                WHEN 'regression_hac'
                    THEN 'Adjusted for observed factors'
                ELSE evaluation.estimator_name
            END AS estimator_label,
            evaluation.period_name,
            evaluation.observations,
            evaluation.estimate_value,
            evaluation.estimate_pct,
            evaluation.estimate_pct / 100.0 AS estimate_ratio,
            evaluation.lower_bound,
            evaluation.upper_bound,
            evaluation.p_value,
            evaluation.interval_excludes_zero,
            summary.estimated_incremental_net_revenue,
            summary.marketing_spend,
            summary.associated_return_after_marketing_spend
                AS associated_return_after_spend,
            summary.revenue_evidence,
            summary.ticket_yield_evidence,
            summary.causal_claim_allowed
        FROM analytics.vw_latest_campaign_evaluation AS evaluation
        CROSS JOIN analytics.vw_latest_campaign_analysis_summary AS summary
        ORDER BY evaluation.outcome_name, evaluation.estimator_name
        """,
    )

    product = read_dashboard_query(
        engine,
        """
        SELECT
            target_date,
            product_name,
            ticket_tier,
            base_price,
            predicted_product_share,
            predicted_product_demand,
            predicted_product_net_yield,
            predicted_product_net_revenue
        FROM analytics.vw_latest_product_revenue_forecast
        ORDER BY target_date, product_name
        """,
        ("target_date",),
    )

    return {
        "planning": planning,
        "demand_series": demand_series,
        "revenue_series": revenue_series,
        "accuracy": accuracy,
        "campaign": campaign,
        "product": product,
    }


# Purpose: Fail early when a dashboard dataset is empty or violates its grain.
# Used by: main before writing deliverables.
def validate_dashboard_data(data: dict[str, pd.DataFrame]) -> None:
    for name, frame in data.items():
        if frame.empty:
            raise ValueError(f"Tableau dataset {name!r} is empty")

    planning = data["planning"]
    if len(planning) != 30 or planning["target_date"].duplicated().any():
        raise ValueError("Planning data must contain 30 unique target dates")
    if set(planning["recommended_action_code"]) - {
        "protect_capacity",
        "monitor_high_demand",
        "revenue_recovery_review",
        "targeted_promotion_review",
        "maintain_plan",
    }:
        raise ValueError("Planning data contains an undocumented action code")

    if len(data["demand_series"]) != 150:
        raise ValueError("Demand series must contain five series for 30 dates")
    if len(data["revenue_series"]) != 120:
        raise ValueError("Revenue series must contain four series for 30 dates")
    if len(data["product"]) != 120:
        raise ValueError("Product forecast must contain four products for 30 dates")


# Purpose: Write small, reviewable snapshots used by the Tableau workbook.
# Used by: main and package_workbook.
def export_csv_snapshots(
    data: dict[str, pd.DataFrame],
    output_directory: Path,
) -> dict[str, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, frame in data.items():
        path = output_directory / f"{name}.csv"
        frame.to_csv(path, index=False, float_format="%.6f")
        paths[name] = path
    return paths


# Purpose: Convert pandas types to Tableau text-connection metadata.
# Used by: add_data_source and add_dependency_fields.
def tableau_datatype(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series.dtype):
        return "boolean"
    if pd.api.types.is_integer_dtype(series.dtype):
        return "integer"
    if pd.api.types.is_numeric_dtype(series.dtype):
        return "real"
    return "string"


# Purpose: Map Tableau data types to text-connection remote type identifiers.
# Used by: add_data_source.
def remote_type(datatype: str) -> str:
    return {
        "boolean": "11",
        "integer": "20",
        "real": "5",
        "string": "129",
    }[datatype]


# Purpose: Produce readable field captions from SQL-safe column names.
# Used by: add_data_source and add_dependency_fields.
def field_caption(name: str) -> str:
    business_captions = {
        "short_date": "Date",
        "forecast_date": "Forecast Date",
        "tickets": "Tickets",
        "net_revenue_dollars": "Net Revenue ($)",
        "series_name": "Measure",
        "peak_capacity_utilization": "Peak Capacity Used",
        "decision_label": "Recommended Action",
        "forecast_range": "Forecast Range",
        "demand_mae_tickets": "Typical Daily Ticket Error (MAE)",
        "revenue_wape_ratio": "Revenue Forecast Error (WAPE)",
        "estimator_label": "Comparison",
        "estimate_pct": "Revenue Difference (%)",
        "estimate_ratio": "Revenue Difference (%)",
        "product_name": "Ticket Type",
        "predicted_product_net_revenue": "30-Day Net Revenue ($)",
    }
    return business_captions.get(name, name.replace("_", " ").title())


# Purpose: Return Tableau role and type metadata for a source field.
# Used by: add_data_source and add_dependency_fields.
def field_role(datatype: str) -> tuple[str, str]:
    if datatype in {"integer", "real"}:
        return "measure", "quantitative"
    return "dimension", "nominal"


# Purpose: Apply business-aware display formats without confusing ratios and
# percentage-point metrics.
# Used by: add_data_source and add_dependency_fields.
def default_format(name: str) -> str | None:
    currency_fields = {
        "predicted_net_revenue",
        "revenue_lower_bound",
        "revenue_upper_bound",
        "revenue_target",
        "forecast_revenue_variance",
        "predicted_net_revenue_per_ticket",
        "selected_revenue_mae",
        "estimated_incremental_net_revenue",
        "marketing_spend",
        "predicted_product_net_revenue",
        "predicted_product_net_yield",
        "base_price",
        "net_revenue_dollars",
    }
    ratio_fields = {
        "forecast_capacity_utilization",
        "upper_capacity_utilization",
        "forecast_demand_variance_pct",
        "forecast_revenue_variance_pct",
        "lower_revenue_variance_pct",
        "upper_revenue_variance_pct",
        "predicted_product_share",
        "peak_capacity_utilization",
        "estimate_ratio",
        "revenue_wape_ratio",
    }
    percentage_point_fields = {
        "selected_demand_mape_pct",
        "selected_demand_coverage_pct",
        "selected_revenue_wape_pct",
        "selected_revenue_coverage_pct",
        "estimate_pct",
        "percentage_error",
        "interval_coverage_pct",
    }
    if name in currency_fields:
        return "$#,##0.00;($#,##0.00)"
    if name in ratio_fields:
        return "p0.0%"
    if name in percentage_point_fields:
        return "n0.0"
    if name in {"selected_demand_mae", "demand_mae_tickets"}:
        return "n0.0"
    if name == "associated_return_after_spend":
        return "n0.00"
    return None


# Purpose: Create the workbook root and shared visual preferences.
# Used by: build_workbook_xml.
def create_workbook_root() -> ET.Element:
    root = ET.Element(
        "workbook",
        {
            "original-version": TABLEAU_VERSION,
            "source-build": TABLEAU_BUILD,
            "source-platform": "mac",
            "version": TABLEAU_VERSION,
            "xmlns:user": "http://www.tableausoftware.com/xml/user",
        },
    )
    manifest = ET.SubElement(root, "document-format-change-manifest")
    for feature in (
        "AccessibleZoneTabOrder",
        "AnimationOnByDefault",
        "MarkAnimation",
        "ObjectModelEncapsulateLegacy",
        "ObjectModelTableType",
        "SchemaViewerObjectModel",
        "SetMembershipControl",
        "SheetIdentifierTracking",
        "WindowsPersistSimpleIdentifiers",
        "WorksheetBackgroundTransparency",
        "ZoneBackgroundTransparency",
    ):
        ET.SubElement(manifest, feature)
    preferences = ET.SubElement(root, "preferences")
    palette = ET.SubElement(
        preferences,
        "color-palette",
        {"custom": "true", "name": "Orlando Planning", "type": "regular"},
    )
    for color in (NAVY, TEAL, ORANGE, RED, "#6C7A89", "#A7D8D8"):
        ET.SubElement(palette, "color").text = color
    return root


# Purpose: Add one complete CSV-backed data source to the workbook XML.
# Used by: build_workbook_xml for every exported dataset.
def add_data_source(
    datasources: ET.Element,
    spec: DataSourceSpec,
    data_directory: str,
) -> None:
    source = ET.SubElement(
        datasources,
        "datasource",
        {
            "caption": spec.caption,
            "inline": "true",
            "name": spec.key,
            "version": TABLEAU_VERSION,
        },
    )
    connection = ET.SubElement(source, "connection", {"class": "federated"})
    named_connections = ET.SubElement(connection, "named-connections")
    connection_name = f"textscan.{spec.key.split('.')[-1]}"
    named = ET.SubElement(
        named_connections,
        "named-connection",
        {"caption": spec.caption, "name": connection_name},
    )
    ET.SubElement(
        named,
        "connection",
        {
            "class": "textscan",
            "directory": data_directory,
            "filename": spec.filename,
            "password": "",
            "server": "",
        },
    )
    table_name = f"{Path(spec.filename).stem}.csv"
    object_id = f"{Path(spec.filename).stem.upper()}_TABLE"
    relation = ET.SubElement(
        connection,
        "relation",
        {
            "connection": connection_name,
            "name": spec.filename,
            "table": f"[{Path(spec.filename).stem}#csv]",
            "type": "table",
        },
    )
    columns = ET.SubElement(
        relation,
        "columns",
        {
            "character-set": "UTF-8",
            "header": "yes",
            "locale": "en_US",
            "separator": ",",
        },
    )
    for ordinal, column in enumerate(spec.frame.columns):
        datatype = tableau_datatype(spec.frame[column])
        ET.SubElement(
            columns,
            "column",
            {"datatype": datatype, "name": column, "ordinal": str(ordinal)},
        )

    metadata_records = ET.SubElement(connection, "metadata-records")
    for ordinal, column in enumerate(spec.frame.columns):
        datatype = tableau_datatype(spec.frame[column])
        role, _ = field_role(datatype)
        record = ET.SubElement(metadata_records, "metadata-record", {"class": "column"})
        ET.SubElement(record, "remote-name").text = column
        ET.SubElement(record, "remote-type").text = remote_type(datatype)
        ET.SubElement(record, "local-name").text = f"[{column}]"
        ET.SubElement(record, "parent-name").text = f"[{spec.filename}]"
        ET.SubElement(record, "remote-alias").text = column
        ET.SubElement(record, "ordinal").text = str(ordinal)
        ET.SubElement(record, "local-type").text = datatype
        ET.SubElement(record, "aggregation").text = "Sum" if role == "measure" else "Count"
        ET.SubElement(record, "contains-null").text = "true"
        ET.SubElement(record, "object-id").text = f"[{object_id}]"

    ET.SubElement(source, "aliases", {"enabled": "yes"})
    ET.SubElement(
        source,
        "column",
        {
            "caption": table_name,
            "datatype": "table",
            "name": f"[__tableau_internal_object_id__].[{object_id}]",
            "role": "measure",
            "type": "quantitative",
        },
    )
    for column in spec.frame.columns:
        datatype = tableau_datatype(spec.frame[column])
        role, field_type = field_role(datatype)
        attributes = {
            "caption": field_caption(column),
            "datatype": datatype,
            "name": f"[{column}]",
            "role": role,
            "type": field_type,
        }
        display_format = default_format(column)
        if display_format:
            attributes["default-format"] = display_format
        ET.SubElement(source, "column", attributes)
    ET.SubElement(
        source,
        "layout",
        {
            "dim-ordering": "alphabetic",
            "measure-ordering": "alphabetic",
            "show-structure": "true",
        },
    )
    object_graph = ET.SubElement(source, "object-graph")
    objects = ET.SubElement(object_graph, "objects")
    data_object = ET.SubElement(
        objects,
        "object",
        {"caption": spec.caption, "id": object_id},
    )
    properties = ET.SubElement(data_object, "properties", {"context": ""})
    properties.append(ET.fromstring(ET.tostring(relation, encoding="unicode")))


# Purpose: Build the Tableau field-instance name used in shelves and encodings.
# Used by: add_dependency_fields, add_filter, and worksheet constructors.
def instance_name(field: FieldSpec) -> str:
    operation_codes = {
        "none": ("none", "nk"),
        "sum": ("sum", "qk"),
        "min": ("min", "qk"),
        "max": ("max", "qk"),
        "avg": ("avg", "qk"),
        "count": ("cnt", "qk"),
    }
    prefix, suffix = operation_codes[field.operation]
    return f"[{prefix}:{field.name}:{suffix}]"


# Purpose: Add source columns and reusable field instances to a worksheet.
# Used by: add_kpi_sheet, add_line_sheet, add_bar_sheet, and add_text_sheet.
def add_dependency_fields(
    dependency: ET.Element,
    source: DataSourceSpec,
    fields: list[FieldSpec],
) -> None:
    added_columns: set[str] = set()
    for field in fields:
        if field.name not in source.frame.columns:
            raise KeyError(f"{field.name!r} is not present in {source.caption}")
        datatype = tableau_datatype(source.frame[field.name])
        role, field_type = field_role(datatype)
        if field.name not in added_columns:
            attributes = {
                "caption": field_caption(field.name),
                "datatype": datatype,
                "name": f"[{field.name}]",
                "role": role,
                "type": field_type,
            }
            display_format = default_format(field.name)
            if display_format:
                attributes["default-format"] = display_format
            ET.SubElement(dependency, "column", attributes)
            added_columns.add(field.name)

        operation = field.operation
        instance_type = "nominal" if operation == "none" else "quantitative"
        derivation = {
            "none": "None",
            "sum": "Sum",
            "min": "Min",
            "max": "Max",
            "avg": "Avg",
            "count": "Count",
        }[operation]
        ET.SubElement(
            dependency,
            "column-instance",
            {
                "column": f"[{field.name}]",
                "derivation": derivation,
                "name": instance_name(field),
                "pivot": "key",
                "type": instance_type,
            },
        )


# Purpose: Add a fixed member filter to a worksheet view.
# Used by: worksheet constructors for domain-specific dashboard panels.
def add_filter(
    view: ET.Element,
    source_key: str,
    field_name: str,
    member: Any,
) -> None:
    reference = f"[{source_key}].{instance_name(FieldSpec(field_name))}"
    filter_element = ET.SubElement(
        view,
        "filter",
        {"class": "categorical", "column": reference},
    )
    ET.SubElement(
        filter_element,
        "groupfilter",
        {
            "function": "member",
            "level": instance_name(FieldSpec(field_name)),
            "member": f'"{member}"',
            "user:ui-domain": "relevant",
            "user:ui-enumeration": "inclusive",
        },
    )


# Purpose: Create shared worksheet view, dependency, and table containers.
# Used by: all worksheet constructors.
def create_sheet_base(
    worksheets: ET.Element,
    name: str,
    title: str,
    source: DataSourceSpec,
    fields: list[FieldSpec],
    filters: dict[str, Any] | None = None,
) -> tuple[ET.Element, ET.Element, ET.Element]:
    worksheet = ET.SubElement(worksheets, "worksheet", {"name": name})
    layout = ET.SubElement(worksheet, "layout-options")
    title_element = ET.SubElement(layout, "title")
    formatted = ET.SubElement(title_element, "formatted-text")
    ET.SubElement(
        formatted,
        "run",
        {"fontcolor": NAVY, "fontname": "Tableau Medium"},
    ).text = title
    table = ET.SubElement(worksheet, "table")
    view = ET.SubElement(table, "view")
    view_sources = ET.SubElement(view, "datasources")
    ET.SubElement(
        view_sources,
        "datasource",
        {"caption": source.caption, "name": source.key},
    )
    dependency = ET.SubElement(
        view,
        "datasource-dependencies",
        {"datasource": source.key},
    )
    add_dependency_fields(dependency, source, fields)
    for filter_field, member in (filters or {}).items():
        add_filter(view, source.key, filter_field, member)
    ET.SubElement(view, "aggregation", {"value": "true"})
    ET.SubElement(table, "style")
    return worksheet, table, view


# Purpose: Add a large-number KPI worksheet.
# Used by: build_workbook_xml for dashboard headline metrics.
def add_kpi_sheet(
    worksheets: ET.Element,
    source: DataSourceSpec,
    name: str,
    title: str,
    measure: FieldSpec,
    filters: dict[str, Any] | None = None,
    prefix: str = "",
    suffix: str = "",
) -> None:
    filter_fields = [FieldSpec(field) for field in (filters or {})]
    worksheet, table, _ = create_sheet_base(
        worksheets,
        name,
        title,
        source,
        [measure, *filter_fields],
        filters,
    )
    panes = ET.SubElement(table, "panes")
    pane = ET.SubElement(panes, "pane", {"selection-relaxation-option": "selection-relaxation-allow"})
    pane_view = ET.SubElement(pane, "view")
    ET.SubElement(pane_view, "breakdown", {"value": "auto"})
    ET.SubElement(pane, "mark", {"class": "Automatic"})
    encodings = ET.SubElement(pane, "encodings")
    reference = f"[{source.key}].{instance_name(measure)}"
    ET.SubElement(encodings, "text", {"column": reference})
    label = ET.SubElement(pane, "customized-label")
    formatted = ET.SubElement(label, "formatted-text")
    run = ET.SubElement(
        formatted,
        "run",
        {"fontcolor": NAVY, "fontname": "Tableau Light", "fontsize": "24"},
    )
    run.text = f"{prefix}<{reference}>{suffix}"
    style = ET.SubElement(pane, "style")
    cell_rule = ET.SubElement(style, "style-rule", {"element": "cell"})
    ET.SubElement(cell_rule, "format", {"attr": "text-align", "value": "center"})
    ET.SubElement(cell_rule, "format", {"attr": "vertical-align", "value": "center"})
    mark_rule = ET.SubElement(style, "style-rule", {"element": "mark"})
    ET.SubElement(mark_rule, "format", {"attr": "mark-labels-show", "value": "true"})
    ET.SubElement(table, "rows")
    ET.SubElement(table, "cols")
    ET.SubElement(worksheet, "simple-id", {"uuid": f"{{{str(uuid.uuid4()).upper()}}}"})


# Purpose: Add a time-series worksheet with an optional color series.
# Used by: build_workbook_xml for demand, revenue, and utilization trends.
def add_line_sheet(
    worksheets: ET.Element,
    source: DataSourceSpec,
    name: str,
    title: str,
    date_field: str,
    measure_field: str,
    color_field: str | None = None,
    filters: dict[str, Any] | None = None,
    x_axis_title: str | None = None,
    y_axis_title: str | None = None,
) -> None:
    date = FieldSpec(date_field)
    measure = FieldSpec(measure_field, "sum")
    fields = [date, measure]
    if color_field:
        fields.append(FieldSpec(color_field))
    fields.extend(FieldSpec(field) for field in (filters or {}))
    worksheet, table, _ = create_sheet_base(
        worksheets, name, title, source, fields, filters
    )
    table_style = table.find("style")
    if table_style is None:
        raise RuntimeError("Worksheet table style was not created")
    if x_axis_title or y_axis_title:
        axis_rule = ET.SubElement(table_style, "style-rule", {"element": "axis"})
        if x_axis_title:
            ET.SubElement(
                axis_rule,
                "format",
                {
                    "attr": "title",
                    "class": "0",
                    "field": f"[{source.key}].{instance_name(date)}",
                    "scope": "cols",
                    "value": x_axis_title,
                },
            )
        if y_axis_title:
            ET.SubElement(
                axis_rule,
                "format",
                {
                    "attr": "title",
                    "class": "0",
                    "field": f"[{source.key}].{instance_name(measure)}",
                    "scope": "rows",
                    "value": y_axis_title,
                },
            )
    panes = ET.SubElement(table, "panes")
    pane = ET.SubElement(panes, "pane", {"selection-relaxation-option": "selection-relaxation-allow"})
    pane_view = ET.SubElement(pane, "view")
    ET.SubElement(pane_view, "breakdown", {"value": "auto"})
    ET.SubElement(pane, "mark", {"class": "Line"})
    encodings = ET.SubElement(pane, "encodings")
    if color_field:
        ET.SubElement(
            encodings,
            "color",
            {"column": f"[{source.key}].{instance_name(FieldSpec(color_field))}"},
        )
    style = ET.SubElement(pane, "style")
    mark_rule = ET.SubElement(style, "style-rule", {"element": "mark"})
    ET.SubElement(mark_rule, "format", {"attr": "mark-color", "value": TEAL})
    ET.SubElement(table, "rows").text = f"[{source.key}].{instance_name(measure)}"
    ET.SubElement(table, "cols").text = f"[{source.key}].{instance_name(date)}"
    ET.SubElement(worksheet, "simple-id", {"uuid": f"{{{str(uuid.uuid4()).upper()}}}"})


# Purpose: Add a categorical bar worksheet with labels.
# Used by: build_workbook_xml for actions, accuracy, campaign, and products.
def add_bar_sheet(
    worksheets: ET.Element,
    source: DataSourceSpec,
    name: str,
    title: str,
    category_field: str,
    measure: FieldSpec,
    filters: dict[str, Any] | None = None,
    color_field: str | None = None,
    axis_title: str | None = None,
    category_width: int | None = None,
) -> None:
    category = FieldSpec(category_field)
    fields = [category, measure]
    if color_field:
        fields.append(FieldSpec(color_field))
    fields.extend(FieldSpec(field) for field in (filters or {}))
    worksheet, table, _ = create_sheet_base(
        worksheets, name, title, source, fields, filters
    )
    table_style = table.find("style")
    if table_style is None:
        raise RuntimeError("Worksheet table style was not created")
    if axis_title:
        axis_rule = ET.SubElement(table_style, "style-rule", {"element": "axis"})
        ET.SubElement(
            axis_rule,
            "format",
            {
                "attr": "title",
                "class": "0",
                "field": f"[{source.key}].{instance_name(measure)}",
                "scope": "cols",
                "value": axis_title,
            },
        )
    if category_width:
        header_rule = ET.SubElement(table_style, "style-rule", {"element": "header"})
        ET.SubElement(
            header_rule,
            "format",
            {
                "attr": "width",
                "field": f"[{source.key}].{instance_name(category)}",
                "value": str(category_width),
            },
        )
    panes = ET.SubElement(table, "panes")
    pane = ET.SubElement(panes, "pane", {"selection-relaxation-option": "selection-relaxation-allow"})
    pane_view = ET.SubElement(pane, "view")
    ET.SubElement(pane_view, "breakdown", {"value": "auto"})
    ET.SubElement(pane, "mark", {"class": "Bar"})
    encodings = ET.SubElement(pane, "encodings")
    measure_reference = f"[{source.key}].{instance_name(measure)}"
    ET.SubElement(encodings, "text", {"column": measure_reference})
    if color_field:
        ET.SubElement(
            encodings,
            "color",
            {"column": f"[{source.key}].{instance_name(FieldSpec(color_field))}"},
        )
    style = ET.SubElement(pane, "style")
    mark_rule = ET.SubElement(style, "style-rule", {"element": "mark"})
    ET.SubElement(mark_rule, "format", {"attr": "mark-labels-show", "value": "true"})
    ET.SubElement(mark_rule, "format", {"attr": "mark-color", "value": TEAL})
    ET.SubElement(table, "rows").text = f"[{source.key}].{instance_name(category)}"
    ET.SubElement(table, "cols").text = measure_reference
    ET.SubElement(worksheet, "simple-id", {"uuid": f"{{{str(uuid.uuid4()).upper()}}}"})


# Purpose: Add a ranked text worksheet for operational watchlists.
# Used by: build_workbook_xml on executive and demand dashboards.
def add_text_sheet(
    worksheets: ET.Element,
    source: DataSourceSpec,
    name: str,
    title: str,
    row_field: str,
    label_field: str,
    filters: dict[str, Any] | None = None,
) -> None:
    row = FieldSpec(row_field)
    label = FieldSpec(label_field)
    worksheet, table, _ = create_sheet_base(
        worksheets,
        name,
        title,
        source,
        [row, label, *(FieldSpec(field) for field in (filters or {}))],
        filters,
    )
    panes = ET.SubElement(table, "panes")
    pane = ET.SubElement(panes, "pane", {"selection-relaxation-option": "selection-relaxation-allow"})
    pane_view = ET.SubElement(pane, "view")
    ET.SubElement(pane_view, "breakdown", {"value": "auto"})
    ET.SubElement(pane, "mark", {"class": "Automatic"})
    encodings = ET.SubElement(pane, "encodings")
    ET.SubElement(
        encodings,
        "text",
        {"column": f"[{source.key}].{instance_name(label)}"},
    )
    style = ET.SubElement(pane, "style")
    mark_rule = ET.SubElement(style, "style-rule", {"element": "mark"})
    ET.SubElement(mark_rule, "format", {"attr": "mark-labels-show", "value": "true"})
    ET.SubElement(table, "rows").text = f"[{source.key}].{instance_name(row)}"
    ET.SubElement(table, "cols")
    ET.SubElement(worksheet, "simple-id", {"uuid": f"{{{str(uuid.uuid4()).upper()}}}"})


# Purpose: Add consistent borders, padding, and background to a dashboard zone.
# Used by: add_dashboard.
def add_zone_style(zone: ET.Element, background: str = "#FFFFFF") -> None:
    style = ET.SubElement(zone, "zone-style")
    ET.SubElement(style, "format", {"attr": "border-color", "value": "#D9E2E8"})
    ET.SubElement(style, "format", {"attr": "border-style", "value": "solid"})
    ET.SubElement(style, "format", {"attr": "border-width", "value": "1"})
    ET.SubElement(style, "format", {"attr": "margin", "value": "6"})
    ET.SubElement(style, "format", {"attr": "background-color", "value": background})


# Purpose: Add one fixed-size decision dashboard and position its sheet zones.
# Used by: build_workbook_xml for the three portfolio pages.
def add_dashboard(
    dashboards: ET.Element,
    name: str,
    title: str,
    subtitle: str,
    zones: list[tuple[str, int, int, int, int]],
    legends: list[tuple[str, DataSourceSpec, str, int, int, int, int]] | None = None,
) -> None:
    dashboard = ET.SubElement(
        dashboards,
        "dashboard",
        {"enable-sort-zone-taborder": "true", "name": name},
    )
    layout = ET.SubElement(dashboard, "layout-options")
    title_element = ET.SubElement(layout, "title")
    formatted = ET.SubElement(title_element, "formatted-text")
    ET.SubElement(
        formatted,
        "run",
        {"bold": "true", "fontcolor": NAVY, "fontname": "Tableau Medium", "fontsize": "22"},
    ).text = title
    ET.SubElement(
        formatted,
        "run",
        {"fontcolor": "#506475", "fontname": "Tableau Light", "fontsize": "11"},
    ).text = f"  |  {subtitle}"
    ET.SubElement(
        dashboard,
        "size",
        {
            "maxheight": "800",
            "maxwidth": "1200",
            "minheight": "800",
            "minwidth": "1200",
            "sizing-mode": "fixed",
        },
    )
    dashboard_zones = ET.SubElement(dashboard, "zones")
    root_zone = ET.SubElement(
        dashboard_zones,
        "zone",
        {"h": "100000", "id": "1", "type-v2": "layout-basic", "w": "100000", "x": "0", "y": "0"},
    )
    title_zone = ET.SubElement(
        root_zone,
        "zone",
        {"h": "10000", "id": "2", "type-v2": "title", "w": "100000", "x": "0", "y": "0"},
    )
    add_zone_style(title_zone, LIGHT_BLUE)
    for zone_id, (sheet, x, y, width, height) in enumerate(zones, start=3):
        zone = ET.SubElement(
            root_zone,
            "zone",
            {
                "h": str(height),
                "id": str(zone_id),
                "name": sheet,
                "w": str(width),
                "x": str(x),
                "y": str(y),
            },
        )
        ET.SubElement(zone, "layout-cache", {"type-h": "scalable", "type-w": "scalable"})
        add_zone_style(zone)
    next_zone_id = 3 + len(zones)
    for offset, (
        sheet,
        source,
        field_name,
        x,
        y,
        width,
        height,
    ) in enumerate(legends or []):
        legend = ET.SubElement(
            root_zone,
            "zone",
            {
                "h": str(height),
                "id": str(next_zone_id + offset),
                "name": sheet,
                "pane-specification-id": "0",
                "param": f"[{source.key}].{instance_name(FieldSpec(field_name))}",
                "type-v2": "color",
                "w": str(width),
                "x": str(x),
                "y": str(y),
            },
        )
        legend_style = ET.SubElement(legend, "zone-style")
        ET.SubElement(
            legend_style,
            "format",
            {"attr": "border-style", "value": "none"},
        )
        ET.SubElement(
            legend_style,
            "format",
            {"attr": "background-color", "value": "#FFFFFF"},
        )
        ET.SubElement(
            legend_style,
            "format",
            {"attr": "margin", "value": "3"},
        )
    add_zone_style(root_zone, LIGHT_GRAY)
    ET.SubElement(dashboard, "simple-id", {"uuid": f"{{{str(uuid.uuid4()).upper()}}}"})


# Purpose: Add Tableau window metadata so the workbook opens on a dashboard.
# Used by: build_workbook_xml after worksheets and dashboards are defined.
def add_windows(
    root: ET.Element,
    worksheet_names: list[str],
    dashboard_sheets: dict[str, list[str]],
) -> None:
    windows = ET.SubElement(root, "windows", {"show-side-pane": "false", "source-height": "135"})
    for sheet in worksheet_names:
        window = ET.SubElement(windows, "window", {"class": "worksheet", "name": sheet})
        cards = ET.SubElement(window, "cards")
        left = ET.SubElement(cards, "edge", {"name": "left"})
        strip = ET.SubElement(left, "strip", {"size": "220"})
        for card_type in ("pages", "filters", "marks"):
            ET.SubElement(strip, "card", {"type": card_type})
        top = ET.SubElement(cards, "edge", {"name": "top"})
        for card_type in ("columns", "rows", "title"):
            top_strip = ET.SubElement(top, "strip", {"size": "2147483647"})
            ET.SubElement(top_strip, "card", {"type": card_type})
        ET.SubElement(window, "simple-id", {"uuid": f"{{{str(uuid.uuid4()).upper()}}}"})

    for index, (dashboard_name, sheets) in enumerate(dashboard_sheets.items()):
        attributes = {"class": "dashboard", "name": dashboard_name}
        if index == 0:
            attributes["maximized"] = "true"
        window = ET.SubElement(windows, "window", attributes)
        viewpoints = ET.SubElement(window, "viewpoints")
        for sheet in sheets:
            viewpoint = ET.SubElement(viewpoints, "viewpoint", {"name": sheet})
            ET.SubElement(viewpoint, "zoom", {"type": "entire-view"})
        ET.SubElement(window, "active", {"id": "-1"})
        ET.SubElement(window, "simple-id", {"uuid": f"{{{str(uuid.uuid4()).upper()}}}"})


# Purpose: Assemble a two-page decision story into valid TWB XML.
# Used by: main for local and packaged workbook variants.
def build_workbook_xml(
    specs: dict[str, DataSourceSpec],
    data_directory: str,
) -> ET.ElementTree:
    root = create_workbook_root()
    datasources = ET.SubElement(root, "datasources")
    for spec in specs.values():
        add_data_source(datasources, spec, data_directory)

    worksheets = ET.SubElement(root, "worksheets")
    planning = specs["planning"]
    demand_series = specs["demand_series"]
    revenue_series = specs["revenue_series"]
    accuracy = specs["accuracy"]
    campaign = specs["campaign"]
    product = specs["product"]

    add_kpi_sheet(
        worksheets,
        planning,
        "KPI - Forecast Demand",
        "Expected 30-Day Demand",
        FieldSpec("predicted_demand", "sum"),
        suffix=" tickets",
    )
    add_kpi_sheet(
        worksheets,
        planning,
        "KPI - Forecast Revenue",
        "Expected 30-Day Net Revenue",
        FieldSpec("predicted_net_revenue", "sum"),
        prefix="$",
    )
    add_kpi_sheet(
        worksheets,
        planning,
        "KPI - Peak Capacity Use",
        "Peak Capacity Used",
        FieldSpec("peak_capacity_utilization", "max"),
    )
    add_kpi_sheet(
        worksheets,
        planning,
        "KPI - Review Dates",
        "Dates Needing Review",
        FieldSpec("target_date", "count"),
        filters={"decision_scope": "Review"},
        suffix=" days",
    )
    add_line_sheet(
        worksheets,
        demand_series,
        "Daily Demand Plan",
        "Daily Ticket Demand versus Plan and Capacity",
        "forecast_date",
        "tickets",
        "series_name",
        filters={"series_scope": "Planning"},
        x_axis_title="Forecast Date",
        y_axis_title="Tickets",
    )
    add_text_sheet(
        worksheets,
        planning,
        "Decision Dates",
        "What to Do Differently",
        "short_date",
        "decision_label",
        filters={"decision_scope": "Review"},
    )
    add_line_sheet(
        worksheets,
        revenue_series,
        "Daily Revenue Plan",
        "Daily Net Revenue Forecast, Range, and Plan",
        "forecast_date",
        "net_revenue_dollars",
        "series_name",
        x_axis_title="Forecast Date",
        y_axis_title="Net Revenue ($)",
    )

    add_kpi_sheet(
        worksheets,
        campaign,
        "KPI - Campaign Difference",
        "Campaign-Day Revenue Difference",
        FieldSpec("estimate_ratio", "min"),
        filters={
            "outcome_name": "net_revenue",
            "estimator_name": "matched_block_bootstrap",
            "period_name": "campaign_window",
        },
    )
    add_kpi_sheet(
        worksheets,
        campaign,
        "KPI - Marketing Net Gain",
        "Net Gain per $1 Marketing Spend",
        FieldSpec("associated_return_after_spend", "min"),
        prefix="$",
    )
    add_kpi_sheet(
        worksheets,
        planning,
        "KPI - Demand Error",
        "Typical Daily Demand Error (MAE)",
        FieldSpec("selected_demand_mae", "min"),
        suffix=" tickets",
    )
    add_kpi_sheet(
        worksheets,
        planning,
        "KPI - Revenue Error",
        "Revenue Forecast Error (WAPE)",
        FieldSpec("selected_revenue_wape_pct", "min"),
        suffix="%",
    )
    add_bar_sheet(
        worksheets,
        product,
        "Product Revenue Mix",
        "Which Ticket Types Generate the 30-Day Revenue Forecast?",
        "product_name",
        FieldSpec("predicted_product_net_revenue", "sum"),
        axis_title="30-Day Net Revenue ($)",
        category_width=170,
    )
    add_bar_sheet(
        worksheets,
        campaign,
        "Campaign Comparison",
        "Campaign Days Earned More than Comparable Days",
        "estimator_label",
        FieldSpec("estimate_ratio", "min"),
        filters={"outcome_name": "net_revenue", "period_name": "campaign_window"},
        axis_title="Revenue Difference",
        category_width=210,
    )
    add_bar_sheet(
        worksheets,
        accuracy,
        "Demand Error by Range",
        "Typical Daily Ticket Error by Forecast Range (MAE)",
        "forecast_range",
        FieldSpec("demand_mae_tickets", "min"),
        filters={"forecast_domain": "Demand"},
        axis_title="Typical Daily Error (Tickets)",
        category_width=150,
    )
    add_bar_sheet(
        worksheets,
        accuracy,
        "Revenue Error by Range",
        "Revenue Forecast Error by Forecast Range (WAPE)",
        "forecast_range",
        FieldSpec("revenue_wape_ratio", "min"),
        filters={"forecast_domain": "Revenue"},
        axis_title="Revenue Error (WAPE)",
        category_width=150,
    )

    dashboards = ET.SubElement(root, "dashboards")
    outlook_sheets = [
        "KPI - Forecast Demand",
        "KPI - Forecast Revenue",
        "KPI - Peak Capacity Use",
        "KPI - Review Dates",
        "Daily Demand Plan",
        "Decision Dates",
        "Daily Revenue Plan",
    ]
    review_days = int((planning.frame["decision_scope"] == "Review").sum())
    add_dashboard(
        dashboards,
        "30-Day Outlook & Actions",
        f"30-Day Plan: Capacity Is Sufficient; {review_days} Dates Need Review",
        "Use this page to plan daily staffing, protect price on strong days, and monitor revenue shortfalls.",
        [
            (outlook_sheets[0], 0, 10000, 25000, 15000),
            (outlook_sheets[1], 25000, 10000, 25000, 15000),
            (outlook_sheets[2], 50000, 10000, 25000, 15000),
            (outlook_sheets[3], 75000, 10000, 25000, 15000),
            (outlook_sheets[4], 0, 25000, 65000, 38000),
            (outlook_sheets[5], 65000, 25000, 35000, 38000),
            (outlook_sheets[6], 0, 63000, 100000, 37000),
        ],
        legends=[
            (
                "Daily Demand Plan",
                demand_series,
                "series_name",
                23000,
                31500,
                41000,
                5000,
            ),
            (
                "Daily Revenue Plan",
                revenue_series,
                "series_name",
                34000,
                69500,
                65000,
                5000,
            ),
        ],
    )

    drivers_sheets = [
        "KPI - Campaign Difference",
        "KPI - Marketing Net Gain",
        "KPI - Demand Error",
        "KPI - Revenue Error",
        "Product Revenue Mix",
        "Campaign Comparison",
        "Demand Error by Range",
        "Revenue Error by Range",
    ]
    add_dashboard(
        dashboards,
        "Revenue Drivers & Confidence",
        "Revenue Drivers & Forecast Confidence",
        "Use this page to see which products generate revenue, how campaign periods differ, and how error changes with lead time; campaign results are observational.",
        [
            (drivers_sheets[0], 0, 10000, 25000, 15000),
            (drivers_sheets[1], 25000, 10000, 25000, 15000),
            (drivers_sheets[2], 50000, 10000, 25000, 15000),
            (drivers_sheets[3], 75000, 10000, 25000, 15000),
            (drivers_sheets[4], 0, 25000, 50000, 40000),
            (drivers_sheets[5], 50000, 25000, 50000, 40000),
            (drivers_sheets[6], 0, 65000, 50000, 35000),
            (drivers_sheets[7], 50000, 65000, 50000, 35000),
        ],
    )

    dashboard_sheets = {
        "30-Day Outlook & Actions": outlook_sheets,
        "Revenue Drivers & Confidence": drivers_sheets,
    }
    worksheet_names = [worksheet.attrib["name"] for worksheet in worksheets]
    add_windows(root, worksheet_names, dashboard_sheets)
    ET.indent(root, space="  ")
    return ET.ElementTree(root)


# Purpose: Write the editable local TWB with absolute snapshot paths.
# Used by: main before packaging and visual verification.
def write_workbook(tree: ET.ElementTree, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)


# Purpose: Package the workbook and CSV snapshots into a portable TWBX.
# Used by: main for the recruiter-facing Tableau deliverable.
def package_workbook(
    packaged_tree: ET.ElementTree,
    twbx_path: Path,
    csv_paths: dict[str, Path],
) -> None:
    packaged_twb_path = twbx_path.with_suffix(".packaged.twb")
    write_workbook(packaged_tree, packaged_twb_path)
    try:
        with zipfile.ZipFile(twbx_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(packaged_twb_path, arcname=f"{WORKBOOK_NAME}.twb")
            for path in csv_paths.values():
                archive.write(path, arcname=f"Data/{path.name}")
    finally:
        packaged_twb_path.unlink(missing_ok=True)


# Purpose: Build, validate, export, and package every Tableau deliverable.
# Used by: module entry point.
def main() -> None:
    args = parse_args()
    project_root = args.project_root.resolve()
    tableau_directory = project_root / "tableau"
    data_directory = tableau_directory / "data"
    engine = create_engine(args.database_url)
    data = load_dashboard_data(engine)
    validate_dashboard_data(data)
    csv_paths = export_csv_snapshots(data, data_directory)
    specs = {
        name: DataSourceSpec(
            key=f"federated.orlando_{name}",
            caption=field_caption(name),
            filename=path.name,
            frame=data[name],
        )
        for name, path in csv_paths.items()
    }

    # Keep the unpackaged workbook portable and avoid embedding a developer's
    # absolute filesystem path in the public repository.
    local_tree = build_workbook_xml(specs, "data")
    twb_path = tableau_directory / f"{WORKBOOK_NAME}.twb"
    write_workbook(local_tree, twb_path)

    packaged_tree = build_workbook_xml(specs, "Data")
    twbx_path = tableau_directory / f"{WORKBOOK_NAME}.twbx"
    package_workbook(packaged_tree, twbx_path, csv_paths)

    print("Tableau workbook build completed.")
    print(f"TWB:  {twb_path}")
    print(f"TWBX: {twbx_path}")
    print(f"Planning rows: {len(data['planning']):,}")
    print("Worksheets: 15")
    print("Dashboards: 2")


if __name__ == "__main__":
    main()
