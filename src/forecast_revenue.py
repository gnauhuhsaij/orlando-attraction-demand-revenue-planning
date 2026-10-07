"""Backtest and persist the selected product-level revenue forecast.

The demand pipeline remains the source of total-demand predictions. This
module allocates those predictions with a histogram-gradient-boosting product
mix model, predicts product net yield with Ridge regression, calibrates daily
revenue intervals, and stores immutable daily and product-level runs.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sqlalchemy import Engine, create_engine, text

from src.forecast import (
    DEFAULT_DATABASE_URL,
    FORECAST_HORIZON,
    HORIZON_LABELS,
    INTERVAL_ALPHA,
    MINIMUM_CALIBRATION_ORIGINS,
    assign_horizon_bucket,
    conformal_absolute_quantile,
)

MODEL_VERSION = "1.0"
DEMAND_MODEL_NAME = "sarimax_exogenous"
PRODUCT_MIX_MODEL_NAME = "hgb_product_mix"
PRODUCT_YIELD_MODEL_NAME = "ridge_product_yield"
REVENUE_MODEL_NAME = "sarimax_demand_hgb_mix_ridge_product_yield"

PRODUCT_CATEGORICAL_FEATURES = (
    "product_code",
    "ticket_tier",
    "day_of_week",
    "month_number",
    "season",
)
PRODUCT_NUMERIC_FEATURES = (
    "trend_days",
    "base_price",
    "is_weekend",
    "holiday_flag",
    "school_break_flag",
    "planned_price_multiplier",
    "planned_unit_list_price",
    "active_campaign_count",
    "maximum_planned_discount",
    "paid_media_flag",
    "email_campaign_flag",
    "bundle_campaign_flag",
    "discount_campaign_flag",
)


class RevenueForecastPipelineError(RuntimeError):
    """Raised when revenue input, model output, or persistence is invalid."""


# Purpose: Parse repeatable revenue-pipeline settings without credentials.
# Used by: main.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest and persist the selected revenue forecast."
    )
    parser.add_argument(
        "mode",
        choices=("backtest", "forecast", "all"),
        help=(
            "backtest stores the selected historical pipeline; forecast stores "
            "the selected 30-day production run; all performs both."
        ),
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
        help="Repository root used for forecast review exports.",
    )
    return parser.parse_args()


# Purpose: Read the product-date modeling contract from PostgreSQL.
# Used by: run_pipeline.
def load_product_revenue_input(engine: Engine) -> pd.DataFrame:
    query = text(
        """
        SELECT
            product.date_key,
            product.calendar_date,
            product.product_key,
            product.product_code,
            product.product_name,
            product.ticket_tier,
            product.base_price,
            product.day_of_week,
            product.month_number,
            product.year_number,
            product.is_weekend,
            product.holiday_flag,
            product.school_break_flag,
            product.season,
            product.planned_price_multiplier,
            product.planned_unit_list_price,
            forecast.active_campaign_count,
            forecast.maximum_planned_discount,
            forecast.paid_media_flag,
            forecast.email_campaign_flag,
            forecast.bundle_campaign_flag,
            forecast.discount_campaign_flag,
            product.net_demand AS target_product_demand,
            product.actual_product_demand_share AS target_product_share,
            product.net_revenue AS target_product_net_revenue,
            product.actual_net_revenue_per_ticket AS target_product_net_yield
        FROM analytics.vw_daily_product_performance AS product
        JOIN analytics.vw_forecast_input AS forecast USING (date_key)
        ORDER BY product.calendar_date, product.product_key
        """
    )
    with engine.connect() as connection:
        return pd.read_sql_query(
            query,
            connection,
            parse_dates=["calendar_date"],
        )


# Purpose: Load the newest persisted selected-demand predictions for one run type.
# Used by: run_pipeline before product modeling.
def load_demand_predictions(engine: Engine, run_type: str) -> pd.DataFrame:
    if run_type not in {"backtest", "production"}:
        raise RevenueForecastPipelineError(f"Unsupported run type: {run_type}")
    query = text(
        """
        WITH latest_batch AS (
            SELECT max(created_at) AS created_at
            FROM analytics.fact_forecast
            WHERE forecast_run_type = :run_type
              AND model_name = :model_name
        )
        SELECT
            created_date.calendar_date AS forecast_created_date,
            training_date.calendar_date AS training_end_date,
            target_date.calendar_date AS target_date,
            forecast.forecast_horizon_days AS horizon_days,
            forecast.predicted_demand::double precision AS predicted_demand,
            forecast.actual_demand::double precision AS actual_demand
        FROM analytics.fact_forecast AS forecast
        JOIN latest_batch AS latest
          ON forecast.created_at = latest.created_at
        JOIN analytics.dim_date AS created_date
          ON created_date.date_key = forecast.forecast_created_date_key
        JOIN analytics.dim_date AS training_date
          ON training_date.date_key = forecast.training_end_date_key
        JOIN analytics.dim_date AS target_date
          ON target_date.date_key = forecast.target_date_key
        WHERE forecast.forecast_run_type = :run_type
          AND forecast.model_name = :model_name
        ORDER BY forecast_created_date, target_date
        """
    )
    with engine.connect() as connection:
        predictions = pd.read_sql_query(
            query,
            connection,
            params={"run_type": run_type, "model_name": DEMAND_MODEL_NAME},
            parse_dates=[
                "forecast_created_date",
                "training_end_date",
                "target_date",
            ],
        )
    if predictions.empty:
        raise RevenueForecastPipelineError(
            f"No persisted {run_type} {DEMAND_MODEL_NAME} predictions were found."
        )
    return predictions


# Purpose: Retrieve final-revenue errors when production runs without backtesting.
# Used by: run_pipeline in forecast mode.
def load_latest_revenue_backtests(engine: Engine) -> pd.DataFrame:
    query = text(
        """
        WITH latest_batch AS (
            SELECT max(created_at) AS created_at
            FROM analytics.fact_revenue_forecast
            WHERE forecast_run_type = 'backtest'
              AND revenue_model_name = :model_name
        )
        SELECT
            created_date.calendar_date AS forecast_created_date,
            target_date.calendar_date AS target_date,
            forecast.forecast_horizon_days AS horizon_days,
            forecast.predicted_net_revenue::double precision
                AS predicted_net_revenue,
            forecast.actual_net_revenue::double precision
                AS actual_net_revenue
        FROM analytics.fact_revenue_forecast AS forecast
        JOIN latest_batch AS latest
          ON forecast.created_at = latest.created_at
        JOIN analytics.dim_date AS created_date
          ON created_date.date_key = forecast.forecast_created_date_key
        JOIN analytics.dim_date AS target_date
          ON target_date.date_key = forecast.target_date_key
        WHERE forecast.forecast_run_type = 'backtest'
          AND forecast.revenue_model_name = :model_name
        ORDER BY forecast_created_date, target_date
        """
    )
    with engine.connect() as connection:
        backtests = pd.read_sql_query(
            query,
            connection,
            params={"model_name": REVENUE_MODEL_NAME},
            parse_dates=["forecast_created_date", "target_date"],
        )
    if backtests.empty:
        raise RevenueForecastPipelineError(
            "No revenue backtests were found. Run forecast_revenue.py backtest first."
        )
    return backtests


# Purpose: Fail early when the product-date modeling grain is incomplete.
# Used by: run_pipeline and unit tests.
def validate_product_revenue_input(
    data: pd.DataFrame,
    forecast_horizon: int = FORECAST_HORIZON,
) -> None:
    required = {
        "date_key",
        "calendar_date",
        "product_key",
        "target_product_demand",
        "target_product_share",
        "target_product_net_revenue",
        "target_product_net_yield",
        *PRODUCT_CATEGORICAL_FEATURES,
        *PRODUCT_NUMERIC_FEATURES[1:],
    }
    missing = sorted(required.difference(data.columns))
    if missing:
        raise RevenueForecastPipelineError(
            f"Product revenue input is missing columns: {', '.join(missing)}"
        )
    if data.empty:
        raise RevenueForecastPipelineError("Product revenue input is empty.")
    if data.duplicated(["calendar_date", "product_key"]).any():
        raise RevenueForecastPipelineError("Product revenue input has duplicate keys.")
    products_per_date = data.groupby("calendar_date")["product_key"].nunique()
    if products_per_date.nunique() != 1:
        raise RevenueForecastPipelineError("Product coverage varies by date.")
    product_count = int(products_per_date.iloc[0])
    if product_count < 2:
        raise RevenueForecastPipelineError("At least two products are required.")
    history = data.loc[data["target_product_demand"].notna()]
    future = data.loc[data["target_product_demand"].isna()]
    if history.empty:
        raise RevenueForecastPipelineError("Product revenue history is empty.")
    if future["calendar_date"].nunique() != forecast_horizon:
        raise RevenueForecastPipelineError("Product future horizon is incomplete.")
    historical_targets = (
        "target_product_share",
        "target_product_net_revenue",
        "target_product_net_yield",
    )
    if history[list(historical_targets)].isna().any().any():
        raise RevenueForecastPipelineError("Historical product targets are missing.")
    if future[list(historical_targets)].notna().any().any():
        raise RevenueForecastPipelineError("Future product actuals are populated.")
    share_totals = history.groupby("calendar_date")["target_product_share"].sum()
    if not np.allclose(share_totals.astype(float), 1.0, atol=1e-5):
        raise RevenueForecastPipelineError(
            "Historical product shares do not sum to one."
        )


# Purpose: Prove demand runs have one complete 30-day horizon per origin.
# Used by: run_pipeline and unit tests.
def validate_demand_predictions(
    predictions: pd.DataFrame,
    run_type: str,
    forecast_horizon: int = FORECAST_HORIZON,
) -> None:
    required = {
        "forecast_created_date",
        "training_end_date",
        "target_date",
        "horizon_days",
        "predicted_demand",
        "actual_demand",
    }
    missing = sorted(required.difference(predictions.columns))
    if missing:
        raise RevenueForecastPipelineError(
            f"Demand predictions are missing columns: {', '.join(missing)}"
        )
    keys = ["forecast_created_date", "target_date"]
    if predictions.empty or predictions.duplicated(keys).any():
        raise RevenueForecastPipelineError("Demand prediction keys are invalid.")
    counts = predictions.groupby("forecast_created_date").size()
    if not counts.eq(forecast_horizon).all():
        raise RevenueForecastPipelineError("A demand run lacks a full horizon.")
    expected_horizon = (
        predictions["target_date"] - predictions["forecast_created_date"]
    ).dt.days
    if not expected_horizon.eq(predictions["horizon_days"]).all():
        raise RevenueForecastPipelineError("Demand forecast horizon is incorrect.")
    if (
        predictions["predicted_demand"].isna().any()
        or predictions["predicted_demand"].lt(0).any()
    ):
        raise RevenueForecastPipelineError("Predicted demand is missing or negative.")
    if run_type == "backtest" and predictions["actual_demand"].isna().any():
        raise RevenueForecastPipelineError("Backtest demand actuals are missing.")
    if run_type == "production":
        if predictions["forecast_created_date"].nunique() != 1:
            raise RevenueForecastPipelineError("Production has multiple origins.")
        if predictions["actual_demand"].notna().any():
            raise RevenueForecastPipelineError("Production demand contains actuals.")


# Purpose: Convert product-date fields into the common model feature matrix.
# Used by: product-mix and product-yield model training and scoring.
def prepare_product_features(
    data: pd.DataFrame,
    series_start_date: pd.Timestamp,
) -> pd.DataFrame:
    prepared = data.copy()
    prepared["trend_days"] = (
        prepared["calendar_date"] - pd.Timestamp(series_start_date)
    ).dt.days
    for column in PRODUCT_CATEGORICAL_FEATURES:
        prepared[column] = prepared[column].astype(str)
    for column in PRODUCT_NUMERIC_FEATURES:
        prepared[column] = pd.to_numeric(prepared[column], errors="raise").astype(float)
    return prepared[[*PRODUCT_CATEGORICAL_FEATURES, *PRODUCT_NUMERIC_FEATURES]]


# Purpose: Build an unfitted transformer for the shared product feature contract.
# Used by: build_product_mix_model and build_product_yield_model.
def make_product_preprocessor() -> ColumnTransformer:
    return ColumnTransformer(
        transformers=[
            (
                "categorical",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                list(PRODUCT_CATEGORICAL_FEATURES),
            ),
            (
                "numeric",
                StandardScaler(),
                list(PRODUCT_NUMERIC_FEATURES),
            ),
        ]
    )


# Purpose: Create the selected nonlinear product-share model.
# Used by: predict_product_components.
def build_product_mix_model() -> Pipeline:
    return Pipeline(
        steps=[
            ("preprocessing", make_product_preprocessor()),
            (
                "model",
                HistGradientBoostingRegressor(
                    learning_rate=0.05,
                    max_iter=300,
                    max_leaf_nodes=15,
                    min_samples_leaf=30,
                    l2_regularization=1.0,
                    early_stopping=False,
                    random_state=42,
                ),
            ),
        ]
    )


# Purpose: Create the selected interpretable product-net-yield model.
# Used by: predict_product_components.
def build_product_yield_model() -> Pipeline:
    return Pipeline(
        steps=[
            ("preprocessing", make_product_preprocessor()),
            ("model", Ridge(alpha=1.0)),
        ]
    )


# Purpose: Convert raw model output into nonnegative shares summing to one.
# Used by: predict_product_components and unit tests.
def normalize_product_shares(
    predictions: pd.DataFrame,
    raw_shares: np.ndarray,
) -> pd.DataFrame:
    normalized = predictions.copy()
    normalized["predicted_product_share"] = np.clip(
        np.asarray(raw_shares, dtype=float), 1e-6, None
    )
    group_keys = ["forecast_created_date", "target_date"]
    totals = normalized.groupby(group_keys)["predicted_product_share"].transform("sum")
    normalized["predicted_product_share"] /= totals
    normalized["predicted_product_demand"] = (
        normalized["predicted_demand"] * normalized["predicted_product_share"]
    )
    return normalized


# Purpose: Fit the selected mix and yield models for one forecast origin.
# Used by: run_product_backtest and build_production_revenue_forecast.
def predict_product_components(
    product_data: pd.DataFrame,
    demand_fold: pd.DataFrame,
    mix_model_builder: Callable[[], Pipeline] = build_product_mix_model,
    yield_model_builder: Callable[[], Pipeline] = build_product_yield_model,
) -> pd.DataFrame:
    cutoff = pd.Timestamp(demand_fold["forecast_created_date"].iloc[0])
    if not demand_fold["forecast_created_date"].eq(cutoff).all():
        raise RevenueForecastPipelineError("A product fold contains multiple origins.")
    training = product_data.loc[
        product_data["calendar_date"].le(cutoff)
        & product_data["target_product_demand"].notna()
        & product_data["target_product_net_yield"].notna()
    ].copy()
    scoring = (
        product_data.loc[product_data["calendar_date"].isin(demand_fold["target_date"])]
        .rename(columns={"calendar_date": "target_date"})
        .merge(
            demand_fold,
            on="target_date",
            how="inner",
            validate="many_to_one",
        )
    )
    if training.empty or scoring.empty:
        raise RevenueForecastPipelineError("Product training or scoring data is empty.")
    if training["calendar_date"].max() > cutoff:
        raise RevenueForecastPipelineError("Product training extends past the origin.")
    series_start = product_data["calendar_date"].min()
    training_features = prepare_product_features(training, series_start)
    scoring_features = prepare_product_features(
        scoring.rename(columns={"target_date": "calendar_date"}),
        series_start,
    )

    mix_model = mix_model_builder()
    mix_model.fit(
        training_features,
        training["target_product_share"].astype(float),
    )
    scored = normalize_product_shares(
        scoring,
        mix_model.predict(scoring_features),
    )

    yield_model = yield_model_builder()
    yield_model.fit(
        training_features,
        training["target_product_net_yield"].astype(float),
    )
    scored["predicted_product_net_yield"] = np.maximum(
        yield_model.predict(scoring_features), 0.01
    )
    scored["predicted_product_net_revenue"] = (
        scored["predicted_product_demand"] * scored["predicted_product_net_yield"]
    )
    scored["product_mix_model_name"] = PRODUCT_MIX_MODEL_NAME
    scored["product_yield_model_name"] = PRODUCT_YIELD_MODEL_NAME
    return scored


# Purpose: Run the selected product models for every persisted demand origin.
# Used by: run_pipeline.
def run_product_forecasts(
    product_data: pd.DataFrame,
    demand_predictions: pd.DataFrame,
) -> pd.DataFrame:
    folds: list[pd.DataFrame] = []
    for origin in sorted(demand_predictions["forecast_created_date"].unique()):
        demand_fold = demand_predictions.loc[
            demand_predictions["forecast_created_date"].eq(origin)
        ].copy()
        folds.append(predict_product_components(product_data, demand_fold))
    product_predictions = pd.concat(folds, ignore_index=True)
    product_count = product_data["product_key"].nunique()
    expected_rows = len(demand_predictions) * product_count
    if len(product_predictions) != expected_rows:
        raise RevenueForecastPipelineError(
            f"Product models produced {len(product_predictions)} rows; "
            f"expected {expected_rows}."
        )
    share_totals = product_predictions.groupby(
        ["forecast_created_date", "target_date"]
    )["predicted_product_share"].sum()
    if not np.allclose(share_totals, 1.0):
        raise RevenueForecastPipelineError(
            "Predicted product shares do not sum to one."
        )
    return product_predictions


# Purpose: Reconcile product components into one daily revenue forecast.
# Used by: run_pipeline and unit tests.
def aggregate_daily_revenue(product_predictions: pd.DataFrame) -> pd.DataFrame:
    group_keys = [
        "forecast_created_date",
        "training_end_date",
        "target_date",
        "horizon_days",
    ]
    daily = (
        product_predictions.groupby(group_keys, as_index=False)
        .agg(
            predicted_demand=("predicted_demand", "first"),
            actual_demand=("actual_demand", "first"),
            predicted_net_revenue=("predicted_product_net_revenue", "sum"),
            actual_net_revenue=(
                "target_product_net_revenue",
                lambda values: values.sum(min_count=1),
            ),
        )
        .sort_values(["forecast_created_date", "target_date"])
        .reset_index(drop=True)
    )
    daily["revenue_model_name"] = REVENUE_MODEL_NAME
    daily["model_version"] = MODEL_VERSION
    daily["demand_model_name"] = DEMAND_MODEL_NAME
    daily["product_mix_model_name"] = PRODUCT_MIX_MODEL_NAME
    daily["product_yield_model_name"] = PRODUCT_YIELD_MODEL_NAME
    return daily


# Purpose: Calculate the selected daily revenue model's comparable metrics.
# Used by: run_pipeline summary and tests.
def calculate_revenue_metrics(predictions: pd.DataFrame) -> pd.Series:
    actual = predictions["actual_net_revenue"].astype(float)
    predicted = predictions["predicted_net_revenue"].astype(float)
    error = predicted - actual
    nonzero_actual = actual.ne(0)
    return pd.Series(
        {
            "observations": len(predictions),
            "mae": error.abs().mean(),
            "rmse": error.pow(2).mean() ** 0.5,
            "mape_pct": (
                error.loc[nonzero_actual].abs() / actual.loc[nonzero_actual]
            ).mean()
            * 100,
            "wape_pct": error.abs().sum() / actual.abs().sum() * 100,
            "forecast_bias": error.mean(),
            "forecast_bias_pct": error.sum() / actual.sum() * 100,
        }
    )


# Purpose: Calibrate backtest intervals with earlier revenue origins only.
# Used by: run_pipeline before backtest persistence and unit tests.
def calibrate_revenue_backtest_intervals(
    predictions: pd.DataFrame,
) -> pd.DataFrame:
    calibrated = predictions.copy()
    calibrated["horizon_bucket"] = assign_horizon_bucket(calibrated["horizon_days"])
    calibrated["absolute_error"] = (
        calibrated["actual_net_revenue"] - calibrated["predicted_net_revenue"]
    ).abs()
    calibrated["lower_bound"] = np.nan
    calibrated["upper_bound"] = np.nan
    calibrated["interval_confidence"] = np.nan
    calibrated["interval_method"] = None
    calibrated["calibration_observations"] = np.nan
    origins = sorted(calibrated["forecast_created_date"].unique())
    for origin_number, origin in enumerate(origins):
        if origin_number < MINIMUM_CALIBRATION_ORIGINS:
            continue
        prior = calibrated.loc[calibrated["forecast_created_date"].lt(origin)]
        for horizon_bucket in HORIZON_LABELS:
            errors = prior.loc[
                prior["horizon_bucket"].eq(horizon_bucket), "absolute_error"
            ]
            quantile = conformal_absolute_quantile(errors, alpha=INTERVAL_ALPHA)
            mask = calibrated["forecast_created_date"].eq(origin) & calibrated[
                "horizon_bucket"
            ].eq(horizon_bucket)
            calibrated.loc[mask, "lower_bound"] = np.maximum(
                calibrated.loc[mask, "predicted_net_revenue"] - quantile, 0
            )
            calibrated.loc[mask, "upper_bound"] = (
                calibrated.loc[mask, "predicted_net_revenue"] + quantile
            )
            calibrated.loc[mask, "interval_confidence"] = 1 - INTERVAL_ALPHA
            calibrated.loc[mask, "interval_method"] = "time_ordered_horizon_conformal"
            calibrated.loc[mask, "calibration_observations"] = len(errors)
    return calibrated


# Purpose: Use every retained final-model error for the production interval.
# Used by: run_pipeline before production persistence and unit tests.
def calibrate_production_revenue_intervals(
    forecast: pd.DataFrame,
    backtests: pd.DataFrame,
) -> pd.DataFrame:
    calibrated = forecast.copy()
    calibration = backtests.copy()
    calibrated["horizon_bucket"] = assign_horizon_bucket(calibrated["horizon_days"])
    calibration["horizon_bucket"] = assign_horizon_bucket(calibration["horizon_days"])
    calibration["absolute_error"] = (
        calibration["actual_net_revenue"] - calibration["predicted_net_revenue"]
    ).abs()
    calibrated["lower_bound"] = np.nan
    calibrated["upper_bound"] = np.nan
    calibrated["interval_confidence"] = 1 - INTERVAL_ALPHA
    calibrated["interval_method"] = "horizon_bucket_conformal"
    calibrated["calibration_observations"] = 0
    for horizon_bucket in HORIZON_LABELS:
        errors = calibration.loc[
            calibration["horizon_bucket"].eq(horizon_bucket), "absolute_error"
        ]
        quantile = conformal_absolute_quantile(errors, alpha=INTERVAL_ALPHA)
        mask = calibrated["horizon_bucket"].eq(horizon_bucket)
        calibrated.loc[mask, "lower_bound"] = np.maximum(
            calibrated.loc[mask, "predicted_net_revenue"] - quantile, 0
        )
        calibrated.loc[mask, "upper_bound"] = (
            calibrated.loc[mask, "predicted_net_revenue"] + quantile
        )
        calibrated.loc[mask, "calibration_observations"] = len(errors)
    if calibrated[["lower_bound", "upper_bound"]].isna().any().any():
        raise RevenueForecastPipelineError("Production revenue intervals are missing.")
    return calibrated


# Purpose: Attach stable run IDs, date keys, and database-safe numeric values.
# Used by: run_pipeline before validation and persistence.
def build_database_records(
    daily_predictions: pd.DataFrame,
    product_predictions: pd.DataFrame,
    run_type: str,
    execution_time: datetime,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily = daily_predictions.copy()
    products = product_predictions.copy()
    execution_id = execution_time.strftime("%Y%m%dT%H%M%S%fZ")
    prefix = "bt_rev" if run_type == "backtest" else "prod_rev"
    origins = pd.to_datetime(daily["forecast_created_date"].unique())
    run_ids = {
        origin: f"{prefix}_{origin.strftime('%Y%m%d')}_{execution_id}"
        for origin in origins
    }
    daily["run_id"] = daily["forecast_created_date"].map(run_ids)
    products["run_id"] = products["forecast_created_date"].map(run_ids)
    daily["forecast_run_type"] = run_type
    daily["model_role"] = "selected"
    daily["forecast_created_date_key"] = (
        daily["forecast_created_date"].dt.strftime("%Y%m%d").astype(int)
    )
    daily["training_end_date_key"] = (
        daily["training_end_date"].dt.strftime("%Y%m%d").astype(int)
    )
    daily["target_date_key"] = daily["target_date"].dt.strftime("%Y%m%d").astype(int)
    products["target_date_key"] = (
        products["target_date"].dt.strftime("%Y%m%d").astype(int)
    )
    actual_loaded_at = execution_time if run_type == "backtest" else None
    daily["actual_loaded_at"] = actual_loaded_at
    products["actual_loaded_at"] = actual_loaded_at

    for column in (
        "lower_bound",
        "upper_bound",
        "interval_confidence",
        "interval_method",
        "calibration_observations",
    ):
        if column not in daily:
            daily[column] = None
    for column in (
        "predicted_demand",
        "predicted_net_revenue",
        "lower_bound",
        "upper_bound",
        "actual_net_revenue",
    ):
        daily[column] = pd.to_numeric(daily[column], errors="coerce").round(2)
    daily["actual_demand"] = daily["actual_demand"].map(
        lambda value: None if pd.isna(value) else int(value)
    )
    daily["calibration_observations"] = daily["calibration_observations"].map(
        lambda value: None if pd.isna(value) else int(value)
    )
    daily["interval_confidence"] = daily["interval_confidence"].map(
        lambda value: None if pd.isna(value) else float(value)
    )

    product_renames = {
        "target_product_share": "actual_product_share",
        "target_product_demand": "actual_product_demand",
        "target_product_net_yield": "actual_product_net_yield",
        "target_product_net_revenue": "actual_product_net_revenue",
    }
    products = products.rename(columns=product_renames)
    product_rounding = {
        "predicted_product_share": 8,
        "predicted_product_demand": 4,
        "predicted_product_net_yield": 4,
        "predicted_product_net_revenue": 2,
        "actual_product_share": 8,
        "actual_product_net_yield": 4,
        "actual_product_net_revenue": 2,
    }
    for column, decimals in product_rounding.items():
        products[column] = pd.to_numeric(products[column], errors="coerce").round(
            decimals
        )
    products["actual_product_demand"] = products["actual_product_demand"].map(
        lambda value: None if pd.isna(value) else int(value)
    )
    return daily, products


# Purpose: Check daily and product records plus their cross-grain reconciliation.
# Used by: persist_forecasts and unit tests.
def validate_database_records(
    daily: pd.DataFrame,
    products: pd.DataFrame,
) -> None:
    daily_required = {
        "run_id",
        "forecast_run_type",
        "forecast_created_date_key",
        "training_end_date_key",
        "target_date_key",
        "revenue_model_name",
        "model_version",
        "model_role",
        "demand_model_name",
        "product_mix_model_name",
        "product_yield_model_name",
        "horizon_days",
        "predicted_demand",
        "predicted_net_revenue",
        "lower_bound",
        "upper_bound",
        "interval_confidence",
        "interval_method",
        "calibration_observations",
        "actual_demand",
        "actual_net_revenue",
        "actual_loaded_at",
    }
    product_required = {
        "run_id",
        "target_date_key",
        "product_key",
        "predicted_product_share",
        "predicted_product_demand",
        "predicted_product_net_yield",
        "predicted_product_net_revenue",
        "actual_product_share",
        "actual_product_demand",
        "actual_product_net_yield",
        "actual_product_net_revenue",
        "actual_loaded_at",
    }
    missing_daily = sorted(daily_required.difference(daily.columns))
    missing_product = sorted(product_required.difference(products.columns))
    if missing_daily or missing_product:
        raise RevenueForecastPipelineError(
            "Database records are missing columns: "
            + ", ".join([*missing_daily, *missing_product])
        )
    if daily.empty or products.empty:
        raise RevenueForecastPipelineError("Revenue database records are empty.")
    if daily.duplicated(["run_id", "target_date_key"]).any():
        raise RevenueForecastPipelineError("Daily revenue targets are duplicated.")
    if products.duplicated(["run_id", "target_date_key", "product_key"]).any():
        raise RevenueForecastPipelineError("Product revenue targets are duplicated.")
    if daily["run_id"].str.len().gt(64).any():
        raise RevenueForecastPipelineError("A revenue run_id exceeds 64 characters.")
    calculated_horizon = (
        pd.to_datetime(daily["target_date_key"].astype(str))
        - pd.to_datetime(daily["forecast_created_date_key"].astype(str))
    ).dt.days
    if not calculated_horizon.eq(daily["horizon_days"]).all():
        raise RevenueForecastPipelineError("A revenue forecast horizon is incorrect.")
    if daily[["predicted_demand", "predicted_net_revenue"]].isna().any().any():
        raise RevenueForecastPipelineError("A daily revenue prediction is missing.")
    if daily[["predicted_demand", "predicted_net_revenue"]].lt(0).any().any():
        raise RevenueForecastPipelineError("A daily revenue prediction is negative.")
    interval_fields = [
        "lower_bound",
        "upper_bound",
        "interval_confidence",
        "interval_method",
        "calibration_observations",
    ]
    interval_completeness = daily[interval_fields].notna().sum(axis=1)
    if not interval_completeness.isin([0, len(interval_fields)]).all():
        raise RevenueForecastPipelineError("Revenue interval metadata is incomplete.")
    with_interval = interval_completeness.eq(len(interval_fields))
    if not (
        daily.loc[with_interval, "lower_bound"]
        .le(daily.loc[with_interval, "predicted_net_revenue"])
        .all()
        and daily.loc[with_interval, "predicted_net_revenue"]
        .le(daily.loc[with_interval, "upper_bound"])
        .all()
    ):
        raise RevenueForecastPipelineError("Revenue point forecast is outside bounds.")
    production = daily["forecast_run_type"].eq("production")
    if production.any() and not with_interval.loc[production].all():
        raise RevenueForecastPipelineError("Production revenue intervals are required.")
    backtest = daily["forecast_run_type"].eq("backtest")
    if (
        backtest.any()
        and daily.loc[backtest, ["actual_demand", "actual_net_revenue"]]
        .isna()
        .any()
        .any()
    ):
        raise RevenueForecastPipelineError("Backtest revenue actuals are required.")

    reconciliation = (
        products.groupby(["run_id", "target_date_key"], as_index=False)
        .agg(
            product_rows=("product_key", "size"),
            predicted_share=("predicted_product_share", "sum"),
            predicted_demand=("predicted_product_demand", "sum"),
            predicted_net_revenue=("predicted_product_net_revenue", "sum"),
            actual_demand=(
                "actual_product_demand",
                lambda values: values.sum(min_count=1),
            ),
            actual_net_revenue=(
                "actual_product_net_revenue",
                lambda values: values.sum(min_count=1),
            ),
        )
        .merge(
            daily[
                [
                    "run_id",
                    "target_date_key",
                    "predicted_demand",
                    "predicted_net_revenue",
                    "actual_demand",
                    "actual_net_revenue",
                ]
            ],
            on=["run_id", "target_date_key"],
            suffixes=("_product", "_daily"),
            validate="one_to_one",
        )
    )
    if reconciliation["product_rows"].nunique() != 1:
        raise RevenueForecastPipelineError("Product row counts vary by target.")
    if not np.allclose(reconciliation["predicted_share"], 1.0, atol=1e-6):
        raise RevenueForecastPipelineError("Stored product shares do not sum to one.")
    if not np.allclose(
        reconciliation["predicted_demand_product"],
        reconciliation["predicted_demand_daily"],
        atol=0.02,
    ):
        raise RevenueForecastPipelineError("Product demand does not reconcile.")
    if not np.allclose(
        reconciliation["predicted_net_revenue_product"],
        reconciliation["predicted_net_revenue_daily"],
        atol=0.05,
    ):
        raise RevenueForecastPipelineError("Product revenue does not reconcile.")
    actual_rows = reconciliation["actual_demand_daily"].notna()
    if actual_rows.any() and not (
        np.allclose(
            reconciliation.loc[actual_rows, "actual_demand_product"],
            reconciliation.loc[actual_rows, "actual_demand_daily"],
        )
        and np.allclose(
            reconciliation.loc[actual_rows, "actual_net_revenue_product"],
            reconciliation.loc[actual_rows, "actual_net_revenue_daily"],
            atol=0.01,
        )
    ):
        raise RevenueForecastPipelineError("Product actuals do not reconcile.")


# Purpose: Append daily and product forecasts atomically and verify row counts.
# Used by: run_pipeline.
def persist_forecasts(
    engine: Engine,
    daily: pd.DataFrame,
    products: pd.DataFrame,
) -> None:
    validate_database_records(daily, products)
    daily_statement = text(
        """
        INSERT INTO analytics.fact_revenue_forecast (
            run_id,
            forecast_run_type,
            forecast_created_date_key,
            training_end_date_key,
            target_date_key,
            revenue_model_name,
            model_version,
            model_role,
            demand_model_name,
            product_mix_model_name,
            product_yield_model_name,
            forecast_horizon_days,
            predicted_demand,
            predicted_net_revenue,
            lower_bound,
            upper_bound,
            interval_confidence,
            interval_method,
            calibration_observations,
            actual_demand,
            actual_net_revenue,
            actual_loaded_at
        ) VALUES (
            :run_id,
            :forecast_run_type,
            :forecast_created_date_key,
            :training_end_date_key,
            :target_date_key,
            :revenue_model_name,
            :model_version,
            :model_role,
            :demand_model_name,
            :product_mix_model_name,
            :product_yield_model_name,
            :horizon_days,
            :predicted_demand,
            :predicted_net_revenue,
            :lower_bound,
            :upper_bound,
            :interval_confidence,
            :interval_method,
            :calibration_observations,
            :actual_demand,
            :actual_net_revenue,
            :actual_loaded_at
        )
        """
    )
    product_statement = text(
        """
        INSERT INTO analytics.fact_product_revenue_forecast (
            run_id,
            target_date_key,
            product_key,
            predicted_product_share,
            predicted_product_demand,
            predicted_product_net_yield,
            predicted_product_net_revenue,
            actual_product_share,
            actual_product_demand,
            actual_product_net_yield,
            actual_product_net_revenue,
            actual_loaded_at
        ) VALUES (
            :run_id,
            :target_date_key,
            :product_key,
            :predicted_product_share,
            :predicted_product_demand,
            :predicted_product_net_yield,
            :predicted_product_net_revenue,
            :actual_product_share,
            :actual_product_demand,
            :actual_product_net_yield,
            :actual_product_net_revenue,
            :actual_loaded_at
        )
        """
    )
    daily_columns = [
        "run_id",
        "forecast_run_type",
        "forecast_created_date_key",
        "training_end_date_key",
        "target_date_key",
        "revenue_model_name",
        "model_version",
        "model_role",
        "demand_model_name",
        "product_mix_model_name",
        "product_yield_model_name",
        "horizon_days",
        "predicted_demand",
        "predicted_net_revenue",
        "lower_bound",
        "upper_bound",
        "interval_confidence",
        "interval_method",
        "calibration_observations",
        "actual_demand",
        "actual_net_revenue",
        "actual_loaded_at",
    ]
    product_columns = [
        "run_id",
        "target_date_key",
        "product_key",
        "predicted_product_share",
        "predicted_product_demand",
        "predicted_product_net_yield",
        "predicted_product_net_revenue",
        "actual_product_share",
        "actual_product_demand",
        "actual_product_net_yield",
        "actual_product_net_revenue",
        "actual_loaded_at",
    ]
    daily_payload_frame = daily[daily_columns].astype(object)
    daily_payload_frame = daily_payload_frame.where(pd.notna(daily_payload_frame), None)
    product_payload_frame = products[product_columns].astype(object)
    product_payload_frame = product_payload_frame.where(
        pd.notna(product_payload_frame), None
    )
    daily_payload = daily_payload_frame.to_dict(orient="records")
    product_payload = product_payload_frame.to_dict(orient="records")
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": 2_026_100_7},
        )
        daily_result = connection.execute(daily_statement, daily_payload)
        product_result = connection.execute(product_statement, product_payload)
        if daily_result.rowcount != len(daily) or product_result.rowcount != len(
            products
        ):
            raise RevenueForecastPipelineError(
                "Persisted revenue row counts do not match model output."
            )
        for run_id, expected_daily in daily.groupby("run_id").size().items():
            actual_daily = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM analytics.fact_revenue_forecast
                    WHERE run_id = :run_id
                    """
                ),
                {"run_id": run_id},
            ).scalar_one()
            expected_products = int(products["run_id"].eq(run_id).sum())
            actual_products = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM analytics.fact_product_revenue_forecast
                    WHERE run_id = :run_id
                    """
                ),
                {"run_id": run_id},
            ).scalar_one()
            if actual_daily != expected_daily or actual_products != expected_products:
                raise RevenueForecastPipelineError(
                    f"Persisted revenue run {run_id} failed row-count validation."
                )


# Purpose: Export latest daily and product production runs for human review.
# Used by: run_pipeline after production persistence.
def export_production_forecasts(
    daily: pd.DataFrame,
    products: pd.DataFrame,
    project_root: Path,
) -> tuple[Path, Path]:
    output_dir = project_root / "outputs" / "forecasts"
    output_dir.mkdir(parents=True, exist_ok=True)
    daily_path = output_dir / "latest_revenue_forecast.csv"
    product_path = output_dir / "latest_product_revenue_forecast.csv"
    daily_columns = [
        "forecast_created_date",
        "target_date",
        "horizon_days",
        "revenue_model_name",
        "predicted_demand",
        "predicted_net_revenue",
        "lower_bound",
        "upper_bound",
        "interval_confidence",
        "interval_method",
        "calibration_observations",
    ]
    product_columns = [
        "forecast_created_date",
        "target_date",
        "horizon_days",
        "product_code",
        "predicted_product_share",
        "predicted_product_demand",
        "predicted_product_net_yield",
        "predicted_product_net_revenue",
    ]
    daily[daily_columns].to_csv(daily_path, index=False)
    products[product_columns].to_csv(product_path, index=False)
    return daily_path, product_path


# Purpose: Coordinate selected-model backtesting, forecasting, and persistence.
# Used by: main.
def run_pipeline(args: argparse.Namespace) -> None:
    engine = create_engine(args.database_url)
    product_data = load_product_revenue_input(engine)
    validate_product_revenue_input(product_data)
    execution_time = datetime.now(UTC)
    revenue_backtests: pd.DataFrame | None = None

    if args.mode in {"backtest", "all"}:
        demand_backtests = load_demand_predictions(engine, "backtest")
        validate_demand_predictions(demand_backtests, "backtest")
        product_backtests = run_product_forecasts(product_data, demand_backtests)
        revenue_backtests = calibrate_revenue_backtest_intervals(
            aggregate_daily_revenue(product_backtests)
        )
        daily_records, product_records = build_database_records(
            revenue_backtests,
            product_backtests,
            "backtest",
            execution_time,
        )
        persist_forecasts(engine, daily_records, product_records)
        metrics = calculate_revenue_metrics(revenue_backtests).round(2)
        print("Revenue backtest rows persisted:", len(daily_records))
        print("Product backtest rows persisted:", len(product_records))
        print(metrics.to_string())

    if args.mode in {"forecast", "all"}:
        demand_production = load_demand_predictions(engine, "production")
        validate_demand_predictions(demand_production, "production")
        product_production = run_product_forecasts(product_data, demand_production)
        revenue_production = aggregate_daily_revenue(product_production)
        calibration = (
            revenue_backtests
            if revenue_backtests is not None
            else load_latest_revenue_backtests(engine)
        )
        revenue_production = calibrate_production_revenue_intervals(
            revenue_production,
            calibration,
        )
        daily_records, product_records = build_database_records(
            revenue_production,
            product_production,
            "production",
            execution_time,
        )
        persist_forecasts(engine, daily_records, product_records)
        daily_path, product_path = export_production_forecasts(
            revenue_production,
            product_production,
            args.project_root,
        )
        print("Revenue production rows persisted:", len(daily_records))
        print("Product production rows persisted:", len(product_records))
        print(
            "Forecast dates:",
            revenue_production["target_date"].min().date(),
            "to",
            revenue_production["target_date"].max().date(),
        )
        print("Daily CSV review copy:", daily_path)
        print("Product CSV review copy:", product_path)


# Purpose: Provide the command-line entry point and concise failures.
# Used by: ``python -m src.forecast_revenue``.
def main() -> None:
    args = parse_args()
    try:
        run_pipeline(args)
    except RevenueForecastPipelineError as error:
        raise SystemExit(f"Revenue forecast pipeline failed: {error}") from error


if __name__ == "__main__":
    main()
