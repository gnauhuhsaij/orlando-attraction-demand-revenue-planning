-- Purpose: Expose persisted model accuracy, the latest production forecast,
-- and forecast-driven operating recommendations to Tableau.
-- Grain: Model/horizon summary or one row per future target date.
-- Used by: Tableau dashboards and SQL forecast reporting.

BEGIN;
SET LOCAL search_path TO analytics, public;

DROP VIEW IF EXISTS vw_daily_business_action_plan;
DROP VIEW IF EXISTS vw_latest_product_revenue_forecast;
DROP VIEW IF EXISTS vw_latest_revenue_forecast;
DROP VIEW IF EXISTS vw_revenue_forecast_accuracy;
DROP VIEW IF EXISTS vw_forecast_action_monitor;
DROP VIEW IF EXISTS vw_latest_demand_forecast;
DROP VIEW IF EXISTS vw_forecast_accuracy;

-- Use only the most recently persisted backtest batch so rerunning an
-- experiment does not give repeated predictions extra weight.
CREATE VIEW vw_forecast_accuracy AS
WITH latest_backtest_batch AS (
    SELECT max(created_at) AS created_at
    FROM fact_forecast
    WHERE forecast_run_type = 'backtest'
), scored AS (
    SELECT
        forecast.model_name,
        forecast.model_version,
        forecast.model_role,
        forecast.forecast_horizon_days,
        forecast.actual_demand,
        forecast.predicted_demand,
        forecast.lower_bound,
        forecast.upper_bound,
        forecast.predicted_demand - forecast.actual_demand AS error,
        abs(forecast.predicted_demand - forecast.actual_demand)
            AS absolute_error,
        power(forecast.predicted_demand - forecast.actual_demand, 2)
            AS squared_error
    FROM fact_forecast AS forecast
    CROSS JOIN latest_backtest_batch AS latest
    WHERE forecast.forecast_run_type = 'backtest'
      AND forecast.created_at = latest.created_at
      AND forecast.actual_demand IS NOT NULL
), bucketed AS (
    SELECT
        scored.*,
        CASE
            WHEN forecast_horizon_days <= 7 THEN 'Days 1-7'
            WHEN forecast_horizon_days <= 14 THEN 'Days 8-14'
            WHEN forecast_horizon_days <= 21 THEN 'Days 15-21'
            ELSE 'Days 22-30'
        END AS horizon_bucket
    FROM scored
), expanded AS (
    SELECT
        bucketed.*,
        horizons.horizon_bucket AS evaluation_horizon
    FROM bucketed
    CROSS JOIN LATERAL (
        VALUES ('All Horizons'::text), (bucketed.horizon_bucket)
    ) AS horizons (horizon_bucket)
)
SELECT
    model_name,
    model_version,
    model_role,
    evaluation_horizon AS horizon_bucket,
    count(*)::integer AS observations,
    round(avg(absolute_error), 2) AS mae,
    round(sqrt(avg(squared_error)), 2) AS rmse,
    round(
        avg(absolute_error / NULLIF(actual_demand, 0)) * 100,
        2
    ) AS mape_pct,
    round(avg(error), 2) AS forecast_bias,
    round(sum(error) / NULLIF(sum(actual_demand), 0) * 100, 2)
        AS forecast_bias_pct,
    count(*) FILTER (WHERE lower_bound IS NOT NULL)::integer
        AS interval_observations,
    round(
        avg(
            (
                actual_demand BETWEEN lower_bound AND upper_bound
            )::integer
        ) FILTER (WHERE lower_bound IS NOT NULL) * 100,
        2
    ) AS interval_coverage_pct,
    round(
        avg(upper_bound - lower_bound)
            FILTER (WHERE lower_bound IS NOT NULL),
        2
    ) AS average_interval_width
FROM expanded
GROUP BY
    model_name,
    model_version,
    model_role,
    evaluation_horizon;

-- Select every row from the latest production run as one coherent snapshot.
CREATE VIEW vw_latest_demand_forecast AS
WITH latest_run AS (
    SELECT run_id
    FROM fact_forecast
    WHERE forecast_run_type = 'production'
      AND model_role = 'selected'
    ORDER BY created_at DESC, run_id DESC
    LIMIT 1
)
SELECT
    forecast.run_id,
    forecast.created_at AS forecast_loaded_at,
    created_date.calendar_date AS forecast_created_date,
    training_date.calendar_date AS training_end_date,
    target_date.calendar_date AS target_date,
    forecast.forecast_horizon_days,
    forecast.model_name,
    forecast.model_version,
    forecast.predicted_demand,
    forecast.lower_bound,
    forecast.upper_bound,
    forecast.interval_confidence,
    forecast.interval_method,
    forecast.calibration_observations,
    round(forecast.upper_bound - forecast.lower_bound, 2)
        AS prediction_interval_width,
    inputs.day_of_week,
    inputs.month_number,
    inputs.is_weekend,
    inputs.holiday_flag,
    inputs.school_break_flag,
    inputs.season,
    inputs.planned_price_multiplier,
    inputs.active_campaign_count,
    inputs.maximum_planned_discount,
    inputs.available_capacity,
    inputs.demand_target,
    inputs.revenue_target,
    'training_monthly_climatology'::text AS weather_feature_source
FROM fact_forecast AS forecast
JOIN latest_run USING (run_id)
JOIN dim_date AS created_date
    ON created_date.date_key = forecast.forecast_created_date_key
JOIN dim_date AS training_date
    ON training_date.date_key = forecast.training_end_date_key
JOIN dim_date AS target_date
    ON target_date.date_key = forecast.target_date_key
JOIN vw_forecast_input AS inputs
    ON inputs.date_key = forecast.target_date_key;

-- Apply business thresholds to the latest forecast. Watchlist ranks remain
-- useful even when no date crosses an alert threshold.
CREATE VIEW vw_forecast_action_monitor AS
WITH scored AS (
    SELECT
        forecast.*,
        round(
            forecast.predicted_demand
            / NULLIF(forecast.available_capacity, 0),
            4
        ) AS forecast_capacity_utilization,
        round(
            forecast.upper_bound
            / NULLIF(forecast.available_capacity, 0),
            4
        ) AS upper_capacity_utilization,
        round(
            forecast.predicted_demand - forecast.demand_target,
            2
        ) AS forecast_demand_variance,
        round(
            (forecast.predicted_demand - forecast.demand_target)
            / NULLIF(forecast.demand_target, 0),
            4
        ) AS forecast_demand_variance_pct
    FROM vw_latest_demand_forecast AS forecast
), classified AS (
    SELECT
        scored.*,
        CASE
            WHEN predicted_demand >= available_capacity THEN 'capacity_risk'
            WHEN upper_bound >= available_capacity
              OR forecast_capacity_utilization >= 0.95 THEN 'capacity_risk'
            WHEN forecast_capacity_utilization >= 0.85 THEN 'high_demand'
            WHEN forecast_capacity_utilization < 0.60
             AND forecast_demand_variance_pct < -0.15
             AND upper_capacity_utilization < 0.85
             AND active_campaign_count = 0 THEN 'promotion_opportunity'
            ELSE 'on_plan'
        END AS demand_status,
        CASE
            WHEN predicted_demand >= available_capacity THEN 1
            WHEN upper_bound >= available_capacity
              OR forecast_capacity_utilization >= 0.95 THEN 2
            WHEN forecast_capacity_utilization >= 0.85 THEN 4
            WHEN forecast_capacity_utilization < 0.60
             AND forecast_demand_variance_pct < -0.15
             AND upper_capacity_utilization < 0.85
             AND active_campaign_count = 0 THEN 5
            ELSE 6
        END::smallint AS action_priority
    FROM scored
)
SELECT
    classified.*,
    row_number() OVER (
        ORDER BY upper_capacity_utilization DESC, target_date
    )::smallint AS capacity_watch_rank,
    row_number() OVER (
        ORDER BY forecast_demand_variance_pct, target_date
    )::smallint AS demand_watch_rank,
    CASE
        WHEN action_priority = 1 THEN
            'Point forecast is at or above capacity.'
        WHEN action_priority = 2 THEN
            'Forecast uncertainty reaches capacity.'
        WHEN action_priority = 4 THEN
            'Point forecast is at least 85% of capacity.'
        WHEN action_priority = 5 THEN
            'Demand remains low after accounting for uncertainty.'
        ELSE
            'Forecast is within the current operating plan.'
    END AS risk_basis,
    CASE
        WHEN action_priority = 1 THEN
            'Protect capacity: pause broad discounts and review staffing and inventory controls.'
        WHEN action_priority = 2 THEN
            'Monitor booking pace and remaining inventory; hold broad promotions.'
        WHEN action_priority = 4 THEN
            'Monitor remaining capacity and limit untargeted discounting.'
        WHEN action_priority = 5 THEN
            'Review a targeted promotion and prioritize cost-efficient channels.'
        ELSE
            'Maintain the current plan and monitor updated demand signals.'
    END AS recommended_action
FROM classified;

COMMENT ON VIEW vw_forecast_accuracy IS
    'Latest backtest-batch error and interval metrics by model and forecast horizon.';
COMMENT ON VIEW vw_latest_demand_forecast IS
    'Latest selected production demand forecast with calibrated intervals and plan context.';
COMMENT ON VIEW vw_forecast_action_monitor IS
    'Forecast-driven alerts plus capacity and demand watchlist ranks for Tableau.';

COMMIT;
