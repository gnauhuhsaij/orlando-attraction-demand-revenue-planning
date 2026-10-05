-- Purpose: Preview observed and climatology weather features.
-- Grain: One row per visit date.
-- Used by: Forecast feature design.

SET search_path TO analytics, public;

WITH monthly_climatology AS (
    SELECT
        month_number,
        AVG(avg_temperature_f) AS climatology_temperature_f,
        AVG(precipitation_in) AS climatology_precipitation_in,
        AVG(severe_weather_flag::integer)
            AS severe_weather_probability
    FROM vw_daily_performance
    WHERE has_actuals
    GROUP BY month_number
)

SELECT
    daily.calendar_date,
    daily.has_actuals,

    daily.avg_temperature_f AS observed_temperature_f,
    climate.climatology_temperature_f,

    COALESCE(
        daily.avg_temperature_f,
        climate.climatology_temperature_f
    ) AS model_temperature_f,

    daily.precipitation_in AS observed_precipitation_in,
    climate.climatology_precipitation_in,

    COALESCE(
        daily.precipitation_in,
        climate.climatology_precipitation_in
    ) AS model_precipitation_in,

    CASE
        WHEN daily.has_actuals
            THEN daily.severe_weather_flag::integer
        ELSE climate.severe_weather_probability
    END AS model_weather_risk,

    CASE
        WHEN daily.has_actuals THEN 'observed'
        ELSE 'monthly_climatology'
    END AS weather_feature_source

FROM vw_daily_performance AS daily

JOIN monthly_climatology AS climate
    USING (month_number)

WHERE daily.calendar_date
    BETWEEN DATE '2025-12-29' AND DATE '2026-01-03'

ORDER BY daily.calendar_date;