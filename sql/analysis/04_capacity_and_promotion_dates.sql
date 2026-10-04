-- Purpose: Identify historical dates that required capacity or demand action.
-- Grain: One row per actual visit date requiring attention.
-- Used by: Operations review and later forecast-action calibration.

SET search_path TO analytics, public;

SELECT
    calendar_date,
    day_name,
    season,
    holiday_flag,
    school_break_flag,
    severe_weather_flag,
    net_demand,
    available_capacity,
    capacity_utilization,
    demand_target,
    demand_variance_pct,
    net_revenue,
    revenue_target,
    revenue_variance_pct,
    demand_status,
    action_priority,
    recommended_action
FROM vw_daily_action_monitor
WHERE has_actuals
  AND demand_status <> 'on_plan'
ORDER BY action_priority, calendar_date;
