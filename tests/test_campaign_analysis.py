from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analyze_campaign import (
    MATCHING_COVARIATES,
    CampaignAnalysisPipelineError,
    build_matched_pairs,
    moving_block_mean_interval,
    standardized_mean_difference,
    validate_database_records,
)


# Purpose: Create one treatment and two eligible controls with known distances.
# Used by: campaign nearest-neighbor matching tests.
def _matching_sample() -> tuple[pd.DataFrame, pd.DataFrame]:
    common = {
        "year_number": 2025,
        "season": "shoulder",
        "is_weekend": False,
        "holiday_flag": False,
        "school_break_flag": False,
        "severe_weather_flag": False,
        "planned_price_multiplier": 1.0,
        "precipitation_in": 0.0,
        "net_demand": 1_200.0,
        "net_revenue": 130_000.0,
        "net_revenue_per_ticket": 108.33,
    }
    treatment = pd.DataFrame(
        [
            {
                **common,
                "calendar_date": pd.Timestamp("2025-11-03"),
                "day_of_week": 1,
                "avg_temperature_f": 70.0,
            }
        ]
    )
    controls = pd.DataFrame(
        [
            {
                **common,
                "calendar_date": pd.Timestamp("2025-12-01"),
                "day_of_week": 1,
                "avg_temperature_f": 69.5,
            },
            {
                **common,
                "calendar_date": pd.Timestamp("2025-12-02"),
                "day_of_week": 2,
                "avg_temperature_f": 55.0,
            },
        ]
    )
    return treatment, controls


# Purpose: Create valid database-shaped records for validation tests.
# Used by: campaign record-contract tests.
def _database_records() -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
]:
    run_id = "campaign_test"
    run = pd.DataFrame(
        {
            "run_id": [run_id],
            "matched_pairs": [1],
            "causal_claim_allowed": [False],
        }
    )
    evaluation_rows = []
    for outcome in ("net_revenue", "net_demand", "net_revenue_per_ticket"):
        evaluation_rows.append(
            {
                "run_id": run_id,
                "outcome_name": outcome,
                "estimator_name": "matched_block_bootstrap",
                "period_name": "campaign_window",
                "estimate_value": 1.0,
                "lower_bound": 0.5,
                "upper_bound": 1.5,
            }
        )
        for period in ("campaign_window", "pre_period_placebo"):
            evaluation_rows.append(
                {
                    "run_id": run_id,
                    "outcome_name": outcome,
                    "estimator_name": "regression_hac",
                    "period_name": period,
                    "estimate_value": 1.0,
                    "lower_bound": 0.5,
                    "upper_bound": 1.5,
                }
            )
    evaluation = pd.DataFrame(evaluation_rows)
    matches = pd.DataFrame(
        {
            "run_id": [run_id],
            "treatment_date_key": [20251101],
            "treated_net_demand": [100],
            "control_net_demand": [90],
            "demand_lift": [10],
            "treated_net_revenue": [11_000.0],
            "control_net_revenue": [10_000.0],
            "net_revenue_lift": [1_000.0],
        }
    )
    balance = pd.DataFrame(
        {
            "run_id": run_id,
            "covariate_name": list(MATCHING_COVARIATES),
            "matched_smd": np.zeros(len(MATCHING_COVARIATES)),
            "balance_threshold": 0.1,
            "balance_passed": True,
        }
    )
    return run, evaluation, matches, balance


# Purpose: Confirm standardized differences use pooled standard deviation.
# Used by: the unit-test suite.
def test_standardized_mean_difference() -> None:
    treated = pd.Series([2.0, 4.0, 6.0])
    control = pd.Series([1.0, 3.0, 5.0])

    assert standardized_mean_difference(treated, control) == pytest.approx(0.5)


# Purpose: Confirm matching selects the closest eligible control date.
# Used by: the unit-test suite.
def test_build_matched_pairs_selects_nearest_control() -> None:
    treatment, controls = _matching_sample()

    matched = build_matched_pairs(treatment, controls)

    assert len(matched) == 1
    assert matched.loc[0, "control_date"] == pd.Timestamp("2025-12-01")


# Purpose: Confirm block-bootstrap intervals are deterministic and ordered.
# Used by: the unit-test suite.
def test_moving_block_mean_interval_is_repeatable() -> None:
    frame = pd.DataFrame(
        {
            "treated_date": pd.date_range("2023-11-01", periods=12, freq="D"),
            "campaign_year": [2023] * 4 + [2024] * 4 + [2025] * 4,
            "difference": np.arange(12, dtype=float),
        }
    )

    first = moving_block_mean_interval(
        frame,
        "difference",
        block_length=2,
        bootstrap_samples=200,
        seed=7,
    )
    second = moving_block_mean_interval(
        frame,
        "difference",
        block_length=2,
        bootstrap_samples=200,
        seed=7,
    )

    assert first == second
    assert first[0] <= frame["difference"].mean() <= first[1]


# Purpose: Confirm record validation rejects a failed matching balance gate.
# Used by: the unit-test suite.
def test_validate_database_records_rejects_balance_failure() -> None:
    run, evaluation, matches, balance = _database_records()
    validate_database_records(run, evaluation, matches, balance)

    balance.loc[0, "matched_smd"] = 0.2
    balance.loc[0, "balance_passed"] = False
    with pytest.raises(CampaignAnalysisPipelineError, match="balance threshold"):
        validate_database_records(run, evaluation, matches, balance)
