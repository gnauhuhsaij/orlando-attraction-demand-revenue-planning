-- Purpose: Compare campaign attribution, efficiency, and matched-window lift.
-- Grain: One row per synthetic campaign.
-- Used by: Marketing & Revenue dashboard and campaign review.
-- Note: Associated lift is observational, non-causal, and non-additive when
-- campaign windows overlap.

SET search_path TO analytics, public;

SELECT
    campaign_code,
    campaign_name,
    campaign_type,
    target_segment,
    start_date,
    end_date,
    marketing_spend,
    attributed_orders,
    attributed_net_units,
    attributed_net_revenue,
    attributed_discount_amount,
    click_through_rate,
    click_conversion_rate,
    cost_per_conversion,
    attributed_roas,
    associated_demand_lift,
    associated_demand_lift_pct,
    associated_net_revenue_lift,
    associated_net_revenue_lift_pct,
    campaign_avg_list_price,
    matched_avg_list_price,
    campaign_avg_temperature_f,
    matched_avg_temperature_f,
    matched_campaign_days,
    active_days
FROM vw_campaign_performance
ORDER BY attributed_roas DESC, campaign_code;
