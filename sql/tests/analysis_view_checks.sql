-- Purpose: Validate analysis-view grain, reconciliation, and status semantics.
-- Grain: One failure record per violated analytical contract.
-- Used by: src/load_postgres.py after the views are created.

BEGIN;
SET LOCAL search_path TO analytics, public;

CREATE TEMP TABLE analysis_view_failures (
    check_name            text NOT NULL,
    record_identifier     text NOT NULL,
    issue                 text NOT NULL
) ON COMMIT DROP;

INSERT INTO analysis_view_failures
SELECT
    'duplicate_daily_performance_date',
    date_key::text,
    'Daily performance contains more than one row for a date'
FROM vw_daily_performance
GROUP BY date_key
HAVING count(*) > 1;

INSERT INTO analysis_view_failures
WITH fact_totals AS (
    SELECT
        sum(net_units)::bigint AS net_demand,
        sum(net_revenue)::numeric(20, 2) AS net_revenue
    FROM fact_ticket_sales
), view_totals AS (
    SELECT
        sum(net_demand)::bigint AS net_demand,
        sum(net_revenue)::numeric(20, 2) AS net_revenue
    FROM vw_daily_performance
    WHERE has_actuals
)
SELECT
    'daily_performance_reconciliation',
    'all_actual_dates',
    'Daily demand or revenue does not reconcile to ticket facts'
FROM fact_totals
CROSS JOIN view_totals
WHERE fact_totals.net_demand <> view_totals.net_demand
   OR fact_totals.net_revenue <> view_totals.net_revenue;

INSERT INTO analysis_view_failures
WITH daily_totals AS (
    SELECT
        sum(net_demand)::bigint AS net_demand,
        sum(net_revenue)::numeric(20, 2) AS net_revenue
    FROM vw_daily_performance
    WHERE has_actuals
), product_totals AS (
    SELECT
        sum(net_demand)::bigint AS net_demand,
        sum(net_revenue)::numeric(20, 2) AS net_revenue
    FROM vw_daily_product_performance
    WHERE has_actuals
)
SELECT
    'daily_product_reconciliation',
    'all_actual_dates',
    'Daily product demand or revenue does not reconcile to daily totals'
FROM daily_totals
CROSS JOIN product_totals
WHERE daily_totals.net_demand <> product_totals.net_demand
   OR daily_totals.net_revenue <> product_totals.net_revenue;

INSERT INTO analysis_view_failures
SELECT
    'daily_product_share_reconciliation',
    calendar_date::text,
    'Historical product demand shares do not sum to one'
FROM vw_daily_product_performance
WHERE has_actuals
GROUP BY calendar_date
HAVING abs(sum(actual_product_demand_share) - 1) > 0.00001;

INSERT INTO analysis_view_failures
WITH daily_totals AS (
    SELECT
        sum(net_demand)::bigint AS net_demand,
        sum(net_revenue)::numeric(20, 2) AS net_revenue
    FROM vw_daily_performance
    WHERE has_actuals
), weekly_totals AS (
    SELECT
        sum(net_demand)::bigint AS net_demand,
        sum(net_revenue)::numeric(20, 2) AS net_revenue
    FROM vw_weekly_performance
)
SELECT
    'weekly_performance_reconciliation',
    'all_actual_weeks',
    'Weekly demand or revenue does not reconcile to daily performance'
FROM daily_totals
CROSS JOIN weekly_totals
WHERE daily_totals.net_demand <> weekly_totals.net_demand
   OR daily_totals.net_revenue <> weekly_totals.net_revenue;

INSERT INTO analysis_view_failures
WITH fact_totals AS (
    SELECT
        sum(net_units)::bigint AS net_units,
        sum(net_revenue)::numeric(20, 2) AS net_revenue
    FROM fact_ticket_sales
), segment_totals AS (
    SELECT
        sum(net_units)::bigint AS net_units,
        sum(net_revenue)::numeric(20, 2) AS net_revenue
    FROM vw_monthly_channel_product_performance
)
SELECT
    'segment_performance_reconciliation',
    'all_purchase_months',
    'Product/channel demand or revenue does not reconcile to ticket facts'
FROM fact_totals
CROSS JOIN segment_totals
WHERE fact_totals.net_units <> segment_totals.net_units
   OR fact_totals.net_revenue <> segment_totals.net_revenue;

INSERT INTO analysis_view_failures
SELECT
    'campaign_view_row_count',
    'vw_campaign_performance',
    'Campaign view must contain one row per non-fallback campaign'
WHERE (
    SELECT count(*) FROM vw_campaign_performance
) <> (
    SELECT count(*) FROM dim_campaign WHERE NOT is_no_campaign
);

INSERT INTO analysis_view_failures
SELECT
    'campaign_missing_matched_day',
    campaign_code,
    'At least one active campaign day lacks a matched comparison'
FROM vw_campaign_performance
WHERE matched_campaign_days <> active_days
   OR matched_control_observations < matched_campaign_days;

INSERT INTO analysis_view_failures
SELECT
    'invalid_action_status',
    date_key::text,
    'Daily action status is outside the documented decision set'
FROM vw_daily_action_monitor
WHERE demand_status NOT IN (
    'awaiting_forecast',
    'capacity_constrained',
    'capacity_risk',
    'weather_risk',
    'high_demand',
    'promotion_opportunity',
    'on_plan'
);

INSERT INTO analysis_view_failures
SELECT
    'capacity_status_mismatch',
    date_key::text,
    'Capacity-constrained status does not match demand at or above capacity'
FROM vw_daily_action_monitor
WHERE has_actuals
  AND (
      (demand_status = 'capacity_constrained')
      <> (net_demand >= available_capacity)
  );

INSERT INTO analysis_view_failures
SELECT
    'partial_week_comparison',
    week_start::text,
    'A partial week received a week-over-week or year-over-year rate'
FROM vw_weekly_performance
WHERE NOT is_complete_week
  AND (
      week_over_week_demand_pct IS NOT NULL
      OR year_over_year_demand_pct IS NOT NULL
  );

INSERT INTO analysis_view_failures
SELECT
    'future_date_not_awaiting_forecast',
    date_key::text,
    'A date without actual demand received a historical action label'
FROM vw_daily_action_monitor
WHERE NOT has_actuals
  AND demand_status <> 'awaiting_forecast';

INSERT INTO analysis_view_failures
SELECT
    'duplicate_latest_forecast_date',
    target_date::text,
    'Latest production forecast contains more than one row for a target date'
FROM vw_latest_demand_forecast
GROUP BY target_date
HAVING count(*) > 1;

INSERT INTO analysis_view_failures
WITH latest_run AS (
    SELECT run_id
    FROM fact_forecast
    WHERE forecast_run_type = 'production'
      AND model_role = 'selected'
    ORDER BY created_at DESC, run_id DESC
    LIMIT 1
)
SELECT
    'latest_forecast_row_count',
    latest_run.run_id,
    'Latest forecast view does not expose every row in its selected run'
FROM latest_run
WHERE (
    SELECT count(*)
    FROM vw_latest_demand_forecast
) <> (
    SELECT count(*)
    FROM fact_forecast
    WHERE run_id = latest_run.run_id
);

INSERT INTO analysis_view_failures
SELECT
    'invalid_forecast_action_status',
    target_date::text,
    'Forecast action status is outside the documented decision set'
FROM vw_forecast_action_monitor
WHERE demand_status NOT IN (
    'capacity_risk',
    'high_demand',
    'promotion_opportunity',
    'on_plan'
);

INSERT INTO analysis_view_failures
SELECT
    'forecast_action_priority_mismatch',
    target_date::text,
    'Forecast action priority does not match its decision status'
FROM vw_forecast_action_monitor
WHERE action_priority <> CASE demand_status
    WHEN 'capacity_risk' THEN
        CASE WHEN predicted_demand >= available_capacity THEN 1 ELSE 2 END
    WHEN 'high_demand' THEN 4
    WHEN 'promotion_opportunity' THEN 5
    ELSE 6
END;

INSERT INTO analysis_view_failures
SELECT
    'duplicate_latest_revenue_forecast_date',
    target_date::text,
    'Latest revenue forecast contains more than one row for a target date'
FROM vw_latest_revenue_forecast
GROUP BY target_date
HAVING count(*) > 1;

INSERT INTO analysis_view_failures
WITH latest_run AS (
    SELECT run_id
    FROM fact_revenue_forecast
    WHERE forecast_run_type = 'production'
      AND model_role = 'selected'
    ORDER BY created_at DESC, run_id DESC
    LIMIT 1
)
SELECT
    'latest_revenue_forecast_row_count',
    latest_run.run_id,
    'Latest revenue view does not expose every row in its selected run'
FROM latest_run
WHERE (
    SELECT count(*) FROM vw_latest_revenue_forecast
) <> (
    SELECT count(*)
    FROM fact_revenue_forecast
    WHERE run_id = latest_run.run_id
);

INSERT INTO analysis_view_failures
WITH latest_run AS (
    SELECT run_id
    FROM fact_revenue_forecast
    WHERE forecast_run_type = 'production'
      AND model_role = 'selected'
    ORDER BY created_at DESC, run_id DESC
    LIMIT 1
), expected AS (
    SELECT
        latest_run.run_id,
        count(*)::integer AS expected_rows
    FROM latest_run
    JOIN fact_revenue_forecast AS daily USING (run_id)
    CROSS JOIN dim_product
    GROUP BY latest_run.run_id
)
SELECT
    'latest_product_revenue_forecast_row_count',
    expected.run_id,
    'Latest product view does not contain one row per target and product'
FROM expected
WHERE (
    SELECT count(*) FROM vw_latest_product_revenue_forecast
) <> expected.expected_rows;

INSERT INTO analysis_view_failures
SELECT
    'latest_revenue_demand_mismatch',
    revenue.target_date::text,
    'Revenue and selected demand forecasts disagree on predicted demand'
FROM vw_latest_revenue_forecast AS revenue
JOIN vw_latest_demand_forecast AS demand USING (target_date)
WHERE revenue.predicted_demand <> demand.predicted_demand;

INSERT INTO analysis_view_failures
WITH latest_run AS (
    SELECT run_id, matched_pairs
    FROM fact_campaign_analysis_run
    ORDER BY created_at DESC, run_id DESC
    LIMIT 1
)
SELECT
    'latest_campaign_view_row_counts',
    latest_run.run_id,
    'Latest campaign views do not expose the complete selected run'
FROM latest_run
WHERE (SELECT count(*) FROM vw_latest_campaign_analysis_summary) <> 1
   OR (SELECT count(*) FROM vw_latest_campaign_evaluation) <> 9
   OR (SELECT count(*) FROM vw_latest_campaign_match)
      <> latest_run.matched_pairs
   OR (SELECT count(*) FROM vw_latest_campaign_balance) <> 6;

INSERT INTO analysis_view_failures
SELECT
    'latest_campaign_balance_failure',
    covariate_name,
    'Latest campaign match exceeds its declared balance threshold'
FROM vw_latest_campaign_balance
WHERE NOT balance_passed
   OR matched_absolute_smd > balance_threshold;

INSERT INTO analysis_view_failures
SELECT
    'latest_campaign_summary_causal_claim',
    run_id,
    'Latest observational campaign summary incorrectly permits causality'
FROM vw_latest_campaign_analysis_summary
WHERE causal_claim_allowed;

INSERT INTO analysis_view_failures
SELECT
    'duplicate_business_action_date',
    target_date::text,
    'Business action view contains more than one row for a forecast date'
FROM vw_daily_business_action_plan
GROUP BY target_date
HAVING count(*) > 1;

INSERT INTO analysis_view_failures
SELECT
    'business_action_row_count',
    'vw_daily_business_action_plan',
    'Business action view does not align one-to-one with the latest demand and revenue forecasts'
WHERE (SELECT count(*) FROM vw_daily_business_action_plan)
      <> (SELECT count(*) FROM vw_latest_demand_forecast)
   OR (SELECT count(*) FROM vw_daily_business_action_plan)
      <> (SELECT count(*) FROM vw_latest_revenue_forecast);

INSERT INTO analysis_view_failures
SELECT
    'invalid_business_action_code',
    target_date::text,
    'Business action code is outside the documented decision set'
FROM vw_daily_business_action_plan
WHERE recommended_action_code NOT IN (
    'protect_capacity',
    'monitor_high_demand',
    'revenue_recovery_review',
    'targeted_promotion_review',
    'maintain_plan'
);

INSERT INTO analysis_view_failures
SELECT
    'business_action_priority_mismatch',
    target_date::text,
    'Business priority does not match the selected action code'
FROM vw_daily_business_action_plan
WHERE business_action_priority <> CASE recommended_action_code
    WHEN 'protect_capacity' THEN 1
    WHEN 'monitor_high_demand' THEN 2
    WHEN 'revenue_recovery_review' THEN 3
    WHEN 'targeted_promotion_review' THEN 4
    ELSE 6
END;

INSERT INTO analysis_view_failures
SELECT
    'invalid_revenue_action_status',
    target_date::text,
    'Revenue status is inconsistent with its forecast interval and target'
FROM vw_daily_business_action_plan
WHERE revenue_status <> CASE
    WHEN upper_revenue_variance_pct < 0 THEN 'revenue_shortfall_risk'
    WHEN lower_revenue_variance_pct > 0 THEN 'revenue_upside'
    WHEN forecast_revenue_variance_pct < 0 THEN 'below_target_watch'
    ELSE 'revenue_on_plan'
END;

INSERT INTO analysis_view_failures
SELECT
    'business_action_campaign_causal_claim',
    target_date::text,
    'Daily action view must not expose historical campaign evidence as causal'
FROM vw_daily_business_action_plan
WHERE historical_campaign_causal_claim_allowed IS TRUE;

SELECT
    check_name,
    count(*) AS failed_records
FROM analysis_view_failures
GROUP BY check_name
ORDER BY check_name;

DO $$
DECLARE
    failure_count integer;
BEGIN
    SELECT count(*) INTO failure_count
    FROM analysis_view_failures;

    IF failure_count > 0 THEN
        RAISE EXCEPTION
            'Analysis-view validation failed with % issue(s)', failure_count;
    END IF;
END
$$;

COMMIT;
