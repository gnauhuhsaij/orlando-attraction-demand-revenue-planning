from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from src.forecast_revenue import (
    aggregate_daily_revenue,
    build_database_records,
    calculate_revenue_metrics,
    calibrate_production_revenue_intervals,
    calibrate_revenue_backtest_intervals,
    normalize_product_shares,
    validate_database_records,
)


# Purpose: Create two product rows for normalization and aggregation tests.
# Used by: revenue forecast unit tests.
def _product_predictions(production: bool = False) -> pd.DataFrame:
    actual_demand = [np.nan, np.nan] if production else [60.0, 40.0]
    actual_revenue = [np.nan, np.nan] if production else [7200.0, 3200.0]
    actual_share = [np.nan, np.nan] if production else [0.6, 0.4]
    actual_yield = [np.nan, np.nan] if production else [120.0, 80.0]
    return pd.DataFrame(
        {
            "forecast_created_date": pd.to_datetime(["2025-12-31", "2025-12-31"]),
            "training_end_date": pd.to_datetime(["2025-12-31", "2025-12-31"]),
            "target_date": pd.to_datetime(["2026-01-01", "2026-01-01"]),
            "horizon_days": [1, 1],
            "product_key": [1, 2],
            "product_code": ["ADULT", "CHILD"],
            "predicted_demand": [100.0, 100.0],
            "actual_demand": [
                np.nan if production else 100.0,
                np.nan if production else 100.0,
            ],
            "predicted_product_share": [0.6, 0.4],
            "predicted_product_demand": [60.0, 40.0],
            "predicted_product_net_yield": [121.0, 79.0],
            "predicted_product_net_revenue": [7260.0, 3160.0],
            "target_product_share": actual_share,
            "target_product_demand": actual_demand,
            "target_product_net_yield": actual_yield,
            "target_product_net_revenue": actual_revenue,
            "product_mix_model_name": ["hgb_product_mix"] * 2,
            "product_yield_model_name": ["ridge_product_yield"] * 2,
        }
    )


# Purpose: Confirm raw shares become a valid daily composition.
# Used by: the unit-test suite.
def test_normalize_product_shares_reconciles_to_total_demand() -> None:
    predictions = _product_predictions().drop(
        columns=["predicted_product_share", "predicted_product_demand"]
    )

    normalized = normalize_product_shares(predictions, np.array([2.0, -1.0]))

    assert normalized["predicted_product_share"].sum() == pytest.approx(1.0)
    assert normalized["predicted_product_demand"].sum() == pytest.approx(100.0)
    assert (normalized["predicted_product_share"] > 0).all()


# Purpose: Confirm daily product revenue and comparable metrics are correct.
# Used by: the unit-test suite.
def test_aggregate_daily_revenue_and_metrics() -> None:
    daily = aggregate_daily_revenue(_product_predictions())
    metrics = calculate_revenue_metrics(daily)

    assert len(daily) == 1
    assert daily.loc[0, "predicted_net_revenue"] == pytest.approx(10420.0)
    assert daily.loc[0, "actual_net_revenue"] == pytest.approx(10400.0)
    assert metrics["mae"] == pytest.approx(20.0)
    assert metrics["forecast_bias"] == pytest.approx(20.0)


# Purpose: Confirm revenue interval backtests use earlier origins only.
# Used by: the unit-test suite.
def test_revenue_interval_calibration_is_time_ordered() -> None:
    rows = []
    for origin_number, origin in enumerate(
        pd.date_range("2025-01-31", periods=4, freq="ME")
    ):
        for horizon in range(1, 31):
            rows.append(
                {
                    "forecast_created_date": origin,
                    "target_date": origin + pd.Timedelta(days=horizon),
                    "horizon_days": horizon,
                    "predicted_net_revenue": 100000.0,
                    "actual_net_revenue": (100000.0 + origin_number * 100 + horizon),
                }
            )

    calibrated = calibrate_revenue_backtest_intervals(pd.DataFrame(rows))
    origins = sorted(calibrated["forecast_created_date"].unique())
    initial = calibrated["forecast_created_date"].isin(origins[:3])

    assert calibrated.loc[initial, "lower_bound"].isna().all()
    assert calibrated.loc[~initial, "lower_bound"].notna().all()
    assert set(calibrated.loc[~initial, "calibration_observations"].astype(int)) == {
        21,
        27,
    }


# Purpose: Confirm production intervals cover all four forecast buckets.
# Used by: the unit-test suite.
def test_calibrate_production_revenue_intervals() -> None:
    forecast = pd.DataFrame(
        {
            "horizon_days": np.arange(1, 31),
            "predicted_net_revenue": 100000.0,
        }
    )
    backtests = pd.DataFrame(
        {
            "horizon_days": np.tile(np.arange(1, 31), 3),
            "predicted_net_revenue": 100000.0,
            "actual_net_revenue": 101000.0,
        }
    )

    calibrated = calibrate_production_revenue_intervals(forecast, backtests)

    assert calibrated["lower_bound"].eq(99000.0).all()
    assert calibrated["upper_bound"].eq(101000.0).all()
    assert set(calibrated["calibration_observations"]) == {21, 27}


# Purpose: Confirm database records reconcile across daily and product grains.
# Used by: the unit-test suite.
def test_build_and_validate_revenue_database_records() -> None:
    products = _product_predictions()
    daily = aggregate_daily_revenue(products)
    daily["lower_bound"] = 9000.0
    daily["upper_bound"] = 12000.0
    daily["interval_confidence"] = 0.8
    daily["interval_method"] = "horizon_bucket_conformal"
    daily["calibration_observations"] = 84

    daily_records, product_records = build_database_records(
        daily,
        products,
        "backtest",
        datetime(2026, 1, 1, tzinfo=UTC),
    )

    validate_database_records(daily_records, product_records)

    assert daily_records["run_id"].nunique() == 1
    assert product_records["run_id"].nunique() == 1
    assert product_records["predicted_product_share"].sum() == pytest.approx(1.0)
