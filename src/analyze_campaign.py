"""Estimate and persist observational campaign-associated business lift.

The pipeline reproduces the validated campaign notebook with versioned output.
It matches clean Holiday Early Booking dates to comparable no-campaign dates,
bootstraps paired outcome differences, fits a full-history HAC sensitivity
model, and writes auditable results for SQL and Tableau.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
from sqlalchemy import Engine, create_engine, text

from src.forecast import DEFAULT_DATABASE_URL

ANALYSIS_VERSION = "1.0"
CAMPAIGN_FAMILY = "HOLIDAY_EARLY"
PRIMARY_OUTCOME = "net_revenue"
MATCHES_PER_TREATMENT = 1
BALANCE_THRESHOLD = 0.10
BOOTSTRAP_SAMPLES = 5_000
BOOTSTRAP_BLOCK_DAYS = 7
BOOTSTRAP_SEED = 2_026
HAC_MAX_LAGS = 7

MATCHING_COVARIATES = (
    "holiday_flag",
    "school_break_flag",
    "severe_weather_flag",
    "planned_price_multiplier",
    "avg_temperature_f",
    "precipitation_in",
)
MATCHING_WEIGHTS = {
    **{covariate: 1.0 for covariate in MATCHING_COVARIATES},
    "avg_temperature_f": 4.0,
}
OUTCOMES = (
    "net_revenue",
    "net_demand",
    "net_revenue_per_ticket",
)
OUTCOME_UNITS = {
    "net_revenue": "usd_per_day",
    "net_demand": "tickets_per_day",
    "net_revenue_per_ticket": "usd_per_ticket",
}


class CampaignAnalysisPipelineError(RuntimeError):
    """Raised when campaign inputs, estimates, or persistence are invalid."""


# Purpose: Parse repeatable campaign-analysis settings without exposing credentials.
# Used by: main.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estimate and persist Holiday Early campaign-associated lift."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy PostgreSQL URL. Defaults to DATABASE_URL or local PostgreSQL.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root used for human-review CSV exports.",
    )
    return parser.parse_args()


# Purpose: Read daily outcomes, observed controls, and active campaign flags.
# Used by: run_pipeline.
def load_daily_campaign_input(engine: Engine) -> pd.DataFrame:
    query = text(
        """
        SELECT
            daily.calendar_date,
            daily.year_number,
            daily.month_number,
            daily.day_of_week,
            daily.is_weekend,
            daily.season,
            daily.holiday_flag,
            daily.school_break_flag,
            daily.severe_weather_flag,
            daily.avg_temperature_f::double precision AS avg_temperature_f,
            daily.precipitation_in::double precision AS precipitation_in,
            daily.planned_price_multiplier::double precision
                AS planned_price_multiplier,
            daily.net_demand::double precision AS net_demand,
            daily.net_revenue::double precision AS net_revenue,
            daily.average_net_revenue_per_ticket::double precision
                AS net_revenue_per_ticket,
            daily.available_capacity::double precision AS available_capacity,
            daily.capacity_utilization::double precision AS capacity_utilization,
            count(campaign.campaign_key)::integer AS active_campaign_count,
            coalesce(bool_or(campaign.campaign_type = 'discount'), false)
                AS discount_active,
            coalesce(bool_or(campaign.campaign_type = 'paid_media'), false)
                AS paid_media_active,
            coalesce(bool_or(campaign.campaign_type = 'bundle'), false)
                AS bundle_active,
            coalesce(bool_or(campaign.campaign_type = 'email'), false)
                AS email_active
        FROM analytics.vw_daily_performance AS daily
        LEFT JOIN analytics.dim_campaign AS campaign
            ON NOT campaign.is_no_campaign
           AND daily.calendar_date BETWEEN
               campaign.start_date AND campaign.end_date
        WHERE daily.has_actuals
        GROUP BY
            daily.calendar_date,
            daily.year_number,
            daily.month_number,
            daily.day_of_week,
            daily.is_weekend,
            daily.season,
            daily.holiday_flag,
            daily.school_break_flag,
            daily.severe_weather_flag,
            daily.avg_temperature_f,
            daily.precipitation_in,
            daily.planned_price_multiplier,
            daily.net_demand,
            daily.net_revenue,
            daily.average_net_revenue_per_ticket,
            daily.available_capacity,
            daily.capacity_utilization
        ORDER BY daily.calendar_date
        """
    )
    with engine.connect() as connection:
        return pd.read_sql_query(
            query,
            connection,
            parse_dates=["calendar_date"],
        )


# Purpose: Read campaign spend and attributed discount cost by purchase date.
# Used by: run_pipeline and build_run_record.
def load_campaign_cost_input(engine: Engine) -> pd.DataFrame:
    query = text(
        """
        WITH attributed_sales AS (
            SELECT
                purchase_date_key AS date_key,
                campaign_key,
                sum(discount_amount)::double precision
                    AS attributed_discount_amount
            FROM analytics.fact_ticket_sales
            GROUP BY purchase_date_key, campaign_key
        )
        SELECT
            date_dim.calendar_date,
            campaign.campaign_code,
            delivery.spend::double precision AS marketing_spend,
            delivery.attributed_revenue::double precision AS attributed_revenue,
            coalesce(sales.attributed_discount_amount, 0)::double precision
                AS attributed_discount_amount
        FROM analytics.fact_campaign_daily AS delivery
        JOIN analytics.dim_campaign AS campaign USING (campaign_key)
        JOIN analytics.dim_date AS date_dim USING (date_key)
        LEFT JOIN attributed_sales AS sales
            ON sales.date_key = delivery.date_key
           AND sales.campaign_key = delivery.campaign_key
        WHERE campaign.campaign_code LIKE 'HOLIDAY_EARLY_%'
        ORDER BY date_dim.calendar_date
        """
    )
    with engine.connect() as connection:
        return pd.read_sql_query(
            query,
            connection,
            parse_dates=["calendar_date"],
        )


# Purpose: Enforce the historical daily and campaign-cost input contracts.
# Used by: run_pipeline before any statistical calculation.
def validate_campaign_inputs(daily: pd.DataFrame, costs: pd.DataFrame) -> None:
    required_daily = {
        "calendar_date",
        "year_number",
        "month_number",
        "day_of_week",
        "is_weekend",
        "season",
        "holiday_flag",
        "school_break_flag",
        "severe_weather_flag",
        "avg_temperature_f",
        "precipitation_in",
        "planned_price_multiplier",
        "net_demand",
        "net_revenue",
        "net_revenue_per_ticket",
        "capacity_utilization",
        "active_campaign_count",
        "discount_active",
        "paid_media_active",
        "bundle_active",
        "email_active",
    }
    required_costs = {
        "calendar_date",
        "campaign_code",
        "marketing_spend",
        "attributed_revenue",
        "attributed_discount_amount",
    }
    missing_daily = sorted(required_daily.difference(daily.columns))
    missing_costs = sorted(required_costs.difference(costs.columns))
    if missing_daily or missing_costs:
        raise CampaignAnalysisPipelineError(
            "Campaign inputs are missing columns: "
            + ", ".join([*missing_daily, *missing_costs])
        )
    if len(daily) != 1_096 or daily["calendar_date"].duplicated().any():
        raise CampaignAnalysisPipelineError(
            "Expected 1,096 unique historical daily observations."
        )
    if not daily["calendar_date"].is_monotonic_increasing:
        raise CampaignAnalysisPipelineError("Campaign input is not chronological.")
    if daily[list(MATCHING_COVARIATES) + list(OUTCOMES)].isna().any().any():
        raise CampaignAnalysisPipelineError("Campaign input contains missing values.")
    if daily[["net_demand", "net_revenue", "net_revenue_per_ticket"]].le(0).any().any():
        raise CampaignAnalysisPipelineError("Campaign outcomes must be positive.")
    if costs.empty or costs[list(required_costs)].isna().any().any():
        raise CampaignAnalysisPipelineError(
            "Campaign cost input is empty or incomplete."
        )


# Purpose: Define clean treatment dates and the no-campaign control pool.
# Used by: run_pipeline before matching.
def define_analysis_samples(
    daily: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    prepared = daily.copy()
    prepared["clean_discount_treatment"] = prepared["discount_active"] & prepared[
        "active_campaign_count"
    ].eq(1)
    prepared["eligible_control"] = prepared["active_campaign_count"].eq(0)
    prepared["trend_days"] = (
        prepared["calendar_date"] - prepared["calendar_date"].min()
    ).dt.days

    treatment = prepared.loc[prepared["clean_discount_treatment"]].copy()
    control_pool = prepared.loc[prepared["eligible_control"]].copy()
    if len(treatment) != 135 or len(control_pool) != 180:
        raise CampaignAnalysisPipelineError(
            "Expected 135 clean treatment days and 180 no-campaign controls."
        )
    if not treatment.groupby("year_number").size().eq(45).all():
        raise CampaignAnalysisPipelineError(
            "Clean treatment dates do not contain 45 days per year."
        )
    if treatment["email_active"].any():
        raise CampaignAnalysisPipelineError("Treatment overlaps the email campaign.")
    if treatment["capacity_utilization"].ge(1).any():
        raise CampaignAnalysisPipelineError(
            "Treatment demand is capacity censored and cannot be evaluated safely."
        )
    return prepared, treatment, control_pool


# Purpose: Match each treatment date to its closest comparable no-campaign date.
# Used by: build_analysis_outputs and unit tests.
def build_matched_pairs(
    treatment: pd.DataFrame,
    control_pool: pd.DataFrame,
) -> pd.DataFrame:
    scales = (
        pd.concat([treatment, control_pool])[list(MATCHING_COVARIATES)]
        .astype(float)
        .std(ddof=0)
        .replace(0, 1)
    )
    records: list[dict[str, Any]] = []

    for treated_row in treatment.itertuples(index=False):
        candidates = control_pool.loc[
            control_pool["year_number"].eq(treated_row.year_number)
            & control_pool["season"].eq(treated_row.season)
            & control_pool["is_weekend"].eq(treated_row.is_weekend)
        ].copy()
        if len(candidates) < MATCHES_PER_TREATMENT:
            raise CampaignAnalysisPipelineError(
                f"Insufficient controls for {treated_row.calendar_date.date()}."
            )

        squared_distance = np.zeros(len(candidates), dtype=float)
        for covariate in MATCHING_COVARIATES:
            treated_value = float(getattr(treated_row, covariate))
            candidate_value = candidates[covariate].astype(float)
            squared_distance += (
                MATCHING_WEIGHTS[covariate]
                * ((candidate_value - treated_value) / scales[covariate]) ** 2
            )

        calendar_distance = (
            candidates["calendar_date"] - treated_row.calendar_date
        ).dt.days.abs()
        weekday_distance = (candidates["day_of_week"] - treated_row.day_of_week).abs()
        weekday_distance = np.minimum(weekday_distance, 7 - weekday_distance)
        candidates["match_distance"] = (
            np.sqrt(squared_distance)
            + 0.75 * weekday_distance
            + 0.05 * calendar_distance / 30
        )
        selected = candidates.nsmallest(
            MATCHES_PER_TREATMENT,
            ["match_distance", "calendar_date"],
        )

        for control_row in selected.itertuples(index=False):
            record: dict[str, Any] = {
                "treated_date": treated_row.calendar_date,
                "control_date": control_row.calendar_date,
                "campaign_year": int(treated_row.year_number),
                "season": treated_row.season,
                "day_of_week": int(treated_row.day_of_week),
                "match_distance": float(control_row.match_distance),
            }
            for column in (*MATCHING_COVARIATES, *OUTCOMES):
                record[f"treated_{column}"] = getattr(treated_row, column)
                record[f"control_{column}"] = getattr(control_row, column)
            records.append(record)

    pairs = pd.DataFrame.from_records(records)
    if len(pairs) != len(treatment) * MATCHES_PER_TREATMENT:
        raise CampaignAnalysisPipelineError("Matched-pair row count is incorrect.")
    if pairs["treated_date"].nunique() != len(treatment):
        raise CampaignAnalysisPipelineError("A treatment date is missing a match.")
    return pairs


# Purpose: Express a group difference in pooled standard-deviation units.
# Used by: build_balance_table and unit tests.
def standardized_mean_difference(
    treated_values: pd.Series,
    control_values: pd.Series,
) -> float:
    treated = pd.Series(treated_values, dtype=float)
    control = pd.Series(control_values, dtype=float)
    pooled_standard_deviation = np.sqrt((treated.var(ddof=1) + control.var(ddof=1)) / 2)
    if np.isclose(pooled_standard_deviation, 0):
        return 0.0 if np.isclose(treated.mean(), control.mean()) else float("nan")
    return float((treated.mean() - control.mean()) / pooled_standard_deviation)


# Purpose: Quantify observable balance before and after date matching.
# Used by: build_analysis_outputs and persistence validation.
def build_balance_table(
    treatment: pd.DataFrame,
    control_pool: pd.DataFrame,
    pairs: pd.DataFrame,
) -> pd.DataFrame:
    records = []
    for covariate in MATCHING_COVARIATES:
        raw_smd = standardized_mean_difference(
            treatment[covariate],
            control_pool[covariate],
        )
        matched_smd = standardized_mean_difference(
            pairs[f"treated_{covariate}"],
            pairs[f"control_{covariate}"],
        )
        records.append(
            {
                "covariate_name": covariate,
                "raw_smd": raw_smd,
                "matched_smd": matched_smd,
                "balance_threshold": BALANCE_THRESHOLD,
                "balance_passed": abs(matched_smd) <= BALANCE_THRESHOLD,
            }
        )
    return pd.DataFrame.from_records(records)


# Purpose: Give each treatment date equal weight after matching.
# Used by: evaluate_matched_lift and build_match_records.
def build_paired_daily(pairs: pd.DataFrame) -> pd.DataFrame:
    aggregation: dict[str, tuple[str, str]] = {
        "campaign_year": ("campaign_year", "first"),
        "control_date": ("control_date", "first"),
        "season": ("season", "first"),
        "day_of_week": ("day_of_week", "first"),
        "match_distance": ("match_distance", "mean"),
    }
    for outcome in OUTCOMES:
        aggregation[f"treated_{outcome}"] = (f"treated_{outcome}", "first")
        aggregation[f"control_{outcome}"] = (f"control_{outcome}", "mean")
    return (
        pairs.groupby("treated_date", as_index=False)
        .agg(**aggregation)
        .sort_values("treated_date")
        .reset_index(drop=True)
    )


# Purpose: Preserve weekly dependence when estimating paired-mean uncertainty.
# Used by: evaluate_matched_lift and unit tests.
def moving_block_mean_interval(
    frame: pd.DataFrame,
    value_column: str,
    block_length: int = BOOTSTRAP_BLOCK_DAYS,
    bootstrap_samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    yearly_values = [
        group.sort_values("treated_date")[value_column].to_numpy(dtype=float)
        for _, group in frame.groupby("campaign_year")
    ]
    bootstrap_means = np.empty(bootstrap_samples, dtype=float)
    for sample_number in range(bootstrap_samples):
        resampled_values: list[float] = []
        for values in yearly_values:
            required_blocks = int(np.ceil(len(values) / block_length))
            starts = rng.integers(0, len(values), size=required_blocks)
            indices = np.concatenate(
                [(start + np.arange(block_length)) % len(values) for start in starts]
            )[: len(values)]
            resampled_values.extend(values[indices])
        bootstrap_means[sample_number] = np.mean(resampled_values)
    lower, upper = np.quantile(bootstrap_means, [0.025, 0.975])
    return float(lower), float(upper)


# Purpose: Estimate paired campaign-window lift and block-bootstrap intervals.
# Used by: build_analysis_outputs.
def evaluate_matched_lift(paired_daily: pd.DataFrame) -> pd.DataFrame:
    records = []
    for outcome in OUTCOMES:
        difference_column = f"{outcome}_difference"
        paired_daily[difference_column] = (
            paired_daily[f"treated_{outcome}"] - paired_daily[f"control_{outcome}"]
        )
        lower, upper = moving_block_mean_interval(
            paired_daily,
            difference_column,
        )
        mean_control = paired_daily[f"control_{outcome}"].mean()
        mean_difference = paired_daily[difference_column].mean()
        difference_standard_deviation = paired_daily[difference_column].std(ddof=1)
        records.append(
            {
                "outcome_name": outcome,
                "estimator_name": "matched_block_bootstrap",
                "period_name": "campaign_window",
                "observations": len(paired_daily),
                "estimate_value": mean_difference,
                "estimate_pct": mean_difference / mean_control * 100,
                "lower_bound": lower,
                "upper_bound": upper,
                "estimate_unit": OUTCOME_UNITS[outcome],
                "p_value": None,
                "effect_size": mean_difference / difference_standard_deviation,
                "interval_excludes_zero": lower * upper > 0,
                "is_primary_outcome": outcome == PRIMARY_OUTCOME,
            }
        )
    return pd.DataFrame.from_records(records)


# Purpose: Add annual Fourier terms without collinear month-campaign indicators.
# Used by: evaluate_regression_sensitivity.
def prepare_regression_data(daily: pd.DataFrame) -> pd.DataFrame:
    prepared = daily.copy()
    flag_columns = (
        "holiday_flag",
        "school_break_flag",
        "severe_weather_flag",
        "discount_active",
        "paid_media_active",
        "bundle_active",
        "email_active",
    )
    for column in flag_columns:
        prepared[column] = prepared[column].astype(int)

    prepared["discount_lead_28d"] = False
    for year in sorted(prepared["year_number"].unique()):
        lead_start = pd.Timestamp(year=int(year), month=9, day=17)
        lead_end = pd.Timestamp(year=int(year), month=10, day=14)
        prepared.loc[
            prepared["calendar_date"].between(lead_start, lead_end),
            "discount_lead_28d",
        ] = True
    prepared["discount_lead_28d"] = prepared["discount_lead_28d"].astype(int)
    prepared["day_of_year"] = prepared["calendar_date"].dt.dayofyear
    for harmonic in range(1, 5):
        angle = 2 * np.pi * harmonic * prepared["day_of_year"] / 365.25
        prepared[f"annual_sin_{harmonic}"] = np.sin(angle)
        prepared[f"annual_cos_{harmonic}"] = np.cos(angle)
    return prepared


# Purpose: Test matched conclusions with a full-history controlled estimator.
# Used by: build_analysis_outputs.
def evaluate_regression_sensitivity(daily: pd.DataFrame) -> pd.DataFrame:
    prepared = prepare_regression_data(daily)
    seasonality_terms = " + ".join(
        [f"annual_sin_{harmonic} + annual_cos_{harmonic}" for harmonic in range(1, 5)]
    )
    control_terms = (
        "discount_active + discount_lead_28d + paid_media_active + "
        "bundle_active + email_active + planned_price_multiplier + "
        "holiday_flag + school_break_flag + avg_temperature_f + "
        "precipitation_in + severe_weather_flag + C(day_of_week) + "
        f"C(year_number) + {seasonality_terms}"
    )
    records = []
    for outcome in OUTCOMES:
        formula = f"np.log({outcome}) ~ {control_terms}"
        fitted = smf.ols(formula, data=prepared).fit(
            cov_type="HAC",
            cov_kwds={"maxlags": HAC_MAX_LAGS},
        )
        if np.linalg.matrix_rank(fitted.model.exog) != fitted.model.exog.shape[1]:
            raise CampaignAnalysisPipelineError(
                f"Rank-deficient campaign regression for {outcome}."
            )
        for term, period in (
            ("discount_active", "campaign_window"),
            ("discount_lead_28d", "pre_period_placebo"),
        ):
            lower_log, upper_log = fitted.conf_int().loc[term]
            estimate_pct = np.expm1(fitted.params[term]) * 100
            lower_pct = np.expm1(lower_log) * 100
            upper_pct = np.expm1(upper_log) * 100
            records.append(
                {
                    "outcome_name": outcome,
                    "estimator_name": "regression_hac",
                    "period_name": period,
                    "observations": int(fitted.nobs),
                    "estimate_value": estimate_pct,
                    "estimate_pct": estimate_pct,
                    "lower_bound": lower_pct,
                    "upper_bound": upper_pct,
                    "estimate_unit": "percent",
                    "p_value": fitted.pvalues[term],
                    "effect_size": None,
                    "interval_excludes_zero": lower_pct * upper_pct > 0,
                    "is_primary_outcome": outcome == PRIMARY_OUTCOME,
                }
            )
    return pd.DataFrame.from_records(records)


# Purpose: Build every analysis output before database-specific formatting.
# Used by: run_pipeline and integration tests.
def build_analysis_outputs(
    daily: pd.DataFrame,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
]:
    prepared, treatment, control_pool = define_analysis_samples(daily)
    pairs = build_matched_pairs(treatment, control_pool)
    balance = build_balance_table(treatment, control_pool, pairs)
    paired_daily = build_paired_daily(pairs)
    matched_evaluation = evaluate_matched_lift(paired_daily)
    regression_evaluation = evaluate_regression_sensitivity(prepared)
    evaluation = pd.DataFrame.from_records(
        [
            *matched_evaluation.to_dict(orient="records"),
            *regression_evaluation.to_dict(orient="records"),
        ]
    )
    return treatment, control_pool, paired_daily, balance, evaluation


# Purpose: Convert validated statistical outputs to database-shaped records.
# Used by: run_pipeline before persistence.
def build_database_records(
    treatment: pd.DataFrame,
    control_pool: pd.DataFrame,
    paired_daily: pd.DataFrame,
    balance: pd.DataFrame,
    evaluation: pd.DataFrame,
    campaign_costs: pd.DataFrame,
    execution_time: datetime,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    timestamp = execution_time.astimezone(UTC)
    run_id = f"campaign_{timestamp:%Y%m%dT%H%M%S%fZ}"
    clean_costs = campaign_costs.loc[
        campaign_costs["calendar_date"].isin(set(treatment["calendar_date"]))
    ]
    matched_revenue = evaluation.loc[
        evaluation["outcome_name"].eq("net_revenue")
        & evaluation["estimator_name"].eq("matched_block_bootstrap")
    ].iloc[0]
    placebo_p_value = evaluation.loc[
        evaluation["outcome_name"].eq("net_revenue")
        & evaluation["estimator_name"].eq("regression_hac")
        & evaluation["period_name"].eq("pre_period_placebo"),
        "p_value",
    ].iloc[0]
    incremental_revenue = matched_revenue["estimate_value"] * len(paired_daily)
    incremental_lower = matched_revenue["lower_bound"] * len(paired_daily)
    incremental_upper = matched_revenue["upper_bound"] * len(paired_daily)
    marketing_spend = clean_costs["marketing_spend"].sum()

    run = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "campaign_family": CAMPAIGN_FAMILY,
                "analysis_version": ANALYSIS_VERSION,
                "treatment_definition": (
                    "Holiday Early Booking is the only active campaign"
                ),
                "control_definition": "No active campaign",
                "primary_outcome": PRIMARY_OUTCOME,
                "treatment_days": len(treatment),
                "control_pool_days": len(control_pool),
                "matched_pairs": len(paired_daily),
                "matching_ratio": MATCHES_PER_TREATMENT,
                "balance_threshold": BALANCE_THRESHOLD,
                "matched_balance_passed": bool(balance["balance_passed"].all()),
                "bootstrap_samples": BOOTSTRAP_SAMPLES,
                "bootstrap_block_days": BOOTSTRAP_BLOCK_DAYS,
                "marketing_spend": marketing_spend,
                "attributed_discount_amount": clean_costs[
                    "attributed_discount_amount"
                ].sum(),
                "estimated_incremental_net_revenue": incremental_revenue,
                "incremental_revenue_lower_95": incremental_lower,
                "incremental_revenue_upper_95": incremental_upper,
                "estimated_net_revenue_after_marketing": (
                    incremental_revenue - marketing_spend
                ),
                "associated_revenue_on_marketing_spend": (
                    incremental_revenue / marketing_spend
                ),
                "associated_return_after_marketing_spend": (
                    (incremental_revenue - marketing_spend) / marketing_spend
                ),
                "pre_period_revenue_placebo_p_value": placebo_p_value,
                "causal_claim_allowed": False,
                "created_at": timestamp,
            }
        ]
    )

    evaluation_records = evaluation.copy()
    evaluation_records.insert(0, "run_id", run_id)
    evaluation_records["estimate_value"] = evaluation_records["estimate_value"].round(6)
    evaluation_records["estimate_pct"] = evaluation_records["estimate_pct"].round(6)
    evaluation_records["lower_bound"] = evaluation_records["lower_bound"].round(6)
    evaluation_records["upper_bound"] = evaluation_records["upper_bound"].round(6)
    evaluation_records["p_value"] = evaluation_records["p_value"].map(
        lambda value: None if pd.isna(value) else round(float(value), 10)
    )
    evaluation_records["effect_size"] = evaluation_records["effect_size"].map(
        lambda value: None if pd.isna(value) else round(float(value), 8)
    )

    match_records = pd.DataFrame(
        {
            "run_id": run_id,
            "treatment_date_key": paired_daily["treated_date"]
            .dt.strftime("%Y%m%d")
            .astype(int),
            "control_date_key": paired_daily["control_date"]
            .dt.strftime("%Y%m%d")
            .astype(int),
            "campaign_year": paired_daily["campaign_year"].astype(int),
            "season": paired_daily["season"],
            "day_of_week": paired_daily["day_of_week"].astype(int),
            "match_distance": paired_daily["match_distance"].round(8),
            "treated_net_demand": paired_daily["treated_net_demand"].astype(int),
            "control_net_demand": paired_daily["control_net_demand"]
            .round()
            .astype(int),
            "treated_net_revenue": paired_daily["treated_net_revenue"].round(2),
            "control_net_revenue": paired_daily["control_net_revenue"].round(2),
            "treated_net_revenue_per_ticket": paired_daily[
                "treated_net_revenue_per_ticket"
            ].round(4),
            "control_net_revenue_per_ticket": paired_daily[
                "control_net_revenue_per_ticket"
            ].round(4),
        }
    )
    match_records["demand_lift"] = (
        match_records["treated_net_demand"] - match_records["control_net_demand"]
    )
    match_records["net_revenue_lift"] = (
        match_records["treated_net_revenue"] - match_records["control_net_revenue"]
    ).round(2)
    match_records["net_revenue_per_ticket_lift"] = (
        match_records["treated_net_revenue_per_ticket"]
        - match_records["control_net_revenue_per_ticket"]
    ).round(4)

    balance_records = balance.copy()
    balance_records.insert(0, "run_id", run_id)
    balance_records["raw_smd"] = balance_records["raw_smd"].round(8)
    balance_records["matched_smd"] = balance_records["matched_smd"].round(8)
    balance_records["balance_threshold"] = balance_records["balance_threshold"].round(6)
    numeric_run_columns = [
        "marketing_spend",
        "attributed_discount_amount",
        "estimated_incremental_net_revenue",
        "incremental_revenue_lower_95",
        "incremental_revenue_upper_95",
        "estimated_net_revenue_after_marketing",
    ]
    run[numeric_run_columns] = run[numeric_run_columns].round(2)
    run[
        [
            "associated_revenue_on_marketing_spend",
            "associated_return_after_marketing_spend",
        ]
    ] = run[
        [
            "associated_revenue_on_marketing_spend",
            "associated_return_after_marketing_spend",
        ]
    ].round(6)
    run["pre_period_revenue_placebo_p_value"] = run[
        "pre_period_revenue_placebo_p_value"
    ].round(10)
    return run, evaluation_records, match_records, balance_records


# Purpose: Enforce cross-table grain, interval, balance, and lift invariants.
# Used by: persist_analysis and unit tests.
def validate_database_records(
    run: pd.DataFrame,
    evaluation: pd.DataFrame,
    matches: pd.DataFrame,
    balance: pd.DataFrame,
) -> None:
    if len(run) != 1 or run["run_id"].nunique() != 1:
        raise CampaignAnalysisPipelineError("Expected exactly one campaign run row.")
    run_id = run.loc[0, "run_id"]
    if len(run_id) > 64:
        raise CampaignAnalysisPipelineError("Campaign run_id exceeds 64 characters.")
    for frame in (evaluation, matches, balance):
        if frame.empty or not frame["run_id"].eq(run_id).all():
            raise CampaignAnalysisPipelineError(
                "Campaign result tables are empty or use inconsistent run IDs."
            )
    if (
        len(evaluation) != 9
        or evaluation.duplicated(
            ["outcome_name", "estimator_name", "period_name"]
        ).any()
    ):
        raise CampaignAnalysisPipelineError(
            "Campaign evaluation must contain nine unique estimator results."
        )
    if not (
        evaluation["lower_bound"].le(evaluation["estimate_value"]).all()
        and evaluation["estimate_value"].le(evaluation["upper_bound"]).all()
    ):
        raise CampaignAnalysisPipelineError(
            "A campaign estimate falls outside its confidence interval."
        )
    if len(matches) != int(run.loc[0, "matched_pairs"]):
        raise CampaignAnalysisPipelineError("Campaign match count is inconsistent.")
    if matches.duplicated("treatment_date_key").any():
        raise CampaignAnalysisPipelineError("A treatment date has duplicate matches.")
    if (
        not matches["demand_lift"]
        .eq(matches["treated_net_demand"] - matches["control_net_demand"])
        .all()
    ):
        raise CampaignAnalysisPipelineError("Stored demand lift is inconsistent.")
    expected_revenue_lift = (
        matches["treated_net_revenue"] - matches["control_net_revenue"]
    ).round(2)
    if not matches["net_revenue_lift"].eq(expected_revenue_lift).all():
        raise CampaignAnalysisPipelineError("Stored revenue lift is inconsistent.")
    if len(balance) != len(MATCHING_COVARIATES):
        raise CampaignAnalysisPipelineError("Campaign balance rows are incomplete.")
    expected_pass = balance["matched_smd"].abs().le(balance["balance_threshold"])
    if not balance["balance_passed"].eq(expected_pass).all():
        raise CampaignAnalysisPipelineError("Campaign balance flags are incorrect.")
    if not balance["balance_passed"].all():
        raise CampaignAnalysisPipelineError(
            "Campaign matching failed the declared balance threshold."
        )
    if bool(run.loc[0, "causal_claim_allowed"]):
        raise CampaignAnalysisPipelineError(
            "Observational campaign results cannot allow a causal claim."
        )


# Purpose: Convert pandas nulls and extension scalars to SQL-safe records.
# Used by: persist_analysis.
def dataframe_payload(
    frame: pd.DataFrame,
    columns: list[str],
) -> list[dict[str, Any]]:
    payload_frame = frame[columns].astype(object)
    payload_frame = payload_frame.where(pd.notna(payload_frame), None)
    return payload_frame.to_dict(orient="records")


# Purpose: Append all campaign analysis grains atomically and verify row counts.
# Used by: run_pipeline.
def persist_analysis(
    engine: Engine,
    run: pd.DataFrame,
    evaluation: pd.DataFrame,
    matches: pd.DataFrame,
    balance: pd.DataFrame,
) -> None:
    validate_database_records(run, evaluation, matches, balance)
    run_columns = [
        "run_id",
        "campaign_family",
        "analysis_version",
        "treatment_definition",
        "control_definition",
        "primary_outcome",
        "treatment_days",
        "control_pool_days",
        "matched_pairs",
        "matching_ratio",
        "balance_threshold",
        "matched_balance_passed",
        "bootstrap_samples",
        "bootstrap_block_days",
        "marketing_spend",
        "attributed_discount_amount",
        "estimated_incremental_net_revenue",
        "incremental_revenue_lower_95",
        "incremental_revenue_upper_95",
        "estimated_net_revenue_after_marketing",
        "associated_revenue_on_marketing_spend",
        "associated_return_after_marketing_spend",
        "pre_period_revenue_placebo_p_value",
        "causal_claim_allowed",
        "created_at",
    ]
    evaluation_columns = [
        "run_id",
        "outcome_name",
        "estimator_name",
        "period_name",
        "observations",
        "estimate_value",
        "estimate_pct",
        "lower_bound",
        "upper_bound",
        "estimate_unit",
        "p_value",
        "effect_size",
        "interval_excludes_zero",
        "is_primary_outcome",
    ]
    match_columns = [
        "run_id",
        "treatment_date_key",
        "control_date_key",
        "campaign_year",
        "season",
        "day_of_week",
        "match_distance",
        "treated_net_demand",
        "control_net_demand",
        "demand_lift",
        "treated_net_revenue",
        "control_net_revenue",
        "net_revenue_lift",
        "treated_net_revenue_per_ticket",
        "control_net_revenue_per_ticket",
        "net_revenue_per_ticket_lift",
    ]
    balance_columns = [
        "run_id",
        "covariate_name",
        "raw_smd",
        "matched_smd",
        "balance_threshold",
        "balance_passed",
    ]
    insert_run = text(
        """
        INSERT INTO analytics.fact_campaign_analysis_run (
            run_id, campaign_family, analysis_version,
            treatment_definition, control_definition, primary_outcome,
            treatment_days, control_pool_days, matched_pairs, matching_ratio,
            balance_threshold, matched_balance_passed, bootstrap_samples,
            bootstrap_block_days, marketing_spend,
            attributed_discount_amount, estimated_incremental_net_revenue,
            incremental_revenue_lower_95, incremental_revenue_upper_95,
            estimated_net_revenue_after_marketing,
            associated_revenue_on_marketing_spend,
            associated_return_after_marketing_spend,
            pre_period_revenue_placebo_p_value, causal_claim_allowed, created_at
        ) VALUES (
            :run_id, :campaign_family, :analysis_version,
            :treatment_definition, :control_definition, :primary_outcome,
            :treatment_days, :control_pool_days, :matched_pairs, :matching_ratio,
            :balance_threshold, :matched_balance_passed, :bootstrap_samples,
            :bootstrap_block_days, :marketing_spend,
            :attributed_discount_amount, :estimated_incremental_net_revenue,
            :incremental_revenue_lower_95, :incremental_revenue_upper_95,
            :estimated_net_revenue_after_marketing,
            :associated_revenue_on_marketing_spend,
            :associated_return_after_marketing_spend,
            :pre_period_revenue_placebo_p_value, :causal_claim_allowed, :created_at
        )
        """
    )
    insert_evaluation = text(
        """
        INSERT INTO analytics.fact_campaign_evaluation (
            run_id, outcome_name, estimator_name, period_name, observations,
            estimate_value, estimate_pct, lower_bound, upper_bound,
            estimate_unit, p_value, effect_size, interval_excludes_zero,
            is_primary_outcome
        ) VALUES (
            :run_id, :outcome_name, :estimator_name, :period_name, :observations,
            :estimate_value, :estimate_pct, :lower_bound, :upper_bound,
            :estimate_unit, :p_value, :effect_size, :interval_excludes_zero,
            :is_primary_outcome
        )
        """
    )
    insert_match = text(
        """
        INSERT INTO analytics.fact_campaign_match (
            run_id, treatment_date_key, control_date_key, campaign_year,
            season, day_of_week, match_distance, treated_net_demand,
            control_net_demand, demand_lift, treated_net_revenue,
            control_net_revenue, net_revenue_lift,
            treated_net_revenue_per_ticket, control_net_revenue_per_ticket,
            net_revenue_per_ticket_lift
        ) VALUES (
            :run_id, :treatment_date_key, :control_date_key, :campaign_year,
            :season, :day_of_week, :match_distance, :treated_net_demand,
            :control_net_demand, :demand_lift, :treated_net_revenue,
            :control_net_revenue, :net_revenue_lift,
            :treated_net_revenue_per_ticket, :control_net_revenue_per_ticket,
            :net_revenue_per_ticket_lift
        )
        """
    )
    insert_balance = text(
        """
        INSERT INTO analytics.fact_campaign_balance (
            run_id, covariate_name, raw_smd, matched_smd,
            balance_threshold, balance_passed
        ) VALUES (
            :run_id, :covariate_name, :raw_smd, :matched_smd,
            :balance_threshold, :balance_passed
        )
        """
    )
    run_payload = dataframe_payload(run, run_columns)
    evaluation_payload = dataframe_payload(evaluation, evaluation_columns)
    match_payload = dataframe_payload(matches, match_columns)
    balance_payload = dataframe_payload(balance, balance_columns)
    run_id = run.loc[0, "run_id"]

    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": 2_026_100_8},
        )
        inserted_run = connection.execute(insert_run, run_payload)
        inserted_evaluation = connection.execute(
            insert_evaluation,
            evaluation_payload,
        )
        inserted_matches = connection.execute(insert_match, match_payload)
        inserted_balance = connection.execute(insert_balance, balance_payload)
        inserted_counts = (
            inserted_run.rowcount,
            inserted_evaluation.rowcount,
            inserted_matches.rowcount,
            inserted_balance.rowcount,
        )
        expected_counts = (1, len(evaluation), len(matches), len(balance))
        if inserted_counts != expected_counts:
            raise CampaignAnalysisPipelineError(
                f"Inserted campaign rows {inserted_counts}; expected {expected_counts}."
            )
        persisted_counts = tuple(
            connection.execute(
                text(f"SELECT count(*) FROM analytics.{table} WHERE run_id = :run_id"),
                {"run_id": run_id},
            ).scalar_one()
            for table in (
                "fact_campaign_analysis_run",
                "fact_campaign_evaluation",
                "fact_campaign_match",
                "fact_campaign_balance",
            )
        )
        if persisted_counts != expected_counts:
            raise CampaignAnalysisPipelineError(
                "Persisted campaign row counts failed transactional validation."
            )


# Purpose: Export the latest campaign result grains for human review.
# Used by: run_pipeline after successful database persistence.
def export_analysis_outputs(
    run: pd.DataFrame,
    evaluation: pd.DataFrame,
    matches: pd.DataFrame,
    balance: pd.DataFrame,
    project_root: Path,
) -> tuple[Path, Path, Path, Path]:
    output_dir = project_root / "outputs" / "analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = (
        output_dir / "latest_campaign_summary.csv",
        output_dir / "latest_campaign_evaluation.csv",
        output_dir / "latest_campaign_matches.csv",
        output_dir / "latest_campaign_balance.csv",
    )
    run.to_csv(paths[0], index=False)
    evaluation.to_csv(paths[1], index=False)
    matches.to_csv(paths[2], index=False)
    balance.to_csv(paths[3], index=False)
    return paths


# Purpose: Coordinate loading, estimation, validation, persistence, and export.
# Used by: main.
def run_pipeline(args: argparse.Namespace) -> None:
    engine = create_engine(args.database_url)
    daily = load_daily_campaign_input(engine)
    campaign_costs = load_campaign_cost_input(engine)
    validate_campaign_inputs(daily, campaign_costs)
    treatment, control_pool, paired_daily, balance, evaluation = build_analysis_outputs(
        daily
    )
    records = build_database_records(
        treatment,
        control_pool,
        paired_daily,
        balance,
        evaluation,
        campaign_costs,
        datetime.now(UTC),
    )
    persist_analysis(engine, *records)
    paths = export_analysis_outputs(*records, args.project_root)
    matched_results = records[1].loc[
        records[1]["estimator_name"].eq("matched_block_bootstrap"),
        [
            "outcome_name",
            "estimate_value",
            "estimate_pct",
            "lower_bound",
            "upper_bound",
        ],
    ]
    print("Campaign analysis run:", records[0].loc[0, "run_id"])
    print("Matched pairs persisted:", len(records[2]))
    print("Balance checks passed:", int(records[3]["balance_passed"].sum()))
    print(matched_results.to_string(index=False))
    for path in paths:
        print("CSV review copy:", path)


# Purpose: Provide the command-line entry point and concise failure messages.
# Used by: ``python -m src.analyze_campaign``.
def main() -> None:
    args = parse_args()
    try:
        run_pipeline(args)
    except CampaignAnalysisPipelineError as error:
        raise SystemExit(f"Campaign analysis pipeline failed: {error}") from error


if __name__ == "__main__":
    main()
