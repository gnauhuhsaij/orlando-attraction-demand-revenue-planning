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
