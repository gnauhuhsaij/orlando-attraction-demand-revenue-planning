-- Purpose: Preview features available at the forecasting cutoff.
-- Grain: One row per visit date.
-- Used by: Forecast feature design before creating the permanent view.

SET search_path TO analytics, public;

SELECT
    daily.date_key,
    daily.calendar_date,
    daily.has_actuals,

    -- Prediction target: null for future dates.
    daily.net_demand,

    -- Known calendar features.
    daily.day_of_week,
    daily.month_number,
    daily.is_weekend,
    daily.holiday_flag,
    daily.school_break_flag,
    daily.season,

    -- Observed weather: unavailable for future dates.
    daily.avg_temperature_f AS observed_temperature_f,
    daily.precipitation_in AS observed_precipitation_in,

    -- Final planned price: known for both historical and future dates.
    daily.planned_price_multiplier,

    -- Campaign schedule: known before the visit date.
    campaign.active_campaign_count,
    campaign.maximum_planned_discount,

    -- Used after prediction, not as model features.
    daily.available_capacity,
    daily.demand_target,
    daily.revenue_target

FROM vw_daily_performance AS daily

LEFT JOIN LATERAL (
    SELECT
        COUNT(c.campaign_key)::integer AS active_campaign_count,
        COALESCE(MAX(c.planned_discount_value), 0)
            AS maximum_planned_discount
    FROM dim_campaign AS c
    WHERE NOT c.is_no_campaign
      AND daily.calendar_date BETWEEN c.start_date AND c.end_date
) AS campaign ON true

WHERE daily.calendar_date
    BETWEEN DATE '2025-12-27' AND DATE '2026-01-05'

ORDER BY daily.calendar_date;
