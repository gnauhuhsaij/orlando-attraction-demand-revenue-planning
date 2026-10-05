-- Purpose: Provide the base daily inputs for forecasting.
-- Grain: One row per visit date.
-- Used by: Python backtesting and final 30-day forecasting.

BEGIN;
SET LOCAL search_path TO analytics, public;

DROP VIEW IF EXISTS vw_forecast_input;

CREATE VIEW vw_forecast_input AS
SELECT
    daily.date_key,
    daily.calendar_date,

    -- Target: populated only for historical dates.
    daily.net_demand AS target_demand,
    daily.has_actuals,

    -- Calendar features known before the forecast date.
    daily.day_of_week,
    extract(doy FROM daily.calendar_date)::smallint AS day_of_year,
    daily.week_of_year,
    daily.month_number,
    daily.quarter_number,
    daily.year_number,
    daily.is_weekend,
    daily.holiday_flag,
    daily.school_break_flag,
    daily.season,

    -- Observed historical weather. Python handles future climatology.
    daily.avg_temperature_f AS observed_temperature_f,
    daily.precipitation_in AS observed_precipitation_in,
    daily.severe_weather_flag AS observed_severe_weather_flag,

    -- Planned price known before the visit date.
    ROUND(
        1.00
        + CASE WHEN daily.is_weekend THEN 0.06 ELSE 0 END
        + CASE
            WHEN daily.season = 'peak' THEN 0.08
            WHEN daily.season = 'off_peak' THEN -0.05
            ELSE 0
          END
        + CASE WHEN daily.holiday_flag THEN 0.04 ELSE 0 END,
        4
    ) AS planned_price_multiplier,

    -- Campaign schedule known before the visit date.
    campaign.active_campaign_count,
    campaign.maximum_planned_discount,
    campaign.paid_media_flag,
    campaign.email_campaign_flag,
    campaign.bundle_campaign_flag,
    campaign.discount_campaign_flag,

    -- Decision context, not primary model features.
    daily.available_capacity,
    daily.demand_target,
    daily.revenue_target

FROM vw_daily_performance AS daily

LEFT JOIN LATERAL (
    SELECT
        COUNT(c.campaign_key)::integer
            AS active_campaign_count,

        COALESCE(
            MAX(c.planned_discount_value),
            0
        ) AS maximum_planned_discount,

        COALESCE(
            MAX((c.campaign_type = 'paid_media')::integer),
            0
        ) AS paid_media_flag,

        COALESCE(
            MAX((c.campaign_type = 'email')::integer),
            0
        ) AS email_campaign_flag,

        COALESCE(
            MAX((c.campaign_type = 'bundle')::integer),
            0
        ) AS bundle_campaign_flag,

        COALESCE(
            MAX((c.campaign_type = 'discount')::integer),
            0
        ) AS discount_campaign_flag

    FROM dim_campaign AS c

    WHERE NOT c.is_no_campaign
      AND daily.calendar_date BETWEEN c.start_date AND c.end_date
) AS campaign ON true;

COMMENT ON VIEW vw_forecast_input IS
    'Base daily forecasting inputs. Future weather remains null so each backtest uses training-only climatology.';

COMMIT;