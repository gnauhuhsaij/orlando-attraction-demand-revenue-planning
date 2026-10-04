-- Purpose: Compare each ISO week with the same ISO week in the prior year.
-- Grain: One row per week with an available prior-year comparison.
-- Used by: Executive performance review and demand seasonality analysis.

SET search_path TO analytics, public;

SELECT
    week_start,
    iso_year,
    iso_week,
    net_demand,
    prior_year_demand,
    year_over_year_demand_change,
    year_over_year_demand_pct,
    net_revenue,
    prior_year_revenue,
    year_over_year_revenue_pct,
    rolling_4_week_demand,
    rolling_4_week_revenue
FROM vw_weekly_performance
WHERE year_over_year_demand_pct IS NOT NULL
ORDER BY week_start;
