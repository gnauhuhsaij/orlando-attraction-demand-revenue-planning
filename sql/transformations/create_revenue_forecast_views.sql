-- Purpose: Expose final revenue accuracy and the latest daily and product
-- forecasts to Tableau without collapsing retained model runs.
-- Grain: Model/horizon summary, target date, or target date and product.
-- Used by: Tableau revenue dashboards and SQL validation.

BEGIN;
SET LOCAL search_path TO analytics, public;

DROP VIEW IF EXISTS vw_daily_business_action_plan;
DROP VIEW IF EXISTS vw_latest_product_revenue_forecast;
DROP VIEW IF EXISTS vw_latest_revenue_forecast;
DROP VIEW IF EXISTS vw_revenue_forecast_accuracy;

CREATE VIEW vw_revenue_forecast_accuracy AS
WITH latest_backtest_batch AS (
    SELECT max(created_at) AS created_at
    FROM fact_revenue_forecast
    WHERE forecast_run_type = 'backtest'
), scored AS (
    SELECT
        forecast.revenue_model_name,
        forecast.model_version,
        forecast.model_role,
        forecast.forecast_horizon_days,
        forecast.actual_net_revenue,
        forecast.predicted_net_revenue,
        forecast.lower_bound,
        forecast.upper_bound,
        forecast.predicted_net_revenue - forecast.actual_net_revenue AS error,
        abs(forecast.predicted_net_revenue - forecast.actual_net_revenue)
            AS absolute_error,
        power(
            forecast.predicted_net_revenue - forecast.actual_net_revenue,
            2
        ) AS squared_error
    FROM fact_revenue_forecast AS forecast
    CROSS JOIN latest_backtest_batch AS latest
    WHERE forecast.forecast_run_type = 'backtest'
      AND forecast.created_at = latest.created_at
      AND forecast.actual_net_revenue IS NOT NULL
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
    revenue_model_name,
    model_version,
    model_role,
    evaluation_horizon AS horizon_bucket,
    count(*)::integer AS observations,
    round(avg(absolute_error), 2) AS mae,
    round(sqrt(avg(squared_error)), 2) AS rmse,
    round(
        avg(absolute_error / NULLIF(actual_net_revenue, 0)) * 100,
        2
    ) AS mape_pct,
    round(
        sum(absolute_error) / NULLIF(sum(actual_net_revenue), 0) * 100,
        2
    ) AS wape_pct,
    round(avg(error), 2) AS forecast_bias,
    round(
        sum(error) / NULLIF(sum(actual_net_revenue), 0) * 100,
        2
    ) AS forecast_bias_pct,
    count(*) FILTER (WHERE lower_bound IS NOT NULL)::integer
        AS interval_observations,
    round(
        avg(
            (actual_net_revenue BETWEEN lower_bound AND upper_bound)::integer
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
    revenue_model_name,
    model_version,
    model_role,
    evaluation_horizon;

CREATE VIEW vw_latest_revenue_forecast AS
WITH latest_run AS (
    SELECT run_id
    FROM fact_revenue_forecast
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
    forecast.revenue_model_name,
    forecast.model_version,
    forecast.demand_model_name,
    forecast.product_mix_model_name,
    forecast.product_yield_model_name,
    forecast.predicted_demand,
    forecast.predicted_net_revenue,
    forecast.lower_bound,
    forecast.upper_bound,
    forecast.interval_confidence,
    forecast.interval_method,
    forecast.calibration_observations,
    round(
        forecast.predicted_net_revenue
        / NULLIF(forecast.predicted_demand, 0),
        2
    ) AS predicted_net_revenue_per_ticket,
    round(forecast.upper_bound - forecast.lower_bound, 2)
        AS prediction_interval_width,
    demand.day_of_week,
    demand.month_number,
    demand.is_weekend,
    demand.holiday_flag,
    demand.school_break_flag,
    demand.season,
    demand.planned_price_multiplier,
    demand.active_campaign_count,
    demand.maximum_planned_discount,
    demand.available_capacity,
    demand.demand_target,
    demand.revenue_target,
    round(
        forecast.predicted_net_revenue - demand.revenue_target,
        2
    ) AS forecast_revenue_variance,
    round(
        (forecast.predicted_net_revenue - demand.revenue_target)
        / NULLIF(demand.revenue_target, 0),
        4
    ) AS forecast_revenue_variance_pct,
    round(
        (forecast.lower_bound - demand.revenue_target)
        / NULLIF(demand.revenue_target, 0),
        4
    ) AS lower_revenue_variance_pct,
    round(
        (forecast.upper_bound - demand.revenue_target)
        / NULLIF(demand.revenue_target, 0),
        4
    ) AS upper_revenue_variance_pct
FROM fact_revenue_forecast AS forecast
JOIN latest_run USING (run_id)
JOIN dim_date AS created_date
    ON created_date.date_key = forecast.forecast_created_date_key
JOIN dim_date AS training_date
    ON training_date.date_key = forecast.training_end_date_key
JOIN dim_date AS target_date
    ON target_date.date_key = forecast.target_date_key
JOIN vw_latest_demand_forecast AS demand
    ON demand.target_date = target_date.calendar_date;

CREATE VIEW vw_latest_product_revenue_forecast AS
SELECT
    daily.run_id,
    daily.forecast_loaded_at,
    daily.forecast_created_date,
    daily.target_date,
    daily.forecast_horizon_days,
    product.product_key,
    dimension.product_code,
    dimension.product_name,
    dimension.ticket_tier,
    dimension.base_price,
    product.predicted_product_share,
    product.predicted_product_demand,
    product.predicted_product_net_yield,
    product.predicted_product_net_revenue,
    daily.predicted_demand AS predicted_total_demand,
    daily.predicted_net_revenue AS predicted_total_net_revenue,
    daily.revenue_target,
    daily.planned_price_multiplier
FROM vw_latest_revenue_forecast AS daily
JOIN dim_date AS target_date
    ON target_date.calendar_date = daily.target_date
JOIN fact_product_revenue_forecast AS product
    ON product.run_id = daily.run_id
   AND product.target_date_key = target_date.date_key
JOIN dim_product AS dimension USING (product_key);

COMMENT ON VIEW vw_revenue_forecast_accuracy IS
    'Latest final-revenue backtest metrics and interval coverage by horizon.';
COMMENT ON VIEW vw_latest_revenue_forecast IS
    'Latest daily production revenue forecast with targets and calibrated intervals.';
COMMENT ON VIEW vw_latest_product_revenue_forecast IS
    'Latest production demand-share, net-yield, and revenue forecast by product.';

COMMIT;
