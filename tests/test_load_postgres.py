from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from src.load_postgres import (
    LOAD_SPECS,
    DataLoadError,
    compare_reconciliation,
    strip_outer_transaction,
)


def _manifests() -> dict[str, dict[str, object]]:
    public_outputs: dict[str, object] = {}
    synthetic_outputs: dict[str, object] = {}
    row_counts = {
        "dim_date": 10,
        "dim_product": 4,
        "dim_channel": 5,
        "dim_campaign": 13,
        "fact_weather": 8,
        "fact_daily_plan": 9,
        "fact_campaign_daily": 20,
        "fact_ticket_sales": 100,
    }
    for spec in LOAD_SPECS:
        entry = {"path": spec.relative_path, "rows": row_counts[spec.table_name]}
        if spec.manifest_group == "public":
            public_outputs[spec.manifest_name] = entry
        else:
            synthetic_outputs[spec.manifest_name] = entry

    return {
        "public": {"outputs": public_outputs},
        "synthetic": {
            "outputs": synthetic_outputs,
            "validation": {
                "order_lines": 100,
                "units_sold": 300,
                "units_refunded": 12,
                "gross_revenue": Decimal("36000.00"),
                "discounts": Decimal("900.00"),
                "refunds": Decimal("1400.00"),
                "net_revenue": Decimal("33700.00"),
                "refund_rate": Decimal("0.040000"),
                "capacity_constrained_days": 2,
            },
        },
    }


def _database_summary() -> dict[str, object]:
    manifests = _manifests()
    counts = {
        spec.table_name: int(
            manifests[spec.manifest_group]["outputs"][spec.manifest_name]["rows"]
        )
        for spec in LOAD_SPECS
    }
    return {
        "row_counts": counts,
        "financials": {
            "order_lines": 100,
            "units_sold": 300,
            "units_refunded": 12,
            "gross_revenue": Decimal("36000.00"),
            "discounts": Decimal("900.00"),
            "refunds": Decimal("1400.00"),
            "net_revenue": Decimal("33700.00"),
            "refund_rate": Decimal("0.040000"),
            "capacity_constrained_days": 2,
        },
    }


def test_load_specs_define_unique_tables_and_columns() -> None:
    table_names = [spec.table_name for spec in LOAD_SPECS]

    assert len(table_names) == len(set(table_names))
    assert table_names[:4] == [
        "dim_date",
        "dim_product",
        "dim_channel",
        "dim_campaign",
    ]
    assert all(spec.columns for spec in LOAD_SPECS)
    assert all(len(spec.columns) == len(set(spec.columns)) for spec in LOAD_SPECS)


def test_strip_outer_transaction_preserves_validation_body() -> None:
    script = "-- validation\nBEGIN;\nSELECT 1;\nCOMMIT;\n"

    stripped = strip_outer_transaction(script)

    assert stripped == "-- validation\nSELECT 1;\n"


def test_strip_outer_transaction_rejects_unexpected_contract() -> None:
    with pytest.raises(DataLoadError, match="must contain one BEGIN"):
        strip_outer_transaction("SELECT 1;")


def test_reconciliation_accepts_matching_controls() -> None:
    compare_reconciliation(_database_summary(), _manifests())


def test_reconciliation_reports_row_and_financial_mismatches() -> None:
    summary = deepcopy(_database_summary())
    summary["row_counts"]["fact_ticket_sales"] = 99
    summary["financials"]["net_revenue"] = Decimal("33699.99")

    with pytest.raises(DataLoadError) as error:
        compare_reconciliation(summary, _manifests())

    message = str(error.value)
    assert "fact_ticket_sales row count" in message
    assert "net_revenue" in message
