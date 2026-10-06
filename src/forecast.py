"""Backtest demand models and persist the selected production forecast.

The notebook remains the exploratory record. This module is the repeatable
pipeline: it rebuilds comparable out-of-sample predictions, calibrates the
selected SARIMAX interval without future leakage, and appends versioned runs
to PostgreSQL for SQL analysis and Tableau.
"""

from __future__ import annotations

import argparse
import copy
import math
import os
import random
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sqlalchemy import Engine, create_engine, text
from statsmodels.tools.sm_exceptions import ConvergenceWarning
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from statsmodels.tsa.statespace.sarimax import SARIMAX
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

DEFAULT_DATABASE_URL = "postgresql+psycopg:///orlando_demand_revenue"
FORECAST_HORIZON = 30
MODEL_VERSION = "1.0"
INTERVAL_ALPHA = 0.20
INTERVAL_CONFIDENCE = 1 - INTERVAL_ALPHA
MINIMUM_CALIBRATION_ORIGINS = 3
MOVING_AVERAGE_WINDOW = 28
HORIZON_LABELS = (
    "Days 1-7",
    "Days 8-14",
    "Days 15-21",
    "Days 22-30",
)

NUMERIC_FEATURES = (
    "trend_days",
    "holiday_flag",
    "school_break_flag",
    "planned_price_multiplier",
    "active_campaign_count",
    "maximum_planned_discount",
    "paid_media_flag",
    "email_campaign_flag",
    "bundle_campaign_flag",
    "discount_campaign_flag",
    "model_temperature_f",
    "model_precipitation_in",
    "model_weather_risk",
)
CATEGORICAL_FEATURES = ("day_of_week", "month_number")
SARIMAX_NUMERIC_FEATURES = tuple(
    feature for feature in NUMERIC_FEATURES if feature != "active_campaign_count"
)

MODEL_ROLES = {
    "seasonal_naive_7d": "baseline",
    "moving_average_28d": "baseline",
    "ridge_regression": "challenger",
    "hist_gradient_boosting": "challenger",
    "holt_winters_weekly": "challenger",
    "sarimax_exogenous": "selected",
    "small_lstm": "challenger",
}
MODEL_ALIASES = {
    "seasonal_naive_7d": "snaive",
    "moving_average_28d": "ma28",
    "ridge_regression": "ridge",
    "hist_gradient_boosting": "hgb",
    "holt_winters_weekly": "hw",
    "sarimax_exogenous": "sarimax",
    "small_lstm": "lstm",
}

LSTM_LOOKBACK = 28
LSTM_HIDDEN_UNITS = 32
LSTM_BATCH_SIZE = 64
LSTM_MAX_EPOCHS = 80
LSTM_PATIENCE = 10
LSTM_INNER_VALIDATION_DAYS = 60
LSTM_LEARNING_RATE = 0.001
LSTM_RANDOM_SEED = 42


class ForecastPipelineError(RuntimeError):
    """Raised when forecast inputs, model output, or persistence is invalid."""


class DemandLSTM(nn.Module):
    """Combine a demand history sequence with known next-day features."""

    # Purpose: Configure the sequence encoder and demand output layers.
    # Used by: fit_predict_lstm_fold.
    def __init__(
        self,
        sequence_feature_count: int,
        future_feature_count: int,
        hidden_units: int = LSTM_HIDDEN_UNITS,
    ) -> None:
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=sequence_feature_count,
            hidden_size=hidden_units,
            num_layers=1,
            batch_first=True,
        )
        self.output_layer = nn.Sequential(
            nn.Linear(hidden_units + future_feature_count, hidden_units),
            nn.ReLU(),
            nn.Linear(hidden_units, 1),
        )

    # Purpose: Produce one scaled demand estimate from history and future inputs.
    # Used by: LSTM training and recursive validation forecasting.
    def forward(
        self,
        historical_sequence: torch.Tensor,
        next_day_features: torch.Tensor,
    ) -> torch.Tensor:
        _, (hidden_state, _) = self.lstm(historical_sequence)
        combined = torch.cat([hidden_state[-1], next_day_features], dim=1)
        return self.output_layer(combined).squeeze(1)


# Purpose: Parse repeatable pipeline settings without embedding credentials.
# Used by: main.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest demand models and persist the selected forecast."
    )
    parser.add_argument(
        "mode",
        choices=("backtest", "forecast", "all"),
        help=(
            "backtest stores all model comparisons; forecast stores the selected "
            "30-day run; all performs both in one execution."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy PostgreSQL URL. Defaults to DATABASE_URL or the local database.",
    )
    parser.add_argument(
        "--backtest-start",
        default="2024-12-31",
        help="First month-end forecast origin in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--backtest-end",
        default="2025-11-30",
        help="Last month-end forecast origin in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--skip-lstm",
        action="store_true",
        help="Skip the slow LSTM challenger during development checks.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root used for the generated forecast CSV.",
    )
    return parser.parse_args()


# Purpose: Read the daily modeling contract from PostgreSQL in date order.
# Used by: run_pipeline.
def load_forecast_input(engine: Engine) -> pd.DataFrame:
    query = text(
        """
        SELECT *
        FROM analytics.vw_forecast_input
        ORDER BY calendar_date
        """
    )
    with engine.connect() as connection:
        return pd.read_sql_query(
            query,
            connection,
            parse_dates=["calendar_date"],
        )


# Purpose: Fail before modeling when the daily input grain or horizon is unsafe.
# Used by: run_pipeline and unit tests.
def validate_forecast_input(
    data: pd.DataFrame,
    forecast_horizon: int = FORECAST_HORIZON,
) -> None:
    required_columns = {
        "date_key",
        "calendar_date",
        "target_demand",
        "month_number",
        "day_of_week",
        "observed_temperature_f",
        "observed_precipitation_in",
        "observed_severe_weather_flag",
        "planned_price_multiplier",
        "available_capacity",
        "demand_target",
        *NUMERIC_FEATURES[1:10],
    }
    missing_columns = sorted(required_columns.difference(data.columns))
    if missing_columns:
        raise ForecastPipelineError(
            f"Forecast input is missing columns: {', '.join(missing_columns)}"
        )
    if data.empty:
        raise ForecastPipelineError("Forecast input is empty.")
    if data["calendar_date"].duplicated().any():
        raise ForecastPipelineError("Forecast input contains duplicate dates.")
    if not data["calendar_date"].is_monotonic_increasing:
        raise ForecastPipelineError("Forecast input is not ordered by date.")
    expected_dates = pd.date_range(
        data["calendar_date"].min(), data["calendar_date"].max(), freq="D"
    )
    if not data["calendar_date"].reset_index(drop=True).equals(
        pd.Series(expected_dates, name="calendar_date")
    ):
        raise ForecastPipelineError("Forecast input dates are not continuous.")

    historical = data.loc[data["target_demand"].notna()]
    future = data.loc[data["target_demand"].isna()]
    if len(future) != forecast_horizon:
        raise ForecastPipelineError(
            f"Expected {forecast_horizon} future rows, received {len(future)}."
        )
    if historical.empty or historical["target_demand"].lt(0).any():
        raise ForecastPipelineError("Historical demand is missing or negative.")
    if historical["calendar_date"].max() + pd.Timedelta(days=1) != future[
        "calendar_date"
    ].min():
        raise ForecastPipelineError("Future horizon does not follow historical data.")
    observed_weather = (
        "observed_temperature_f",
        "observed_precipitation_in",
        "observed_severe_weather_flag",
    )
    if future[list(observed_weather)].notna().any().any():
        raise ForecastPipelineError("Future rows contain observed-weather leakage.")


# Purpose: Create month-end forecast origins with complete validation horizons.
# Used by: run_backtests and unit tests.
def build_backtest_plan(
    data: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    forecast_horizon: int = FORECAST_HORIZON,
) -> pd.DataFrame:
    historical = data.loc[data["target_demand"].notna()]
    cutoffs = pd.date_range(start=start, end=end, freq="ME")
    if cutoffs.empty:
        raise ForecastPipelineError("Backtest plan contains no forecast origins.")
    plan = pd.DataFrame({"forecast_created_date": cutoffs})
    plan["test_start"] = plan["forecast_created_date"] + pd.Timedelta(days=1)
    plan["test_end"] = plan["forecast_created_date"] + pd.Timedelta(
        days=forecast_horizon
    )
    plan["training_days"] = plan["forecast_created_date"].map(
        lambda cutoff: historical["calendar_date"].le(cutoff).sum()
    )
    if plan["test_end"].max() > historical["calendar_date"].max():
        raise ForecastPipelineError("Backtest plan extends beyond available actuals.")
    if plan["training_days"].min() < 365:
        raise ForecastPipelineError("Backtesting requires at least one training year.")
    return plan


# Purpose: Calculate comparable point-forecast metrics from aligned predictions.
# Used by: summarize_backtests and tests.
def calculate_forecast_metrics(predictions: pd.DataFrame) -> pd.Series:
    actual = predictions["actual_demand"].astype(float)
    predicted = predictions["predicted_demand"].astype(float)
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
            "forecast_bias": error.mean(),
            "forecast_bias_pct": error.sum() / actual.sum() * 100,
        }
    )


# Purpose: Estimate weather values using only observations available for training.
# Used by: build_backtest_fold and build_final_forecast_data.
def build_monthly_climatology(training: pd.DataFrame) -> pd.DataFrame:
    climatology = (
        training.groupby("month_number", as_index=False)
        .agg(
            climatology_temperature_f=("observed_temperature_f", "mean"),
            climatology_precipitation_in=("observed_precipitation_in", "mean"),
            climatology_weather_risk=("observed_severe_weather_flag", "mean"),
            climatology_days=("calendar_date", "size"),
        )
        .sort_values("month_number")
    )
    if climatology["month_number"].nunique() != 12:
        raise ForecastPipelineError("Weather climatology does not cover all months.")
    return climatology


# Purpose: Attach observed training weather and leakage-safe validation weather.
# Used by: every regression, machine-learning, and SARIMAX backtest.
def build_backtest_fold(
    data: pd.DataFrame,
    cutoff: str | pd.Timestamp,
    forecast_horizon: int = FORECAST_HORIZON,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    cutoff = pd.Timestamp(cutoff)
    forecast_end = cutoff + pd.Timedelta(days=forecast_horizon)
    training = data.loc[
        data["target_demand"].notna() & data["calendar_date"].le(cutoff)
    ].copy()
    validation = data.loc[
        data["target_demand"].notna()
        & data["calendar_date"].gt(cutoff)
        & data["calendar_date"].le(forecast_end)
    ].copy()
    if len(validation) != forecast_horizon:
        raise ForecastPipelineError(
            f"Forecast origin {cutoff.date()} has {len(validation)} validation rows."
        )

    climatology = build_monthly_climatology(training)
    training["model_temperature_f"] = training["observed_temperature_f"].astype(
        float
    )
    training["model_precipitation_in"] = training[
        "observed_precipitation_in"
    ].astype(float)
    training["model_weather_risk"] = training[
        "observed_severe_weather_flag"
    ].astype(float)
    training["weather_feature_source"] = "observed"

    validation = validation.merge(
        climatology,
        on="month_number",
        how="left",
        validate="many_to_one",
    )
    validation["model_temperature_f"] = validation["climatology_temperature_f"]
    validation["model_precipitation_in"] = validation[
        "climatology_precipitation_in"
    ]
    validation["model_weather_risk"] = validation["climatology_weather_risk"]
    validation["weather_feature_source"] = "training_monthly_climatology"
    model_weather = (
        "model_temperature_f",
        "model_precipitation_in",
        "model_weather_risk",
    )
    if validation[list(model_weather)].isna().any().any():
        raise ForecastPipelineError("Backtest climatology contains missing values.")
    return training, validation, climatology


# Purpose: Convert daily fields to numeric model inputs without changing grain.
# Used by: regression, gradient boosting, SARIMAX, and LSTM functions.
def prepare_model_features(
    data: pd.DataFrame,
    series_start_date: pd.Timestamp,
) -> pd.DataFrame:
    prepared = data.copy()
    prepared["trend_days"] = (
        prepared["calendar_date"] - pd.Timestamp(series_start_date)
    ).dt.days
    for column in NUMERIC_FEATURES:
        prepared[column] = pd.to_numeric(prepared[column], errors="raise").astype(
            float
        )
    return prepared


# Purpose: Build a reusable numeric-scaling and categorical-encoding contract.
# Used by: sklearn pipelines, SARIMAX, and LSTM fold training.
def make_preprocessor(
    numeric_features: tuple[str, ...] = NUMERIC_FEATURES,
) -> ColumnTransformer:
    return ColumnTransformer(
        transformers=[
            ("numeric", StandardScaler(), list(numeric_features)),
            (
                "categorical",
                OneHotEncoder(
                    drop="first",
                    handle_unknown="ignore",
                    sparse_output=False,
                ),
                list(CATEGORICAL_FEATURES),
            ),
        ],
        remainder="drop",
        verbose_feature_names_out=False,
    )


# Purpose: Return the common fields required for one backtest prediction fold.
# Used by: every model-specific backtest function.
def build_prediction_frame(
    cutoff: pd.Timestamp,
    validation: pd.DataFrame,
    predicted_demand: np.ndarray,
    model_name: str,
) -> pd.DataFrame:
    validation = validation.sort_values("calendar_date")
    predictions = pd.DataFrame(
        {
            "forecast_created_date": cutoff,
            "training_end_date": cutoff,
            "target_date": validation["calendar_date"].to_numpy(),
            "horizon_days": (validation["calendar_date"] - cutoff).dt.days,
            "model_name": model_name,
            "model_version": MODEL_VERSION,
            "predicted_demand": np.maximum(predicted_demand, 0),
            "actual_demand": validation["target_demand"].astype(int).to_numpy(),
        }
    )
    return predictions


# Purpose: Reproduce the last-known-same-weekday baseline without horizon leakage.
# Used by: run_backtests.
def backtest_seasonal_naive(
    data: pd.DataFrame,
    plan: pd.DataFrame,
    forecast_horizon: int = FORECAST_HORIZON,
) -> pd.DataFrame:
    historical = data.loc[data["target_demand"].notna()].copy()
    target_lookup = historical.set_index("calendar_date")["target_demand"]
    folds: list[pd.DataFrame] = []
    for cutoff in plan["forecast_created_date"]:
        validation = historical.loc[
            historical["calendar_date"].gt(cutoff)
            & historical["calendar_date"].le(
                cutoff + pd.Timedelta(days=forecast_horizon)
            )
        ].copy()
        horizon_days = (validation["calendar_date"] - cutoff).dt.days
        weeks_back = ((horizon_days - 1) // 7) + 1
        reference_dates = validation["calendar_date"] - pd.to_timedelta(
            weeks_back * 7, unit="D"
        )
        predicted = reference_dates.map(target_lookup).to_numpy(dtype=float)
        if np.isnan(predicted).any() or (reference_dates > cutoff).any():
            raise ForecastPipelineError("Seasonal-naive reference date is invalid.")
        folds.append(
            build_prediction_frame(
                cutoff, validation, predicted, "seasonal_naive_7d"
            )
        )
    return pd.concat(folds, ignore_index=True)


# Purpose: Reproduce the trailing 28-day mean baseline at each forecast origin.
# Used by: run_backtests.
def backtest_moving_average(
    data: pd.DataFrame,
    plan: pd.DataFrame,
    forecast_horizon: int = FORECAST_HORIZON,
) -> pd.DataFrame:
    historical = data.loc[data["target_demand"].notna()].copy()
    folds: list[pd.DataFrame] = []
    for cutoff in plan["forecast_created_date"]:
        training_window = (
            historical.loc[historical["calendar_date"].le(cutoff)]
            .sort_values("calendar_date")
            .tail(MOVING_AVERAGE_WINDOW)
        )
        if len(training_window) != MOVING_AVERAGE_WINDOW:
            raise ForecastPipelineError("Moving-average training window is incomplete.")
        validation = historical.loc[
            historical["calendar_date"].gt(cutoff)
            & historical["calendar_date"].le(
                cutoff + pd.Timedelta(days=forecast_horizon)
            )
        ].copy()
        predicted = np.repeat(
            training_window["target_demand"].mean(), len(validation)
        )
        folds.append(
            build_prediction_frame(
                cutoff, validation, predicted, "moving_average_28d"
            )
        )
    return pd.concat(folds, ignore_index=True)


# Purpose: Evaluate one sklearn pipeline across every forecast origin.
# Used by: Ridge regression and histogram gradient boosting backtests.
def backtest_sklearn_pipeline(
    data: pd.DataFrame,
    plan: pd.DataFrame,
    model_template: Pipeline,
    model_name: str,
    forecast_horizon: int = FORECAST_HORIZON,
) -> pd.DataFrame:
    folds: list[pd.DataFrame] = []
    series_start = data["calendar_date"].min()
    for cutoff in plan["forecast_created_date"]:
        training, validation, _ = build_backtest_fold(
            data, cutoff, forecast_horizon
        )
        training_features = prepare_model_features(training, series_start)
        validation_features = prepare_model_features(validation, series_start)
        model = clone(model_template)
        model.fit(training_features, training_features["target_demand"])
        predicted = model.predict(validation_features)
        folds.append(
            build_prediction_frame(cutoff, validation, predicted, model_name)
        )
    return pd.concat(folds, ignore_index=True)


# Purpose: Evaluate a weekly seasonal exponential-smoothing challenger.
# Used by: run_backtests.
def backtest_holt_winters(
    data: pd.DataFrame,
    plan: pd.DataFrame,
    forecast_horizon: int = FORECAST_HORIZON,
) -> pd.DataFrame:
    folds: list[pd.DataFrame] = []
    for cutoff in plan["forecast_created_date"]:
        training, validation, _ = build_backtest_fold(
            data, cutoff, forecast_horizon
        )
        training = training.sort_values("calendar_date")
        fitted = ExponentialSmoothing(
            training["target_demand"].astype(float).to_numpy(),
            trend="add",
            damped_trend=True,
            seasonal="add",
            seasonal_periods=7,
            initialization_method="estimated",
        ).fit(optimized=True, use_brute=True, remove_bias=False)
        predicted = np.asarray(fitted.forecast(forecast_horizon))
        folds.append(
            build_prediction_frame(
                cutoff, validation, predicted, "holt_winters_weekly"
            )
        )
    return pd.concat(folds, ignore_index=True)


# Purpose: Fit the selected SARIMAX specification for one forecast fold.
# Used by: backtest_sarimax and build_production_forecast.
def fit_predict_sarimax(
    training: pd.DataFrame,
    validation: pd.DataFrame,
    series_start_date: pd.Timestamp,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    training = training.sort_values("calendar_date")
    validation = validation.sort_values("calendar_date")
    training_features = prepare_model_features(training, series_start_date)
    validation_features = prepare_model_features(validation, series_start_date)
    preprocessor = make_preprocessor(SARIMAX_NUMERIC_FEATURES)
    training_exog = np.asarray(
        preprocessor.fit_transform(training_features), dtype=float
    )
    validation_exog = np.asarray(
        preprocessor.transform(validation_features), dtype=float
    )
    training_demand = training_features["target_demand"].astype(float).to_numpy()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConvergenceWarning)
        model = SARIMAX(
            endog=training_demand,
            exog=training_exog,
            order=(1, 0, 1),
            seasonal_order=(1, 0, 0, 7),
            trend="c",
            enforce_stationarity=False,
            enforce_invertibility=False,
        )
        fitted = model.fit(method="lbfgs", maxiter=200, disp=False)

    result = fitted.get_forecast(steps=len(validation), exog=validation_exog)
    predicted = np.maximum(np.asarray(result.predicted_mean), 0)
    native_interval = np.asarray(result.conf_int(alpha=INTERVAL_ALPHA))
    lower = np.maximum(native_interval[:, 0], 0)
    upper = np.maximum(native_interval[:, 1], predicted)
    diagnostics = {
        "converged": bool(fitted.mle_retvals.get("converged", False)),
        "iterations": fitted.mle_retvals.get("iterations"),
        "convergence_warnings": sum(
            issubclass(item.category, ConvergenceWarning) for item in caught
        ),
        "aic": float(fitted.aic),
    }
    if not diagnostics["converged"]:
        raise ForecastPipelineError("SARIMAX did not converge.")
    return predicted, lower, upper, diagnostics


# Purpose: Evaluate the selected SARIMAX with fold-specific exogenous features.
# Used by: run_backtests and production interval calibration.
def backtest_sarimax(
    data: pd.DataFrame,
    plan: pd.DataFrame,
    forecast_horizon: int = FORECAST_HORIZON,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    folds: list[pd.DataFrame] = []
    diagnostics: list[dict[str, Any]] = []
    series_start = data["calendar_date"].min()
    for cutoff in plan["forecast_created_date"]:
        training, validation, _ = build_backtest_fold(
            data, cutoff, forecast_horizon
        )
        predicted, native_lower, native_upper, fold_diagnostics = (
            fit_predict_sarimax(training, validation, series_start)
        )
        fold_diagnostics["forecast_created_date"] = cutoff
        diagnostics.append(fold_diagnostics)
        fold = build_prediction_frame(
            cutoff, validation, predicted, "sarimax_exogenous"
        )
        fold["native_lower_bound"] = native_lower
        fold["native_upper_bound"] = native_upper
        folds.append(fold)
    return pd.concat(folds, ignore_index=True), pd.DataFrame(diagnostics)


# Purpose: Make neural-network training repeatable across forecast folds.
# Used by: fit_predict_lstm_fold.
def set_lstm_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True, warn_only=True)


# Purpose: Convert chronological demand and exogenous data into LSTM samples.
# Used by: fit_predict_lstm_fold and unit tests.
def build_lstm_training_arrays(
    scaled_demand: np.ndarray,
    exogenous_features: np.ndarray,
    lookback: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    historical_matrix = np.column_stack(
        [scaled_demand, exogenous_features]
    ).astype(np.float32)
    if len(historical_matrix) <= lookback:
        raise ForecastPipelineError("LSTM history is shorter than its lookback.")
    sequences = np.stack(
        [
            historical_matrix[target_index - lookback : target_index]
            for target_index in range(lookback, len(historical_matrix))
        ]
    )
    next_day_features = exogenous_features[lookback:].astype(np.float32)
    targets = scaled_demand[lookback:].astype(np.float32)
    return sequences, next_day_features, targets, historical_matrix


# Purpose: Train one leakage-safe LSTM and recursively predict its outer fold.
# Used by: backtest_lstm.
def fit_predict_lstm_fold(
    training: pd.DataFrame,
    validation: pd.DataFrame,
    series_start_date: pd.Timestamp,
    seed: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    set_lstm_seed(seed)
    training_features = prepare_model_features(
        training.sort_values("calendar_date"), series_start_date
    )
    validation_features = prepare_model_features(
        validation.sort_values("calendar_date"), series_start_date
    )
    preprocessor = make_preprocessor()
    training_exog = np.asarray(
        preprocessor.fit_transform(training_features), dtype=float
    )
    validation_exog = np.asarray(
        preprocessor.transform(validation_features), dtype=np.float32
    )
    training_demand = training_features["target_demand"].astype(float).to_numpy()
    target_mean = training_demand.mean()
    target_scale = training_demand.std(ddof=0)
    if target_scale <= 0:
        raise ForecastPipelineError("LSTM training demand has zero variance.")
    scaled_demand = (training_demand - target_mean) / target_scale
    sequences, next_features, targets, history = build_lstm_training_arrays(
        scaled_demand, training_exog, LSTM_LOOKBACK
    )
    split_index = len(targets) - LSTM_INNER_VALIDATION_DAYS
    if split_index <= 0:
        raise ForecastPipelineError("Insufficient LSTM early-stopping samples.")

    dataset = TensorDataset(
        torch.from_numpy(sequences[:split_index]),
        torch.from_numpy(next_features[:split_index]),
        torch.from_numpy(targets[:split_index]),
    )
    generator = torch.Generator()
    generator.manual_seed(seed)
    loader = DataLoader(
        dataset,
        batch_size=LSTM_BATCH_SIZE,
        shuffle=True,
        generator=generator,
    )
    inner_sequences = torch.from_numpy(sequences[split_index:])
    inner_features = torch.from_numpy(next_features[split_index:])
    inner_targets = torch.from_numpy(targets[split_index:])
    model = DemandLSTM(
        sequence_feature_count=sequences.shape[2],
        future_feature_count=training_exog.shape[1],
    )
    optimizer = torch.optim.Adam(
        model.parameters(), lr=LSTM_LEARNING_RATE, weight_decay=0.0001
    )
    loss_function = nn.MSELoss()
    best_loss = float("inf")
    best_epoch = 0
    best_state = copy.deepcopy(model.state_dict())
    epochs_without_improvement = 0

    for epoch in range(LSTM_MAX_EPOCHS):
        model.train()
        for batch_sequences, batch_features, batch_targets in loader:
            optimizer.zero_grad(set_to_none=True)
            loss = loss_function(
                model(batch_sequences, batch_features), batch_targets
            )
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
        model.eval()
        with torch.inference_mode():
            validation_loss = loss_function(
                model(inner_sequences, inner_features), inner_targets
            ).item()
        if validation_loss < best_loss - 1e-6:
            best_loss = validation_loss
            best_epoch = epoch + 1
            best_state = copy.deepcopy(model.state_dict())
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1
        if epochs_without_improvement >= LSTM_PATIENCE:
            break

    model.load_state_dict(best_state)
    model.eval()
    rolling_history = history[-LSTM_LOOKBACK:].copy()
    predictions: list[float] = []
    for future_features in validation_exog:
        sequence_tensor = torch.from_numpy(
            rolling_history[np.newaxis, :, :].astype(np.float32)
        )
        feature_tensor = torch.from_numpy(future_features[np.newaxis, :])
        with torch.inference_mode():
            predicted_scaled = model(sequence_tensor, feature_tensor).item()
        predicted = max(predicted_scaled * target_scale + target_mean, 0)
        predictions.append(predicted)
        recursive_scaled = (predicted - target_mean) / target_scale
        next_history_row = np.concatenate(
            [np.array([recursive_scaled], dtype=np.float32), future_features]
        )
        rolling_history = np.vstack([rolling_history[1:], next_history_row])
    diagnostics = {
        "best_epoch": best_epoch,
        "inner_validation_loss": best_loss,
        "training_sequences": split_index,
        "inner_validation_sequences": LSTM_INNER_VALIDATION_DAYS,
    }
    return np.asarray(predictions), diagnostics


# Purpose: Evaluate the small LSTM challenger on the same outer folds.
# Used by: run_backtests when LSTM evaluation is enabled.
def backtest_lstm(
    data: pd.DataFrame,
    plan: pd.DataFrame,
    forecast_horizon: int = FORECAST_HORIZON,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    folds: list[pd.DataFrame] = []
    diagnostics: list[dict[str, Any]] = []
    series_start = data["calendar_date"].min()
    fold_count = len(plan)
    for fold_number, cutoff in enumerate(plan["forecast_created_date"]):
        training, validation, _ = build_backtest_fold(
            data, cutoff, forecast_horizon
        )
        predicted, fold_diagnostics = fit_predict_lstm_fold(
            training,
            validation,
            series_start,
            seed=LSTM_RANDOM_SEED + fold_number,
        )
        fold_diagnostics["forecast_created_date"] = cutoff
        diagnostics.append(fold_diagnostics)
        folds.append(
            build_prediction_frame(cutoff, validation, predicted, "small_lstm")
        )
        print(
            f"LSTM fold {fold_number + 1}/{fold_count} completed "
            f"at epoch {fold_diagnostics['best_epoch']}."
        )
    return pd.concat(folds, ignore_index=True), pd.DataFrame(diagnostics)


# Purpose: Compute the finite-sample absolute-error quantile for an interval.
# Used by: calibrate_backtest_intervals and build_production_forecast.
def conformal_absolute_quantile(
    absolute_errors: pd.Series | np.ndarray,
    alpha: float = INTERVAL_ALPHA,
) -> float:
    scores = np.sort(np.asarray(absolute_errors, dtype=float))
    if len(scores) == 0:
        raise ForecastPipelineError("Conformal calibration requires errors.")
    rank = min(math.ceil((len(scores) + 1) * (1 - alpha)), len(scores))
    return float(scores[rank - 1])


# Purpose: Assign horizon buckets consistently in Python and SQL reporting.
# Used by: conformal calibration and tests.
def assign_horizon_bucket(horizon_days: pd.Series) -> pd.Series:
    return pd.cut(
        horizon_days,
        bins=[0, 7, 14, 21, 30],
        labels=HORIZON_LABELS,
    )


# Purpose: Calibrate SARIMAX intervals using only earlier forecast origins.
# Used by: run_backtests before database persistence.
def calibrate_backtest_intervals(predictions: pd.DataFrame) -> pd.DataFrame:
    calibrated = predictions.copy()
    calibrated["horizon_bucket"] = assign_horizon_bucket(
        calibrated["horizon_days"]
    )
    calibrated["absolute_error"] = (
        calibrated["actual_demand"] - calibrated["predicted_demand"]
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
            quantile = conformal_absolute_quantile(errors)
            mask = calibrated["forecast_created_date"].eq(origin) & calibrated[
                "horizon_bucket"
            ].eq(horizon_bucket)
            calibrated.loc[mask, "lower_bound"] = np.maximum(
                calibrated.loc[mask, "predicted_demand"] - quantile, 0
            )
            calibrated.loc[mask, "upper_bound"] = (
                calibrated.loc[mask, "predicted_demand"] + quantile
            )
            calibrated.loc[mask, "interval_confidence"] = INTERVAL_CONFIDENCE
            calibrated.loc[mask, "interval_method"] = (
                "time_ordered_horizon_conformal"
            )
            calibrated.loc[mask, "calibration_observations"] = len(errors)
    return calibrated


# Purpose: Prove every model was evaluated on the same dates and actual values.
# Used by: run_backtests and unit tests.
def validate_aligned_predictions(
    prediction_sets: dict[str, pd.DataFrame],
    expected_origins: int,
    forecast_horizon: int = FORECAST_HORIZON,
) -> None:
    expected_rows = expected_origins * forecast_horizon
    key_columns = ["forecast_created_date", "target_date", "horizon_days"]
    comparison_columns = [*key_columns, "actual_demand"]
    reference = None
    for model_name, predictions in prediction_sets.items():
        if len(predictions) != expected_rows:
            raise ForecastPipelineError(
                f"{model_name} produced {len(predictions)} rows; "
                f"expected {expected_rows}."
            )
        if predictions.duplicated(key_columns).any():
            raise ForecastPipelineError(f"{model_name} contains duplicate targets.")
        if not np.isfinite(predictions["predicted_demand"].astype(float)).all():
            raise ForecastPipelineError(f"{model_name} contains invalid predictions.")
        candidate = predictions[comparison_columns].sort_values(key_columns)
        candidate = candidate.reset_index(drop=True)
        if reference is None:
            reference = candidate
        else:
            pd.testing.assert_frame_equal(reference, candidate, check_dtype=False)


# Purpose: Run every baseline and challenger on one shared rolling-origin plan.
# Used by: run_pipeline in backtest and all modes.
def run_backtests(
    data: pd.DataFrame,
    start: str,
    end: str,
    include_lstm: bool = True,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    plan = build_backtest_plan(data, start, end)
    ridge_pipeline = Pipeline(
        [("preprocessor", make_preprocessor()), ("model", Ridge(alpha=10.0))]
    )
    gradient_boosting_pipeline = Pipeline(
        [
            ("preprocessor", make_preprocessor()),
            (
                "model",
                HistGradientBoostingRegressor(
                    loss="squared_error",
                    learning_rate=0.05,
                    max_iter=250,
                    max_leaf_nodes=15,
                    min_samples_leaf=20,
                    l2_regularization=1.0,
                    random_state=42,
                ),
            ),
        ]
    )
    prediction_sets = {
        "seasonal_naive_7d": backtest_seasonal_naive(data, plan),
        "moving_average_28d": backtest_moving_average(data, plan),
        "ridge_regression": backtest_sklearn_pipeline(
            data, plan, ridge_pipeline, "ridge_regression"
        ),
        "hist_gradient_boosting": backtest_sklearn_pipeline(
            data, plan, gradient_boosting_pipeline, "hist_gradient_boosting"
        ),
        "holt_winters_weekly": backtest_holt_winters(data, plan),
    }
    sarimax_predictions, sarimax_diagnostics = backtest_sarimax(data, plan)
    prediction_sets["sarimax_exogenous"] = calibrate_backtest_intervals(
        sarimax_predictions
    )
    diagnostics: dict[str, pd.DataFrame] = {
        "sarimax": sarimax_diagnostics
    }
    if include_lstm:
        lstm_predictions, lstm_diagnostics = backtest_lstm(data, plan)
        prediction_sets["small_lstm"] = lstm_predictions
        diagnostics["lstm"] = lstm_diagnostics
    validate_aligned_predictions(prediction_sets, expected_origins=len(plan))
    combined = pd.concat(prediction_sets.values(), ignore_index=True)
    return combined, diagnostics


# Purpose: Prepare all actual history and the planned future horizon safely.
# Used by: build_production_forecast.
def build_final_forecast_data(
    data: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    training = data.loc[data["target_demand"].notna()].copy()
    future = data.loc[data["target_demand"].isna()].copy()
    training = training.sort_values("calendar_date")
    future = future.sort_values("calendar_date")
    climatology = build_monthly_climatology(training)
    training["model_temperature_f"] = training["observed_temperature_f"].astype(
        float
    )
    training["model_precipitation_in"] = training[
        "observed_precipitation_in"
    ].astype(float)
    training["model_weather_risk"] = training[
        "observed_severe_weather_flag"
    ].astype(float)
    training["weather_feature_source"] = "observed"
    future = future.merge(
        climatology, on="month_number", how="left", validate="many_to_one"
    )
    future["model_temperature_f"] = future["climatology_temperature_f"]
    future["model_precipitation_in"] = future[
        "climatology_precipitation_in"
    ]
    future["model_weather_risk"] = future["climatology_weather_risk"]
    future["weather_feature_source"] = "training_monthly_climatology"
    return training, future, climatology


# Purpose: Train the selected model on all history and calibrate its 30-day interval.
# Used by: run_pipeline in forecast and all modes.
def build_production_forecast(
    data: pd.DataFrame,
    sarimax_backtests: pd.DataFrame,
) -> pd.DataFrame:
    training, future, _ = build_final_forecast_data(data)
    predicted, native_lower, native_upper, diagnostics = fit_predict_sarimax(
        training, future, data["calendar_date"].min()
    )
    forecast_created_date = training["calendar_date"].max()
    forecast = build_prediction_frame(
        forecast_created_date,
        future.assign(target_demand=0),
        predicted,
        "sarimax_exogenous",
    )
    forecast["actual_demand"] = np.nan
    forecast["native_lower_bound"] = native_lower
    forecast["native_upper_bound"] = native_upper
    forecast["horizon_bucket"] = assign_horizon_bucket(forecast["horizon_days"])

    calibration = sarimax_backtests.copy()
    calibration["horizon_bucket"] = assign_horizon_bucket(
        calibration["horizon_days"]
    )
    calibration["absolute_error"] = (
        calibration["actual_demand"] - calibration["predicted_demand"]
    ).abs()
    forecast["lower_bound"] = np.nan
    forecast["upper_bound"] = np.nan
    forecast["interval_confidence"] = INTERVAL_CONFIDENCE
    forecast["interval_method"] = "horizon_bucket_conformal"
    forecast["calibration_observations"] = 0
    for horizon_bucket in HORIZON_LABELS:
        errors = calibration.loc[
            calibration["horizon_bucket"].eq(horizon_bucket), "absolute_error"
        ]
        quantile = conformal_absolute_quantile(errors)
        mask = forecast["horizon_bucket"].eq(horizon_bucket)
        forecast.loc[mask, "lower_bound"] = np.maximum(
            forecast.loc[mask, "predicted_demand"] - quantile, 0
        )
        forecast.loc[mask, "upper_bound"] = (
            forecast.loc[mask, "predicted_demand"] + quantile
        )
        forecast.loc[mask, "calibration_observations"] = len(errors)
    forecast.attrs["sarimax_diagnostics"] = diagnostics
    return forecast


# Purpose: Retrieve the newest persisted SARIMAX backtest batch for calibration.
# Used by: forecast mode when backtests are not rerun in the same execution.
def load_latest_sarimax_backtests(engine: Engine) -> pd.DataFrame:
    query = text(
        """
        WITH latest_batch AS (
            SELECT max(created_at) AS created_at
            FROM analytics.fact_forecast
            WHERE forecast_run_type = 'backtest'
        )
        SELECT
            created_date.calendar_date AS forecast_created_date,
            target_date.calendar_date AS target_date,
            forecast.forecast_horizon_days AS horizon_days,
            forecast.predicted_demand::double precision AS predicted_demand,
            forecast.actual_demand::double precision AS actual_demand
        FROM analytics.fact_forecast AS forecast
        JOIN latest_batch AS latest
          ON forecast.created_at = latest.created_at
        JOIN analytics.dim_date AS created_date
          ON created_date.date_key = forecast.forecast_created_date_key
        JOIN analytics.dim_date AS target_date
          ON target_date.date_key = forecast.target_date_key
        WHERE forecast.forecast_run_type = 'backtest'
          AND forecast.model_name = 'sarimax_exogenous'
        ORDER BY forecast.forecast_horizon_days, target_date.calendar_date
        """
    )
    with engine.connect() as connection:
        predictions = pd.read_sql_query(
            query,
            connection,
            parse_dates=["forecast_created_date", "target_date"],
        )
    if predictions.empty:
        raise ForecastPipelineError(
            "No persisted SARIMAX backtests found. Run forecast.py backtest first."
        )
    expected_buckets = set(HORIZON_LABELS)
    received_buckets = set(
        assign_horizon_bucket(predictions["horizon_days"])
        .dropna()
        .astype(str)
    )
    if received_buckets != expected_buckets:
        raise ForecastPipelineError("Persisted calibration lacks horizon coverage.")
    return predictions


# Purpose: Attach database run metadata and normalize nullable interval fields.
# Used by: run_pipeline before persistence.
def build_database_records(
    predictions: pd.DataFrame,
    run_type: str,
    execution_time: datetime,
) -> pd.DataFrame:
    records = predictions.copy()
    execution_id = execution_time.strftime("%Y%m%dT%H%M%S%fZ")
    records["forecast_run_type"] = run_type
    records["model_role"] = records["model_name"].map(MODEL_ROLES)
    if records["model_role"].isna().any():
        raise ForecastPipelineError("A model is missing its database role.")
    origin_text = records["forecast_created_date"].dt.strftime("%Y%m%d")
    aliases = records["model_name"].map(MODEL_ALIASES)
    prefix = "bt" if run_type == "backtest" else "prod"
    records["run_id"] = (
        prefix + "_" + aliases + "_" + origin_text + "_" + execution_id
    )
    records["forecast_created_date_key"] = records[
        "forecast_created_date"
    ].dt.strftime("%Y%m%d").astype(int)
    records["training_end_date_key"] = records["training_end_date"].dt.strftime(
        "%Y%m%d"
    ).astype(int)
    records["target_date_key"] = records["target_date"].dt.strftime(
        "%Y%m%d"
    ).astype(int)
    records["actual_loaded_at"] = (
        execution_time if run_type == "backtest" else None
    )
    for column in (
        "lower_bound",
        "upper_bound",
        "interval_confidence",
        "interval_method",
        "calibration_observations",
    ):
        if column not in records:
            records[column] = None
    money_columns = ("predicted_demand", "lower_bound", "upper_bound")
    for column in money_columns:
        records[column] = pd.to_numeric(records[column], errors="coerce").round(2)
    records["actual_demand"] = records["actual_demand"].map(
        lambda value: None if pd.isna(value) else int(value)
    )
    records["calibration_observations"] = records[
        "calibration_observations"
    ].map(lambda value: None if pd.isna(value) else int(value))
    records["interval_confidence"] = records["interval_confidence"].map(
        lambda value: None if pd.isna(value) else float(value)
    )
    records["lower_bound"] = records["lower_bound"].map(
        lambda value: None if pd.isna(value) else float(value)
    )
    records["upper_bound"] = records["upper_bound"].map(
        lambda value: None if pd.isna(value) else float(value)
    )
    return records


# Purpose: Check database-ready records before opening a write transaction.
# Used by: persist_forecasts and unit tests.
def validate_database_records(records: pd.DataFrame) -> None:
    required_columns = {
        "run_id",
        "forecast_run_type",
        "forecast_created_date_key",
        "training_end_date_key",
        "target_date_key",
        "model_name",
        "model_version",
        "model_role",
        "horizon_days",
        "predicted_demand",
        "lower_bound",
        "upper_bound",
        "interval_confidence",
        "interval_method",
        "calibration_observations",
        "actual_demand",
        "actual_loaded_at",
    }
    missing = sorted(required_columns.difference(records.columns))
    if missing:
        raise ForecastPipelineError(
            f"Database records are missing columns: {', '.join(missing)}"
        )
    if records.empty:
        raise ForecastPipelineError("There are no forecast records to persist.")
    if records.duplicated(["run_id", "target_date_key"]).any():
        raise ForecastPipelineError("Forecast records contain duplicate run targets.")
    if records["run_id"].str.len().gt(64).any():
        raise ForecastPipelineError("A forecast run_id exceeds 64 characters.")
    if records["predicted_demand"].isna().any() or records[
        "predicted_demand"
    ].lt(0).any():
        raise ForecastPipelineError("Predicted demand is missing or negative.")
    calculated_horizon = (
        pd.to_datetime(records["target_date_key"].astype(str))
        - pd.to_datetime(records["forecast_created_date_key"].astype(str))
    ).dt.days
    if not calculated_horizon.eq(records["horizon_days"]).all():
        raise ForecastPipelineError("A stored forecast horizon is incorrect.")
    interval_fields = [
        "lower_bound",
        "upper_bound",
        "interval_confidence",
        "interval_method",
        "calibration_observations",
    ]
    completeness = records[interval_fields].notna().sum(axis=1)
    if not completeness.isin([0, len(interval_fields)]).all():
        raise ForecastPipelineError("Forecast interval metadata is incomplete.")
    with_interval = completeness.eq(len(interval_fields))
    if not (
        records.loc[with_interval, "lower_bound"]
        .le(records.loc[with_interval, "predicted_demand"])
        .all()
        and records.loc[with_interval, "predicted_demand"]
        .le(records.loc[with_interval, "upper_bound"])
        .all()
    ):
        raise ForecastPipelineError("A prediction falls outside its interval.")
    production = records["forecast_run_type"].eq("production")
    if production.any() and not with_interval.loc[production].all():
        raise ForecastPipelineError("Every production forecast requires an interval.")
    backtest = records["forecast_run_type"].eq("backtest")
    if backtest.any() and records.loc[backtest, "actual_demand"].isna().any():
        raise ForecastPipelineError("Every backtest prediction requires an actual.")


# Purpose: Append immutable forecast runs and verify each run inside one transaction.
# Used by: run_pipeline.
def persist_forecasts(engine: Engine, records: pd.DataFrame) -> None:
    validate_database_records(records)
    insert_statement = text(
        """
        INSERT INTO analytics.fact_forecast (
            run_id,
            forecast_run_type,
            forecast_created_date_key,
            training_end_date_key,
            target_date_key,
            model_name,
            model_version,
            model_role,
            forecast_horizon_days,
            predicted_demand,
            lower_bound,
            upper_bound,
            interval_confidence,
            interval_method,
            calibration_observations,
            actual_demand,
            actual_loaded_at
        ) VALUES (
            :run_id,
            :forecast_run_type,
            :forecast_created_date_key,
            :training_end_date_key,
            :target_date_key,
            :model_name,
            :model_version,
            :model_role,
            :horizon_days,
            :predicted_demand,
            :lower_bound,
            :upper_bound,
            :interval_confidence,
            :interval_method,
            :calibration_observations,
            :actual_demand,
            :actual_loaded_at
        )
        """
    )
    insert_columns = [
        "run_id",
        "forecast_run_type",
        "forecast_created_date_key",
        "training_end_date_key",
        "target_date_key",
        "model_name",
        "model_version",
        "model_role",
        "horizon_days",
        "predicted_demand",
        "lower_bound",
        "upper_bound",
        "interval_confidence",
        "interval_method",
        "calibration_observations",
        "actual_demand",
        "actual_loaded_at",
    ]
    payload_frame = records[insert_columns].astype(object)
    payload_frame = payload_frame.where(pd.notna(payload_frame), None)
    payload = payload_frame.to_dict(orient="records")
    expected_counts = records.groupby("run_id").size().to_dict()
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": 2_026_100_6},
        )
        result = connection.execute(insert_statement, payload)
        if result.rowcount != len(records):
            raise ForecastPipelineError(
                f"Inserted {result.rowcount} rows; expected {len(records)}."
            )
        for run_id, expected_count in expected_counts.items():
            actual_count = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM analytics.fact_forecast
                    WHERE run_id = :run_id
                    """
                ),
                {"run_id": run_id},
            ).scalar_one()
            if actual_count != expected_count:
                raise ForecastPipelineError(
                    f"Run {run_id} has {actual_count} rows; expected {expected_count}."
                )


# Purpose: Print a compact comparison proving which model won the backtest.
# Used by: run_pipeline after a backtest run.
def summarize_backtests(predictions: pd.DataFrame) -> pd.DataFrame:
    summary = (
        predictions.groupby("model_name")
        .apply(calculate_forecast_metrics, include_groups=False)
        .sort_values("mae")
    )
    return summary.round(2)


# Purpose: Export the selected run for review without making CSV the system of record.
# Used by: run_pipeline after production persistence.
def export_production_forecast(
    forecast: pd.DataFrame,
    project_root: Path,
) -> Path:
    output_path = project_root / "outputs" / "forecasts" / "latest_demand_forecast.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    export_columns = [
        "forecast_created_date",
        "target_date",
        "horizon_days",
        "model_name",
        "model_version",
        "predicted_demand",
        "lower_bound",
        "upper_bound",
        "interval_confidence",
        "interval_method",
        "calibration_observations",
    ]
    forecast[export_columns].to_csv(output_path, index=False)
    return output_path


# Purpose: Coordinate optional model evaluation and the selected production run.
# Used by: main.
def run_pipeline(args: argparse.Namespace) -> None:
    engine = create_engine(args.database_url)
    data = load_forecast_input(engine)
    validate_forecast_input(data)
    execution_time = datetime.now(UTC)
    backtests: pd.DataFrame | None = None

    if args.mode in {"backtest", "all"}:
        backtests, diagnostics = run_backtests(
            data,
            start=args.backtest_start,
            end=args.backtest_end,
            include_lstm=not args.skip_lstm,
        )
        backtest_records = build_database_records(
            backtests, "backtest", execution_time
        )
        persist_forecasts(engine, backtest_records)
        print("Backtest predictions persisted:", len(backtest_records))
        print(summarize_backtests(backtests).to_string())
        print(
            "SARIMAX converged folds:",
            int(diagnostics["sarimax"]["converged"].sum()),
        )

    if args.mode in {"forecast", "all"}:
        if backtests is None:
            calibration = load_latest_sarimax_backtests(engine)
        else:
            calibration = backtests.loc[
                backtests["model_name"].eq("sarimax_exogenous")
            ].copy()
        production = build_production_forecast(data, calibration)
        production_records = build_database_records(
            production, "production", execution_time
        )
        persist_forecasts(engine, production_records)
        output_path = export_production_forecast(production, args.project_root)
        print("Production forecast rows persisted:", len(production_records))
        print(
            "Forecast dates:",
            production["target_date"].min().date(),
            "to",
            production["target_date"].max().date(),
        )
        print("CSV review copy:", output_path)


# Purpose: Provide the command-line entry point and concise failure messages.
# Used by: ``python -m src.forecast``.
def main() -> None:
    args = parse_args()
    try:
        run_pipeline(args)
    except ForecastPipelineError as error:
        raise SystemExit(f"Forecast pipeline failed: {error}") from error


if __name__ == "__main__":
    main()
