from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from src.forecast import (
    ForecastPipelineError,
    build_database_records,
    build_lstm_training_arrays,
    calculate_forecast_metrics,
    calibrate_backtest_intervals,
    conformal_absolute_quantile,
    validate_database_records,
    validate_forecast_input,
)


# Purpose: Create the smallest valid daily input contract for validation tests.
# Used by: forecast-input validation unit tests.
def _forecast_input() -> pd.DataFrame:
    dates = pd.date_range("2025-12-31", periods=31, freq="D")
    future = np.arange(len(dates)) > 0
    data = pd.DataFrame(
        {
            "date_key": dates.strftime("%Y%m%d").astype(int),
            "calendar_date": dates,
            "target_demand": np.where(future, np.nan, 1000),
            "month_number": dates.month,
            "day_of_week": dates.dayofweek + 1,
            "observed_temperature_f": np.where(future, np.nan, 70.0),
            "observed_precipitation_in": np.where(future, np.nan, 0.0),
            "observed_severe_weather_flag": np.where(future, np.nan, 0.0),
            "planned_price_multiplier": 1.0,
            "available_capacity": 2500,
            "demand_target": 1200,
            "holiday_flag": 0,
            "school_break_flag": 0,
            "active_campaign_count": 0,
            "maximum_planned_discount": 0.0,
            "paid_media_flag": 0,
            "email_campaign_flag": 0,
            "bundle_campaign_flag": 0,
            "discount_campaign_flag": 0,
        }
    )
    return data


# Purpose: Build a valid two-day selected-model forecast for persistence tests.
# Used by: database-record unit tests.
def _production_predictions() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "forecast_created_date": pd.to_datetime(
                ["2025-12-31", "2025-12-31"]
            ),
            "training_end_date": pd.to_datetime(
                ["2025-12-31", "2025-12-31"]
            ),
            "target_date": pd.to_datetime(["2026-01-01", "2026-01-02"]),
            "horizon_days": [1, 2],
            "model_name": ["sarimax_exogenous", "sarimax_exogenous"],
            "model_version": ["1.0", "1.0"],
            "predicted_demand": [1200.126, 1300.224],
            "lower_bound": [1000.0, 1100.0],
            "upper_bound": [1400.0, 1500.0],
            "interval_confidence": [0.8, 0.8],
            "interval_method": [
                "horizon_bucket_conformal",
                "horizon_bucket_conformal",
            ],
            "calibration_observations": [84, 84],
            "actual_demand": [np.nan, np.nan],
        }
    )


# Purpose: Confirm valid input passes and future observed weather is rejected.
# Used by: the unit-test suite.
def test_validate_forecast_input_rejects_future_weather_leakage() -> None:
    data = _forecast_input()
    validate_forecast_input(data)

    data.loc[data.index[-1], "observed_temperature_f"] = 75.0
    with pytest.raises(ForecastPipelineError, match="weather leakage"):
        validate_forecast_input(data)


# Purpose: Confirm core accuracy metrics preserve error direction and magnitude.
# Used by: the unit-test suite.
def test_calculate_forecast_metrics() -> None:
    predictions = pd.DataFrame(
        {"actual_demand": [100.0, 200.0], "predicted_demand": [110.0, 180.0]}
    )

    metrics = calculate_forecast_metrics(predictions)

    assert metrics["observations"] == 2
    assert metrics["mae"] == pytest.approx(15.0)
    assert metrics["rmse"] == pytest.approx((250.0) ** 0.5)
    assert metrics["forecast_bias"] == pytest.approx(-5.0)


# Purpose: Confirm the finite-sample conformal rank uses the conservative tail.
# Used by: the unit-test suite.
def test_conformal_absolute_quantile() -> None:
    errors = np.arange(1, 11)

    assert conformal_absolute_quantile(errors, alpha=0.20) == 9.0


# Purpose: Confirm calibration never uses the current or future forecast origin.
# Used by: the unit-test suite.
def test_calibrate_backtest_intervals_is_time_ordered() -> None:
    rows = []
    for origin_number, origin in enumerate(
        pd.date_range("2025-01-31", periods=4, freq="ME")
    ):
        for horizon in range(1, 31):
            rows.append(
                {
                    "forecast_created_date": origin,
                    "training_end_date": origin,
                    "target_date": origin + pd.Timedelta(days=horizon),
                    "horizon_days": horizon,
                    "model_name": "sarimax_exogenous",
                    "model_version": "1.0",
                    "predicted_demand": 1000.0,
                    "actual_demand": 1000.0 + origin_number + horizon,
                }
            )

    calibrated = calibrate_backtest_intervals(pd.DataFrame(rows))
    first_three = calibrated["forecast_created_date"].isin(
        sorted(calibrated["forecast_created_date"].unique())[:3]
    )

    assert calibrated.loc[first_three, "lower_bound"].isna().all()
    assert calibrated.loc[~first_three, "lower_bound"].notna().all()
    assert set(
        calibrated.loc[~first_three, "calibration_observations"].astype(int)
    ) == {21, 27}


# Purpose: Confirm LSTM sequences and next-day features retain chronological grain.
# Used by: the unit-test suite.
def test_build_lstm_training_arrays_shapes() -> None:
    demand = np.arange(10, dtype=float)
    exogenous = np.arange(20, dtype=float).reshape(10, 2)

    sequences, next_features, targets, history = build_lstm_training_arrays(
        demand, exogenous, lookback=3
    )

    assert sequences.shape == (7, 3, 3)
    assert next_features.shape == (7, 2)
    assert targets.shape == (7,)
    assert history.shape == (10, 3)


# Purpose: Confirm production metadata is complete and database-safe.
# Used by: the unit-test suite.
def test_build_and_validate_production_database_records() -> None:
    records = build_database_records(
        _production_predictions(),
        run_type="production",
        execution_time=datetime(2026, 1, 1, tzinfo=UTC),
    )

    validate_database_records(records)

    assert records["run_id"].nunique() == 1
    assert records["predicted_demand"].tolist() == [1200.13, 1300.22]
    assert records["actual_demand"].isna().all()


# Purpose: Confirm production records cannot silently lose interval metadata.
# Used by: the unit-test suite.
def test_validate_database_records_rejects_incomplete_interval() -> None:
    records = build_database_records(
        _production_predictions(),
        run_type="production",
        execution_time=datetime(2026, 1, 1, tzinfo=UTC),
    )
    records.loc[0, "interval_method"] = None

    with pytest.raises(ForecastPipelineError, match="interval metadata"):
        validate_database_records(records)
