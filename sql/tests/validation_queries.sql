-- Data-quality gate for the analytics schema.
--
-- Run after loading data:
--   psql -v ON_ERROR_STOP=1 -d <database> -f sql/tests/validation_queries.sql
--
-- The script prints failure counts and raises an exception when any check fails.

BEGIN;
SET LOCAL search_path TO analytics, public;

CREATE TEMP TABLE data_quality_failures (
    check_name            text NOT NULL,
    record_identifier     text NOT NULL,
    issue                 text NOT NULL
) ON COMMIT DROP;

-- Calendar dates must be continuous between the earliest and latest loaded day.
INSERT INTO data_quality_failures
WITH date_bounds AS (
    SELECT min(calendar_date) AS min_date, max(calendar_date) AS max_date
    FROM dim_date
), expected_dates AS (
    SELECT generate_series(min_date, max_date, interval '1 day')::date AS calendar_date
    FROM date_bounds
)
SELECT
    'missing_calendar_date',
    expected_dates.calendar_date::text,
    'Date is absent from dim_date'
FROM expected_dates
LEFT JOIN dim_date USING (calendar_date)
WHERE dim_date.calendar_date IS NULL;

-- Business keys should remain unique even if constraints are changed later.
INSERT INTO data_quality_failures
SELECT
    'duplicate_order_line',
    order_id || ':' || order_line_number,
    'Order line appears more than once'
FROM fact_ticket_sales
GROUP BY order_id, order_line_number
HAVING count(*) > 1;

INSERT INTO data_quality_failures
SELECT
    'duplicate_forecast_target',
    run_id || ':' || target_date_key,
    'A model run contains duplicate target dates'
FROM fact_forecast
GROUP BY run_id, target_date_key
HAVING count(*) > 1;

INSERT INTO data_quality_failures
SELECT
    'duplicate_revenue_forecast_target',
    run_id || ':' || target_date_key,
    'A revenue model run contains duplicate target dates'
FROM fact_revenue_forecast
GROUP BY run_id, target_date_key
HAVING count(*) > 1;

INSERT INTO data_quality_failures
SELECT
    'duplicate_product_revenue_forecast_target',
    run_id || ':' || target_date_key || ':' || product_key,
    'A product revenue run contains duplicate target-product rows'
FROM fact_product_revenue_forecast
GROUP BY run_id, target_date_key, product_key
HAVING count(*) > 1;

-- Financial measures must reconcile exactly at the order-line level.
INSERT INTO data_quality_failures
SELECT
    'invalid_gross_revenue',
    ticket_sale_id::text,
    'gross_revenue does not equal units_sold multiplied by unit_list_price'
FROM fact_ticket_sales
WHERE gross_revenue <> round(units_sold * unit_list_price, 2);

INSERT INTO data_quality_failures
SELECT
    'invalid_net_revenue',
    ticket_sale_id::text,
    'net_revenue does not equal gross revenue less discounts and refunds'
FROM fact_ticket_sales
WHERE net_revenue <> round(gross_revenue - discount_amount - refund_amount, 2);

INSERT INTO data_quality_failures
SELECT
    'units_with_zero_gross_revenue',
    ticket_sale_id::text,
    'Positive units have zero gross revenue'
FROM fact_ticket_sales
WHERE units_sold > 0 AND gross_revenue = 0;

INSERT INTO data_quality_failures
SELECT
    'negative_financial_value',
    ticket_sale_id::text,
    'A revenue, discount, or refund measure is negative'
FROM fact_ticket_sales
WHERE gross_revenue < 0
   OR discount_amount < 0
   OR refund_amount < 0
   OR net_revenue < 0;

-- Purchase, visit, and campaign timing must be logically consistent.
INSERT INTO data_quality_failures
SELECT
    'visit_before_purchase',
    ticket_sale_id::text,
    'Visit date occurs before purchase date'
FROM fact_ticket_sales
WHERE visit_date_key < purchase_date_key;

-- Foreign keys prevent orphans during normal loads; this explicit check also
-- protects imported snapshots if constraints are temporarily disabled.
INSERT INTO data_quality_failures
SELECT
    'orphan_ticket_sales_dimension',
    s.ticket_sale_id::text,
    'Ticket sale is missing a required date, product, channel, or campaign member'
FROM fact_ticket_sales AS s
LEFT JOIN dim_date AS purchase_date
    ON purchase_date.date_key = s.purchase_date_key
LEFT JOIN dim_date AS visit_date
    ON visit_date.date_key = s.visit_date_key
LEFT JOIN dim_product AS p USING (product_key)
LEFT JOIN dim_channel AS ch USING (channel_key)
LEFT JOIN dim_campaign AS c USING (campaign_key)
WHERE purchase_date.date_key IS NULL
   OR visit_date.date_key IS NULL
   OR p.product_key IS NULL
   OR ch.channel_key IS NULL
   OR c.campaign_key IS NULL;

INSERT INTO data_quality_failures
SELECT
    'campaign_daily_outside_campaign_window',
    f.date_key::text || ':' || f.campaign_key,
    'Daily campaign record falls outside the campaign start/end dates'
FROM fact_campaign_daily AS f
JOIN dim_date AS d USING (date_key)
JOIN dim_campaign AS c USING (campaign_key)
WHERE NOT c.is_no_campaign
  AND d.calendar_date NOT BETWEEN c.start_date AND c.end_date;

INSERT INTO data_quality_failures
SELECT
    'sale_outside_campaign_window',
    s.ticket_sale_id::text,
    'Sale is attributed to a campaign outside its active dates'
FROM fact_ticket_sales AS s
JOIN dim_date AS d ON d.date_key = s.purchase_date_key
JOIN dim_campaign AS c USING (campaign_key)
WHERE NOT c.is_no_campaign
  AND d.calendar_date NOT BETWEEN c.start_date AND c.end_date;

-- Facts should have the contextual records required by the model and dashboard.
INSERT INTO data_quality_failures
SELECT
    'missing_weather_for_sales_date',
    sales_dates.visit_date_key::text,
    'A visit date with sales has no weather record'
FROM (
    SELECT DISTINCT visit_date_key
    FROM fact_ticket_sales
) AS sales_dates
LEFT JOIN fact_weather AS w ON w.date_key = sales_dates.visit_date_key
WHERE w.date_key IS NULL;

INSERT INTO data_quality_failures
SELECT
    'missing_plan_for_sales_date',
    sales_dates.visit_date_key::text,
    'A visit date with sales has no capacity and target plan'
FROM (
    SELECT DISTINCT visit_date_key
    FROM fact_ticket_sales
) AS sales_dates
LEFT JOIN fact_daily_plan AS p ON p.date_key = sales_dates.visit_date_key
WHERE p.date_key IS NULL;

INSERT INTO data_quality_failures
SELECT
    'invalid_planned_price',
    date_key::text,
    'Planned price multiplier is outside the documented operating range'
FROM fact_daily_plan
WHERE planned_price_multiplier NOT BETWEEN 0.85 AND 1.30;

-- Forecast metadata, horizon, intervals, and actuals must be internally consistent.
INSERT INTO data_quality_failures
SELECT
    'incorrect_forecast_horizon',
    f.forecast_id::text,
    'Stored forecast horizon does not match created and target dates'
FROM fact_forecast AS f
JOIN dim_date AS created_date
    ON created_date.date_key = f.forecast_created_date_key
JOIN dim_date AS target_date
    ON target_date.date_key = f.target_date_key
WHERE target_date.calendar_date - created_date.calendar_date
      <> f.forecast_horizon_days;

INSERT INTO data_quality_failures
SELECT
    'invalid_forecast_interval',
    forecast_id::text,
    'Prediction is not contained by its lower and upper bounds'
FROM fact_forecast
WHERE lower_bound > predicted_demand OR predicted_demand > upper_bound;

INSERT INTO data_quality_failures
SELECT
    'incomplete_forecast_interval_metadata',
    forecast_id::text,
    'Interval bounds and calibration metadata must be populated together'
FROM fact_forecast
WHERE num_nonnulls(
    lower_bound,
    upper_bound,
    interval_confidence,
    interval_method,
    calibration_observations
) NOT IN (0, 5);

INSERT INTO data_quality_failures
SELECT
    'production_forecast_without_interval',
    forecast_id::text,
    'Production forecasts require a calibrated uncertainty interval'
FROM fact_forecast
WHERE forecast_run_type = 'production'
  AND lower_bound IS NULL;

INSERT INTO data_quality_failures
SELECT
    'backtest_without_actual',
    forecast_id::text,
    'Backtest predictions require an out-of-sample actual value'
FROM fact_forecast
WHERE forecast_run_type = 'backtest'
  AND actual_demand IS NULL;

INSERT INTO data_quality_failures
SELECT
    'inconsistent_forecast_run',
    run_id,
    'A run_id contains inconsistent model or forecast-origin metadata'
FROM fact_forecast
GROUP BY run_id
HAVING count(DISTINCT forecast_run_type) > 1
    OR count(DISTINCT forecast_created_date_key) > 1
    OR count(DISTINCT training_end_date_key) > 1
    OR count(DISTINCT model_name) > 1
    OR count(DISTINCT model_version) > 1
    OR count(DISTINCT model_role) > 1;

INSERT INTO data_quality_failures
WITH latest_actual_date AS (
    SELECT max(d.calendar_date) AS calendar_date
    FROM fact_ticket_sales AS s
    JOIN dim_date AS d ON d.date_key = s.visit_date_key
)
SELECT
    'missing_forecast_actual',
    f.forecast_id::text,
    'Forecast target is within the loaded actual period but actual_demand is null'
FROM fact_forecast AS f
JOIN dim_date AS target_date ON target_date.date_key = f.target_date_key
CROSS JOIN latest_actual_date
WHERE target_date.calendar_date <= latest_actual_date.calendar_date
  AND f.actual_demand IS NULL;

INSERT INTO data_quality_failures
WITH daily_actuals AS (
    SELECT visit_date_key AS date_key, sum(net_units)::integer AS actual_demand
    FROM fact_ticket_sales
    GROUP BY visit_date_key
)
SELECT
    'forecast_actual_mismatch',
    f.forecast_id::text,
    'Stored actual_demand does not match net ticket units for the visit date'
FROM fact_forecast AS f
JOIN daily_actuals AS a ON a.date_key = f.target_date_key
WHERE f.actual_demand IS NOT NULL
  AND f.actual_demand <> a.actual_demand;

-- Final revenue forecasts must retain the same temporal, interval, actual,
-- and cross-grain reconciliation guarantees as total-demand forecasts.
INSERT INTO data_quality_failures
SELECT
    'incorrect_revenue_forecast_horizon',
    forecast.revenue_forecast_id::text,
    'Stored revenue horizon does not match created and target dates'
FROM fact_revenue_forecast AS forecast
JOIN dim_date AS created_date
    ON created_date.date_key = forecast.forecast_created_date_key
JOIN dim_date AS target_date
    ON target_date.date_key = forecast.target_date_key
WHERE target_date.calendar_date - created_date.calendar_date
      <> forecast.forecast_horizon_days;

INSERT INTO data_quality_failures
SELECT
    'invalid_revenue_forecast_interval',
    revenue_forecast_id::text,
    'Revenue prediction is not contained by its interval'
FROM fact_revenue_forecast
WHERE lower_bound > predicted_net_revenue
   OR predicted_net_revenue > upper_bound;

INSERT INTO data_quality_failures
SELECT
    'incomplete_revenue_interval_metadata',
    revenue_forecast_id::text,
    'Revenue interval bounds and calibration metadata must be populated together'
FROM fact_revenue_forecast
WHERE num_nonnulls(
    lower_bound,
    upper_bound,
    interval_confidence,
    interval_method,
    calibration_observations
) NOT IN (0, 5);

INSERT INTO data_quality_failures
SELECT
    'production_revenue_without_interval',
    revenue_forecast_id::text,
    'Production revenue forecasts require a calibrated interval'
FROM fact_revenue_forecast
WHERE forecast_run_type = 'production'
  AND lower_bound IS NULL;

INSERT INTO data_quality_failures
SELECT
    'backtest_revenue_without_actual',
    revenue_forecast_id::text,
    'Revenue backtests require demand and revenue actuals'
FROM fact_revenue_forecast
WHERE forecast_run_type = 'backtest'
  AND (actual_demand IS NULL OR actual_net_revenue IS NULL);

INSERT INTO data_quality_failures
SELECT
    'inconsistent_revenue_forecast_run',
    run_id,
    'A revenue run_id contains inconsistent model or origin metadata'
FROM fact_revenue_forecast
GROUP BY run_id
HAVING count(DISTINCT forecast_run_type) > 1
    OR count(DISTINCT forecast_created_date_key) > 1
    OR count(DISTINCT training_end_date_key) > 1
    OR count(DISTINCT revenue_model_name) > 1
    OR count(DISTINCT model_version) > 1
    OR count(DISTINCT demand_model_name) > 1
    OR count(DISTINCT product_mix_model_name) > 1
    OR count(DISTINCT product_yield_model_name) > 1;

INSERT INTO data_quality_failures
WITH daily_actuals AS (
    SELECT
        visit_date_key AS date_key,
        sum(net_units)::integer AS actual_demand,
        sum(net_revenue)::numeric(14, 2) AS actual_net_revenue
    FROM fact_ticket_sales
    GROUP BY visit_date_key
)
SELECT
    'revenue_forecast_actual_mismatch',
    forecast.revenue_forecast_id::text,
    'Stored revenue actuals do not match ticket facts'
FROM fact_revenue_forecast AS forecast
JOIN daily_actuals AS actuals
    ON actuals.date_key = forecast.target_date_key
WHERE forecast.actual_demand IS NOT NULL
  AND (
      forecast.actual_demand <> actuals.actual_demand
      OR forecast.actual_net_revenue <> actuals.actual_net_revenue
  );

INSERT INTO data_quality_failures
WITH product_rollup AS (
    SELECT
        run_id,
        target_date_key,
        count(*)::integer AS product_rows,
        sum(predicted_product_share) AS predicted_share,
        sum(predicted_product_demand) AS predicted_demand,
        sum(predicted_product_net_revenue) AS predicted_net_revenue,
        sum(actual_product_demand) AS actual_demand,
        sum(actual_product_net_revenue) AS actual_net_revenue
    FROM fact_product_revenue_forecast
    GROUP BY run_id, target_date_key
)
SELECT
    'product_revenue_forecast_reconciliation',
    daily.run_id || ':' || daily.target_date_key,
    'Product shares, demand, or revenue do not reconcile to the daily forecast'
FROM fact_revenue_forecast AS daily
JOIN product_rollup AS product
    USING (run_id, target_date_key)
WHERE product.product_rows <> (SELECT count(*) FROM dim_product)
   OR abs(product.predicted_share - 1) > 0.000001
   OR abs(product.predicted_demand - daily.predicted_demand) > 0.02
   OR abs(product.predicted_net_revenue - daily.predicted_net_revenue) > 0.05
   OR (
       daily.actual_demand IS NOT NULL
       AND (
           product.actual_demand <> daily.actual_demand
           OR product.actual_net_revenue <> daily.actual_net_revenue
       )
   );

-- Campaign analysis runs must preserve estimator, match, and balance contracts.
INSERT INTO data_quality_failures
SELECT
    'invalid_campaign_evaluation_interval',
    campaign_evaluation_id::text,
    'Campaign estimate is outside its stored confidence interval'
FROM fact_campaign_evaluation
WHERE lower_bound > estimate_value OR estimate_value > upper_bound;

INSERT INTO data_quality_failures
SELECT
    'invalid_campaign_evaluation_metadata',
    campaign_evaluation_id::text,
    'Campaign estimator unit or p-value metadata is inconsistent'
FROM fact_campaign_evaluation
WHERE (
        estimator_name = 'matched_block_bootstrap'
        AND (period_name <> 'campaign_window' OR p_value IS NOT NULL)
      )
   OR (
        estimator_name = 'regression_hac'
        AND (estimate_unit <> 'percent' OR p_value IS NULL)
      );

INSERT INTO data_quality_failures
WITH run_counts AS (
    SELECT
        analysis.run_id,
        analysis.treatment_days,
        analysis.matched_pairs,
        analysis.matched_balance_passed,
        count(DISTINCT evaluation.campaign_evaluation_id)::integer
            AS evaluation_rows,
        count(DISTINCT matched.treatment_date_key)::integer AS match_rows,
        count(DISTINCT balance.covariate_name)::integer AS balance_rows,
        bool_and(balance.balance_passed) AS all_balance_passed
    FROM fact_campaign_analysis_run AS analysis
    LEFT JOIN fact_campaign_evaluation AS evaluation USING (run_id)
    LEFT JOIN fact_campaign_match AS matched USING (run_id)
    LEFT JOIN fact_campaign_balance AS balance USING (run_id)
    GROUP BY
        analysis.run_id,
        analysis.treatment_days,
        analysis.matched_pairs,
        analysis.matched_balance_passed
)
SELECT
    'incomplete_campaign_analysis_run',
    run_id,
    'Campaign run does not contain 9 estimates, all matches, and 6 balance rows'
FROM run_counts
WHERE evaluation_rows <> 9
   OR match_rows <> matched_pairs
   OR match_rows <> treatment_days
   OR balance_rows <> 6
   OR all_balance_passed IS DISTINCT FROM matched_balance_passed;

INSERT INTO data_quality_failures
SELECT
    'campaign_analysis_causal_claim',
    run_id,
    'Observational campaign analysis cannot authorize a causal claim'
FROM fact_campaign_analysis_run
WHERE causal_claim_allowed;

-- The synthetic model requires exactly one explicit fallback campaign member.
INSERT INTO data_quality_failures
SELECT
    'invalid_no_campaign_member',
    'dim_campaign',
    'Exactly one is_no_campaign member is required'
WHERE (SELECT count(*) FROM dim_campaign WHERE is_no_campaign) <> 1;

SELECT
    check_name,
    count(*) AS failed_records
FROM data_quality_failures
GROUP BY check_name
ORDER BY check_name;

DO $$
DECLARE
    failure_count integer;
BEGIN
    SELECT count(*) INTO failure_count
    FROM data_quality_failures;

    IF failure_count > 0 THEN
        RAISE EXCEPTION 'Data-quality validation failed with % issue(s)', failure_count;
    END IF;
END
$$;

COMMIT;
