-- Purpose: Combine demand, revenue, and historical campaign evidence into one
-- decision-ready daily planning view without manufacturing unsupported alerts.
-- Grain: One row per date in the latest production forecast.
-- Used by: Tableau executive overview and daily operating action tables.

BEGIN;
SET LOCAL search_path TO analytics, public;

DROP VIEW IF EXISTS vw_daily_business_action_plan;

CREATE VIEW vw_daily_business_action_plan AS
WITH combined AS (
    SELECT
        demand.target_date,
        demand.forecast_horizon_days,
        demand.forecast_created_date,
        demand.training_end_date,
        demand.model_name AS demand_model_name,
        revenue.revenue_model_name,
        revenue.product_mix_model_name,
        revenue.product_yield_model_name,
        demand.predicted_demand,
        demand.lower_bound AS demand_lower_bound,
        demand.upper_bound AS demand_upper_bound,
        demand.available_capacity,
        demand.forecast_capacity_utilization,
        demand.upper_capacity_utilization,
        demand.demand_target,
        demand.forecast_demand_variance,
        demand.forecast_demand_variance_pct,
        demand.demand_status,
        demand.action_priority AS demand_action_priority,
        demand.capacity_watch_rank,
        demand.demand_watch_rank,
        revenue.predicted_net_revenue,
        revenue.lower_bound AS revenue_lower_bound,
        revenue.upper_bound AS revenue_upper_bound,
        revenue.predicted_net_revenue_per_ticket,
        revenue.revenue_target,
        revenue.forecast_revenue_variance,
        revenue.forecast_revenue_variance_pct,
        revenue.lower_revenue_variance_pct,
        revenue.upper_revenue_variance_pct,
        demand.is_weekend,
        demand.holiday_flag,
        demand.school_break_flag,
        demand.season,
        demand.planned_price_multiplier,
        demand.active_campaign_count,
        demand.maximum_planned_discount,
        campaign.campaign_family AS historical_campaign_family,
        campaign.matched_net_revenue_lift_pct
            AS historical_matched_revenue_lift_pct,
        campaign.regression_net_revenue_lift_pct
            AS historical_regression_revenue_lift_pct,
        campaign.revenue_evidence AS historical_campaign_revenue_evidence,
        campaign.ticket_yield_evidence
            AS historical_campaign_ticket_yield_evidence,
        campaign.causal_claim_allowed
            AS historical_campaign_causal_claim_allowed
    FROM vw_forecast_action_monitor AS demand
    JOIN vw_latest_revenue_forecast AS revenue USING (target_date)
    LEFT JOIN vw_latest_campaign_analysis_summary AS campaign ON true
), classified AS (
    SELECT
        combined.*,
        CASE
            WHEN upper_revenue_variance_pct < 0
                THEN 'revenue_shortfall_risk'
            WHEN lower_revenue_variance_pct > 0
                THEN 'revenue_upside'
            WHEN forecast_revenue_variance_pct < 0
                THEN 'below_target_watch'
            ELSE 'revenue_on_plan'
        END AS revenue_status,
        CASE
            WHEN upper_revenue_variance_pct < 0
                THEN 'interval_consistently_below_target'
            WHEN lower_revenue_variance_pct > 0
                THEN 'interval_consistently_above_target'
            WHEN forecast_revenue_variance_pct < 0
                THEN 'point_below_target_interval_crosses_target'
            ELSE 'point_at_or_above_target_interval_crosses_target'
        END AS revenue_evidence_strength,
        row_number() OVER (
            ORDER BY forecast_revenue_variance_pct, target_date
        )::smallint AS revenue_watch_rank,
        row_number() OVER (
            ORDER BY forecast_revenue_variance_pct DESC, target_date
        )::smallint AS revenue_upside_rank
    FROM combined
), actions AS (
    SELECT
        classified.*,
        CASE
            WHEN demand_status = 'capacity_risk' THEN 'protect_capacity'
            WHEN demand_status = 'high_demand' THEN 'monitor_high_demand'
            WHEN upper_revenue_variance_pct < 0
                THEN 'revenue_recovery_review'
            WHEN demand_status = 'promotion_opportunity'
                THEN 'targeted_promotion_review'
            ELSE 'maintain_plan'
        END AS recommended_action_code,
        CASE
            WHEN demand_status = 'capacity_risk' THEN 1
            WHEN demand_status = 'high_demand' THEN 2
            WHEN upper_revenue_variance_pct < 0 THEN 3
            WHEN demand_status = 'promotion_opportunity' THEN 4
            ELSE 6
        END::smallint AS business_action_priority,
        CASE
            WHEN predicted_demand >= available_capacity
              OR upper_revenue_variance_pct < 0 THEN 'high'
            WHEN demand_status <> 'on_plan'
              OR lower_revenue_variance_pct > 0 THEN 'medium'
            ELSE 'monitor_only'
        END AS decision_confidence
    FROM classified
)
SELECT
    actions.*,
    row_number() OVER (
        ORDER BY
            business_action_priority,
            CASE
                WHEN recommended_action_code = 'protect_capacity'
                    THEN upper_capacity_utilization
                ELSE NULL
            END DESC NULLS LAST,
            CASE
                WHEN recommended_action_code = 'revenue_recovery_review'
                    THEN forecast_revenue_variance_pct
                ELSE NULL
            END ASC NULLS LAST,
            CASE
                WHEN recommended_action_code = 'targeted_promotion_review'
                    THEN forecast_demand_variance_pct
                ELSE NULL
            END ASC NULLS LAST,
            CASE
                WHEN recommended_action_code = 'maintain_plan'
                    THEN revenue_watch_rank
                ELSE NULL
            END ASC NULLS LAST,
            target_date
    )::smallint AS business_action_rank,
    CASE recommended_action_code
        WHEN 'protect_capacity' THEN
            'Protect capacity: hold broad discounts and review staffing, inventory, and booking pace.'
        WHEN 'monitor_high_demand' THEN
            'Monitor remaining capacity and booking pace; avoid untargeted discounting.'
        WHEN 'revenue_recovery_review' THEN
            'Review price, product mix, and targeted marketing because the full revenue interval is below plan.'
        WHEN 'targeted_promotion_review' THEN
            'Evaluate a targeted promotion test; monitor net revenue and ticket yield, not demand alone.'
        ELSE
            CASE revenue_status
                WHEN 'below_target_watch' THEN
                    'Maintain the plan; the revenue point forecast is below target, but uncertainty still includes the target.'
                WHEN 'revenue_upside' THEN
                    'Maintain the plan and protect pricing because the full revenue interval is above target.'
                ELSE
                    'Maintain the current plan and refresh the forecast as new booking signals arrive.'
            END
    END AS recommended_action,
    'Historical campaign estimates are observational context, not a causal daily forecast adjustment.'::text
        AS campaign_evidence_scope
FROM actions;

COMMENT ON VIEW vw_daily_business_action_plan IS
    'Latest daily demand and revenue outlook, honest alert hierarchy, watchlist ranks, and observational campaign context.';

COMMIT;
