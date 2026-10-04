-- Purpose: Review weekly demand, revenue, ticket value, and target performance.
-- Grain: One row per Monday-starting week.
-- Used by: Executive Overview and Demand & Forecast dashboard pages.

SET search_path TO analytics, public;

SELECT
    week_start,
    iso_year,
    iso_week,
    actual_days,
    net_demand,
    net_revenue,
    average_net_revenue_per_ticket,
    capacity_utilization,
    demand_variance_pct,
    revenue_variance_pct,
    week_over_week_demand_pct,
    week_over_week_revenue_pct,
    rolling_4_week_demand,
    rolling_4_week_revenue
FROM vw_weekly_performance
WHERE is_complete_week
ORDER BY week_start;
