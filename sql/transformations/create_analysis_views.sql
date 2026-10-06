-- Purpose: Build reusable PostgreSQL views for business analysis and Tableau.
-- Grain: Daily, weekly, monthly product/channel, and campaign summaries.
-- Used by: SQL analysis queries, forecasting feature preparation, and dashboards.

BEGIN;
SET LOCAL search_path TO analytics, public;

-- Drop dependent views first so column-contract changes remain rerunnable.
DROP VIEW IF EXISTS vw_forecast_input;
DROP VIEW IF EXISTS vw_forecast_action_monitor;
DROP VIEW IF EXISTS vw_latest_demand_forecast;
DROP VIEW IF EXISTS vw_forecast_accuracy;
DROP VIEW IF EXISTS vw_daily_action_monitor;
DROP VIEW IF EXISTS vw_campaign_performance;
DROP VIEW IF EXISTS vw_monthly_channel_product_performance;
DROP VIEW IF EXISTS vw_weekly_performance;
DROP VIEW IF EXISTS vw_daily_performance;

-- One row per visit date. Future plan dates remain present with null actuals.
CREATE VIEW vw_daily_performance AS
WITH sales_by_visit AS (
    SELECT
        visit_date_key AS date_key,
        count(*)::integer AS order_lines,
        count(DISTINCT order_id)::integer AS orders,
        sum(units_sold)::integer AS units_sold,
        sum(units_refunded)::integer AS units_refunded,
        sum(net_units)::integer AS net_demand,
        sum(gross_revenue)::numeric(14, 2) AS gross_revenue,
        sum(discount_amount)::numeric(14, 2) AS discount_amount,
        sum(refund_amount)::numeric(14, 2) AS refund_amount,
        sum(net_revenue)::numeric(14, 2) AS net_revenue
    FROM fact_ticket_sales
    GROUP BY visit_date_key
)
SELECT
    d.date_key,
    d.calendar_date,
    d.day_of_week,
    d.day_name,
    d.week_start,
    d.week_of_year,
    d.month_number,
    d.month_name,
    d.quarter_number,
    d.year_number,
    d.is_weekend,
    d.holiday_flag,
    d.holiday_name,
    d.school_break_flag,
    d.season,
    sales.date_key IS NOT NULL AS has_actuals,
    sales.order_lines,
    sales.orders,
    sales.units_sold,
    sales.units_refunded,
    sales.net_demand,
    sales.gross_revenue,
    sales.discount_amount,
    sales.refund_amount,
    sales.net_revenue,
    round(
        sales.gross_revenue / NULLIF(sales.units_sold, 0),
        2
    ) AS average_list_price,
    round(
        sales.net_revenue / NULLIF(sales.net_demand, 0),
        2
    ) AS average_net_revenue_per_ticket,
    round(
        sales.discount_amount / NULLIF(sales.gross_revenue, 0),
        4
    ) AS discount_rate,
    round(
        sales.units_refunded::numeric / NULLIF(sales.units_sold, 0),
        4
    ) AS unit_refund_rate,
    weather.min_temperature_f,
    weather.avg_temperature_f,
    weather.max_temperature_f,
    weather.precipitation_in,
    weather.severe_weather_flag,
    plan.planned_price_multiplier,
    plan.available_capacity,
    plan.demand_target,
    plan.revenue_target,
    plan.planned_staff_hours,
    plan.plan_version,
    round(
        sales.net_demand::numeric / NULLIF(plan.available_capacity, 0),
        4
    ) AS capacity_utilization,
    round(
        sales.net_demand::numeric / NULLIF(plan.demand_target, 0),
        4
    ) AS demand_to_target_ratio,
    sales.net_demand - plan.demand_target AS demand_variance_to_target,
    round(
        (sales.net_demand - plan.demand_target)::numeric
        / NULLIF(plan.demand_target, 0),
        4
    ) AS demand_variance_pct,
    sales.net_revenue - plan.revenue_target AS revenue_variance_to_target,
    round(
        (sales.net_revenue - plan.revenue_target)
        / NULLIF(plan.revenue_target, 0),
        4
    ) AS revenue_variance_pct
FROM dim_date AS d
LEFT JOIN sales_by_visit AS sales USING (date_key)
LEFT JOIN fact_weather AS weather USING (date_key)
LEFT JOIN fact_daily_plan AS plan USING (date_key)
WHERE plan.date_key IS NOT NULL OR sales.date_key IS NOT NULL;

-- One row per Monday-starting week with LAG and rolling metrics.
CREATE VIEW vw_weekly_performance AS
WITH weekly AS (
    SELECT
        week_start,
        extract(isoyear FROM week_start)::smallint AS iso_year,
        extract(week FROM week_start)::smallint AS iso_week,
        count(*)::smallint AS actual_days,
        sum(orders)::integer AS orders,
        sum(units_sold)::integer AS units_sold,
        sum(units_refunded)::integer AS units_refunded,
        sum(net_demand)::integer AS net_demand,
        sum(gross_revenue)::numeric(14, 2) AS gross_revenue,
        sum(discount_amount)::numeric(14, 2) AS discount_amount,
        sum(refund_amount)::numeric(14, 2) AS refund_amount,
        sum(net_revenue)::numeric(14, 2) AS net_revenue,
        sum(available_capacity)::integer AS available_capacity,
        sum(demand_target)::integer AS demand_target,
        sum(revenue_target)::numeric(14, 2) AS revenue_target
    FROM vw_daily_performance
    WHERE has_actuals
    GROUP BY week_start
), windowed AS (
    SELECT
        weekly.*,
        lag(actual_days) OVER (ORDER BY week_start) AS prior_week_actual_days,
        lag(net_demand) OVER (ORDER BY week_start) AS prior_week_demand,
        lag(net_revenue) OVER (ORDER BY week_start) AS prior_week_revenue,
        lag(actual_days) OVER (
            PARTITION BY iso_week
            ORDER BY iso_year
        ) AS prior_year_actual_days,
        lag(net_demand) OVER (
            PARTITION BY iso_week
            ORDER BY iso_year
        ) AS prior_year_demand,
        lag(net_revenue) OVER (
            PARTITION BY iso_week
            ORDER BY iso_year
        ) AS prior_year_revenue,
        avg(
            CASE WHEN actual_days = 7 THEN net_demand::numeric END
        ) OVER (
            ORDER BY week_start
            ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
        ) AS rolling_4_week_avg_demand,
        avg(
            CASE WHEN actual_days = 7 THEN net_revenue END
        ) OVER (
            ORDER BY week_start
            ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
        ) AS rolling_4_week_avg_revenue
    FROM weekly
)
SELECT
    windowed.*,
    actual_days = 7 AS is_complete_week,
    round(net_revenue / NULLIF(net_demand, 0), 2)
        AS average_net_revenue_per_ticket,
    round(net_demand::numeric / NULLIF(available_capacity, 0), 4)
        AS capacity_utilization,
    round((net_demand - demand_target)::numeric / NULLIF(demand_target, 0), 4)
        AS demand_variance_pct,
    round((net_revenue - revenue_target) / NULLIF(revenue_target, 0), 4)
        AS revenue_variance_pct,
    CASE
        WHEN actual_days = 7 AND prior_week_actual_days = 7
            THEN net_demand - prior_week_demand
    END AS week_over_week_demand_change,
    CASE
        WHEN actual_days = 7 AND prior_week_actual_days = 7 THEN round(
            (net_demand - prior_week_demand)::numeric
            / NULLIF(prior_week_demand, 0),
            4
        )
    END AS week_over_week_demand_pct,
    CASE
        WHEN actual_days = 7 AND prior_week_actual_days = 7 THEN round(
            (net_revenue - prior_week_revenue)
            / NULLIF(prior_week_revenue, 0),
            4
        )
    END AS week_over_week_revenue_pct,
    CASE
        WHEN actual_days = 7 AND prior_year_actual_days = 7
            THEN net_demand - prior_year_demand
    END AS year_over_year_demand_change,
    CASE
        WHEN actual_days = 7 AND prior_year_actual_days = 7 THEN round(
            (net_demand - prior_year_demand)::numeric
            / NULLIF(prior_year_demand, 0),
            4
        )
    END AS year_over_year_demand_pct,
    CASE
        WHEN actual_days = 7 AND prior_year_actual_days = 7 THEN round(
            (net_revenue - prior_year_revenue)
            / NULLIF(prior_year_revenue, 0),
            4
        )
    END AS year_over_year_revenue_pct,
    round(rolling_4_week_avg_demand, 2) AS rolling_4_week_demand,
    round(rolling_4_week_avg_revenue, 2) AS rolling_4_week_revenue
FROM windowed;

-- One row per purchase month, product, and channel for segment analysis.
CREATE VIEW vw_monthly_channel_product_performance AS
SELECT
    date_trunc('month', purchase_date.calendar_date)::date AS month_start,
    product.product_key,
    product.product_code,
    product.product_name,
    product.ticket_tier,
    channel.channel_key,
    channel.channel_code,
    channel.channel_name,
    channel.channel_type,
    count(DISTINCT sales.order_id)::integer AS orders,
    sum(sales.units_sold)::integer AS units_sold,
    sum(sales.units_refunded)::integer AS units_refunded,
    sum(sales.net_units)::integer AS net_units,
    sum(sales.gross_revenue)::numeric(14, 2) AS gross_revenue,
    sum(sales.discount_amount)::numeric(14, 2) AS discount_amount,
    sum(sales.refund_amount)::numeric(14, 2) AS refund_amount,
    sum(sales.net_revenue)::numeric(14, 2) AS net_revenue,
    round(
        sum(sales.gross_revenue) / NULLIF(sum(sales.units_sold), 0),
        2
    ) AS average_list_price,
    round(
        sum(sales.net_revenue) / NULLIF(sum(sales.net_units), 0),
        2
    ) AS average_net_revenue_per_ticket,
    round(
        sum(sales.discount_amount) / NULLIF(sum(sales.gross_revenue), 0),
        4
    ) AS discount_rate,
    round(
        sum(sales.units_refunded)::numeric / NULLIF(sum(sales.units_sold), 0),
        4
    ) AS unit_refund_rate
FROM fact_ticket_sales AS sales
JOIN dim_date AS purchase_date
    ON purchase_date.date_key = sales.purchase_date_key
JOIN dim_product AS product USING (product_key)
JOIN dim_channel AS channel USING (channel_key)
GROUP BY
    date_trunc('month', purchase_date.calendar_date)::date,
    product.product_key,
    product.product_code,
    product.product_name,
    product.ticket_tier,
    channel.channel_key,
    channel.channel_code,
    channel.channel_name,
    channel.channel_type;

-- One row per campaign. Window-level lift is observational and not additive
-- across overlapping campaigns. Attributed sales remain campaign-specific.
CREATE VIEW vw_campaign_performance AS
WITH campaign_delivery AS (
    SELECT
        campaign_key,
        count(*)::integer AS delivery_days,
        sum(spend)::numeric(14, 2) AS marketing_spend,
        sum(impressions)::bigint AS impressions,
        sum(clicks)::bigint AS clicks,
        sum(conversions)::bigint AS conversions,
        sum(attributed_revenue)::numeric(14, 2) AS attributed_revenue
    FROM fact_campaign_daily
    GROUP BY campaign_key
), attributed_sales AS (
    SELECT
        campaign_key,
        count(DISTINCT order_id)::integer AS attributed_orders,
        sum(units_sold)::integer AS attributed_units_sold,
        sum(net_units)::integer AS attributed_net_units,
        sum(gross_revenue)::numeric(14, 2) AS attributed_gross_revenue,
        sum(discount_amount)::numeric(14, 2) AS attributed_discount_amount,
        sum(refund_amount)::numeric(14, 2) AS attributed_refund_amount,
        sum(net_revenue)::numeric(14, 2) AS attributed_net_revenue
    FROM fact_ticket_sales
    GROUP BY campaign_key
), campaign_day_base AS (
    SELECT
        campaign.campaign_key,
        daily.calendar_date,
        daily.year_number,
        daily.season,
        daily.day_of_week,
        daily.holiday_flag,
        daily.school_break_flag,
        daily.severe_weather_flag,
        daily.net_demand,
        daily.net_revenue,
        daily.average_list_price,
        daily.avg_temperature_f,
        daily.precipitation_in
    FROM dim_campaign AS campaign
    JOIN vw_daily_performance AS daily
        ON daily.calendar_date BETWEEN campaign.start_date AND campaign.end_date
       AND daily.has_actuals
    WHERE NOT campaign.is_no_campaign
), control_day_pool AS (
    SELECT daily.*
    FROM vw_daily_performance AS daily
    WHERE daily.has_actuals
      AND NOT EXISTS (
          SELECT 1
          FROM dim_campaign AS active_campaign
          WHERE NOT active_campaign.is_no_campaign
            AND daily.calendar_date BETWEEN
                active_campaign.start_date AND active_campaign.end_date
      )
), ranked_controls AS (
    SELECT
        campaign_day.campaign_key,
        campaign_day.calendar_date AS campaign_date,
        control.net_demand,
        control.net_revenue,
        control.average_list_price,
        control.avg_temperature_f,
        control.precipitation_in,
        row_number() OVER (
            PARTITION BY
                campaign_day.campaign_key,
                campaign_day.calendar_date
            ORDER BY
                CASE
                    WHEN control.holiday_flag = campaign_day.holiday_flag THEN 0
                    ELSE 1
                END,
                CASE
                    WHEN control.school_break_flag
                        = campaign_day.school_break_flag THEN 0
                    ELSE 1
                END,
                CASE
                    WHEN control.severe_weather_flag
                        = campaign_day.severe_weather_flag THEN 0
                    ELSE 1
                END,
                abs(
                    control.avg_temperature_f
                    - campaign_day.avg_temperature_f
                ),
                abs(
                    control.precipitation_in
                    - campaign_day.precipitation_in
                ),
                abs(
                    control.average_list_price
                    - campaign_day.average_list_price
                ),
                abs(control.calendar_date - campaign_day.calendar_date)
        ) AS control_rank
    FROM campaign_day_base AS campaign_day
    JOIN control_day_pool AS control
        ON control.year_number = campaign_day.year_number
       AND control.season = campaign_day.season
       AND control.day_of_week = campaign_day.day_of_week
), matched_controls AS (
    SELECT
        campaign_key,
        campaign_date,
        count(*)::smallint AS matched_control_days,
        avg(net_demand::numeric) AS matched_demand,
        avg(net_revenue) AS matched_revenue,
        avg(average_list_price) AS matched_list_price,
        avg(avg_temperature_f) AS matched_temperature,
        avg(precipitation_in) AS matched_precipitation
    FROM ranked_controls
    WHERE control_rank <= 4
    GROUP BY campaign_key, campaign_date
), campaign_days AS (
    SELECT
        campaign_day.campaign_key,
        campaign_day.calendar_date,
        campaign_day.net_demand,
        campaign_day.net_revenue,
        campaign_day.average_list_price,
        campaign_day.avg_temperature_f,
        campaign_day.precipitation_in,
        controls.matched_control_days,
        controls.matched_demand,
        controls.matched_revenue,
        controls.matched_list_price,
        controls.matched_temperature,
        controls.matched_precipitation
    FROM campaign_day_base AS campaign_day
    LEFT JOIN matched_controls AS controls
        ON controls.campaign_key = campaign_day.campaign_key
       AND controls.campaign_date = campaign_day.calendar_date
), campaign_window AS (
    SELECT
        campaign_key,
        count(*)::integer AS active_days,
        count(*) FILTER (
            WHERE matched_control_days > 0
        )::integer AS matched_campaign_days,
        sum(matched_control_days)::integer AS matched_control_observations,
        sum(net_demand)::integer AS campaign_window_net_demand,
        sum(net_revenue)::numeric(14, 2) AS campaign_window_net_revenue,
        sum(matched_demand)::numeric(14, 2) AS matched_demand,
        sum(matched_revenue)::numeric(14, 2) AS matched_revenue,
        avg(average_list_price)::numeric(10, 2) AS campaign_avg_list_price,
        avg(matched_list_price)::numeric(10, 2) AS matched_avg_list_price,
        avg(avg_temperature_f)::numeric(6, 2) AS campaign_avg_temperature_f,
        avg(matched_temperature)::numeric(6, 2) AS matched_avg_temperature_f,
        avg(precipitation_in)::numeric(7, 3) AS campaign_avg_precipitation_in,
        avg(matched_precipitation)::numeric(7, 3)
            AS matched_avg_precipitation_in
    FROM campaign_days
    GROUP BY campaign_key
)
SELECT
    campaign.campaign_key,
    campaign.campaign_code,
    campaign.campaign_name,
    campaign.campaign_type,
    campaign.target_segment,
    campaign.start_date,
    campaign.end_date,
    campaign.discount_type,
    campaign.planned_discount_value,
    delivery.delivery_days,
    window_metrics.active_days,
    window_metrics.matched_campaign_days,
    window_metrics.matched_control_observations,
    delivery.marketing_spend,
    delivery.impressions,
    delivery.clicks,
    delivery.conversions,
    delivery.attributed_revenue,
    sales.attributed_orders,
    sales.attributed_units_sold,
    sales.attributed_net_units,
    sales.attributed_gross_revenue,
    sales.attributed_discount_amount,
    sales.attributed_refund_amount,
    sales.attributed_net_revenue,
    round(delivery.clicks::numeric / NULLIF(delivery.impressions, 0), 4)
        AS click_through_rate,
    round(delivery.conversions::numeric / NULLIF(delivery.clicks, 0), 4)
        AS click_conversion_rate,
    round(delivery.marketing_spend / NULLIF(delivery.conversions, 0), 2)
        AS cost_per_conversion,
    round(delivery.attributed_revenue / NULLIF(delivery.marketing_spend, 0), 4)
        AS attributed_roas,
    round(
        sales.attributed_discount_amount
        / NULLIF(sales.attributed_units_sold, 0),
        2
    ) AS discount_cost_per_attributed_unit,
    window_metrics.campaign_window_net_demand,
    window_metrics.matched_demand,
    window_metrics.campaign_window_net_demand - window_metrics.matched_demand
        AS associated_demand_lift,
    round(
        (window_metrics.campaign_window_net_demand - window_metrics.matched_demand)
        / NULLIF(window_metrics.matched_demand, 0),
        4
    ) AS associated_demand_lift_pct,
    window_metrics.campaign_window_net_revenue,
    window_metrics.matched_revenue,
    window_metrics.campaign_window_net_revenue - window_metrics.matched_revenue
        AS associated_net_revenue_lift,
    round(
        (window_metrics.campaign_window_net_revenue - window_metrics.matched_revenue)
        / NULLIF(window_metrics.matched_revenue, 0),
        4
    ) AS associated_net_revenue_lift_pct,
    window_metrics.campaign_avg_list_price,
    window_metrics.matched_avg_list_price,
    window_metrics.campaign_avg_temperature_f,
    window_metrics.matched_avg_temperature_f,
    window_metrics.campaign_avg_precipitation_in,
    window_metrics.matched_avg_precipitation_in
FROM dim_campaign AS campaign
JOIN campaign_delivery AS delivery USING (campaign_key)
JOIN attributed_sales AS sales USING (campaign_key)
JOIN campaign_window AS window_metrics USING (campaign_key)
WHERE NOT campaign.is_no_campaign;

-- One row per plan date with historical action labels. Future dates wait for
-- the forecasting step instead of treating plans as predictions.
CREATE VIEW vw_daily_action_monitor AS
SELECT
    daily.*,
    CASE
        WHEN NOT daily.has_actuals THEN 'awaiting_forecast'
        WHEN daily.net_demand >= daily.available_capacity
            THEN 'capacity_constrained'
        WHEN daily.capacity_utilization >= 0.95 THEN 'capacity_risk'
        WHEN daily.severe_weather_flag THEN 'weather_risk'
        WHEN daily.capacity_utilization >= 0.85 THEN 'high_demand'
        WHEN daily.capacity_utilization < 0.60
         AND daily.demand_variance_pct < -0.15
            THEN 'promotion_opportunity'
        ELSE 'on_plan'
    END AS demand_status,
    CASE
        WHEN NOT daily.has_actuals THEN 6
        WHEN daily.net_demand >= daily.available_capacity THEN 1
        WHEN daily.capacity_utilization >= 0.95 THEN 2
        WHEN daily.severe_weather_flag THEN 3
        WHEN daily.capacity_utilization >= 0.85 THEN 4
        WHEN daily.capacity_utilization < 0.60
         AND daily.demand_variance_pct < -0.15 THEN 5
        ELSE 6
    END::smallint AS action_priority,
    CASE
        WHEN NOT daily.has_actuals THEN
            'Wait for the demand forecast before changing price, promotion, or staffing.'
        WHEN daily.net_demand >= daily.available_capacity THEN
            'Protect capacity: reduce broad discounts and review staffing coverage.'
        WHEN daily.capacity_utilization >= 0.95 THEN
            'Capacity risk: monitor remaining inventory and hold broad promotions.'
        WHEN daily.severe_weather_flag THEN
            'Review weather contingency staffing and shift marketing to flexible dates.'
        WHEN daily.capacity_utilization >= 0.85 THEN
            'Monitor remaining capacity and limit untargeted discounting.'
        WHEN daily.capacity_utilization < 0.60
         AND daily.demand_variance_pct < -0.15 THEN
            'Test a targeted promotion and review channel-specific demand gaps.'
        ELSE
            'Maintain the current plan and monitor updated demand signals.'
    END AS recommended_action
FROM vw_daily_performance AS daily;

COMMENT ON VIEW vw_daily_performance IS
    'Daily visit-date actuals, weather, capacity, and target performance; future actuals remain null.';
COMMENT ON VIEW vw_weekly_performance IS
    'Weekly demand and revenue metrics with LAG-based comparisons and four-week rolling averages.';
COMMENT ON VIEW vw_monthly_channel_product_performance IS
    'Monthly purchase-date revenue performance by synthetic product and sales channel.';
COMMENT ON VIEW vw_campaign_performance IS
    'Campaign attribution and observational matched-window lift; overlapping window metrics are not additive or causal.';
COMMENT ON VIEW vw_daily_action_monitor IS
    'Historical capacity and promotion signals; future plan dates wait for forecast output.';

COMMIT;
