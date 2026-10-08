-- Purpose: Expose the newest versioned campaign analysis to Tableau while
-- retaining historical analysis runs in the underlying facts.
-- Grain: Analysis summary, estimator/outcome, matched date, or covariate.
-- Used by: Marketing & Revenue dashboard and SQL validation.

BEGIN;
SET LOCAL search_path TO analytics, public;

DROP VIEW IF EXISTS vw_daily_business_action_plan;
DROP VIEW IF EXISTS vw_latest_campaign_analysis_summary;
DROP VIEW IF EXISTS vw_latest_campaign_evaluation;
DROP VIEW IF EXISTS vw_latest_campaign_match;
DROP VIEW IF EXISTS vw_latest_campaign_balance;

CREATE VIEW vw_latest_campaign_evaluation AS
WITH latest_run AS (
    SELECT run_id
    FROM fact_campaign_analysis_run
    ORDER BY created_at DESC, run_id DESC
    LIMIT 1
)
SELECT
    evaluation.run_id,
    analysis.created_at AS analysis_created_at,
    analysis.campaign_family,
    analysis.analysis_version,
    analysis.primary_outcome,
    evaluation.outcome_name,
    evaluation.estimator_name,
    evaluation.period_name,
    evaluation.observations,
    evaluation.estimate_value,
    evaluation.estimate_pct,
    evaluation.lower_bound,
    evaluation.upper_bound,
    evaluation.estimate_unit,
    evaluation.p_value,
    evaluation.effect_size,
    evaluation.interval_excludes_zero,
    evaluation.is_primary_outcome
FROM fact_campaign_evaluation AS evaluation
JOIN latest_run USING (run_id)
JOIN fact_campaign_analysis_run AS analysis USING (run_id);

CREATE VIEW vw_latest_campaign_match AS
WITH latest_run AS (
    SELECT run_id
    FROM fact_campaign_analysis_run
    ORDER BY created_at DESC, run_id DESC
    LIMIT 1
)
SELECT
    matched.run_id,
    analysis.created_at AS analysis_created_at,
    analysis.campaign_family,
    treatment_date.calendar_date AS treatment_date,
    control_date.calendar_date AS matched_control_date,
    matched.campaign_year,
    matched.season,
    matched.day_of_week,
    matched.match_distance,
    matched.treated_net_demand,
    matched.control_net_demand,
    matched.demand_lift,
    matched.treated_net_revenue,
    matched.control_net_revenue,
    matched.net_revenue_lift,
    matched.treated_net_revenue_per_ticket,
    matched.control_net_revenue_per_ticket,
    matched.net_revenue_per_ticket_lift
FROM fact_campaign_match AS matched
JOIN latest_run USING (run_id)
JOIN fact_campaign_analysis_run AS analysis USING (run_id)
JOIN dim_date AS treatment_date
    ON treatment_date.date_key = matched.treatment_date_key
JOIN dim_date AS control_date
    ON control_date.date_key = matched.control_date_key;

CREATE VIEW vw_latest_campaign_balance AS
WITH latest_run AS (
    SELECT run_id
    FROM fact_campaign_analysis_run
    ORDER BY created_at DESC, run_id DESC
    LIMIT 1
)
SELECT
    balance.run_id,
    analysis.created_at AS analysis_created_at,
    analysis.campaign_family,
    balance.covariate_name,
    balance.raw_smd,
    balance.matched_smd,
    abs(balance.raw_smd) AS raw_absolute_smd,
    abs(balance.matched_smd) AS matched_absolute_smd,
    balance.balance_threshold,
    balance.balance_passed
FROM fact_campaign_balance AS balance
JOIN latest_run USING (run_id)
JOIN fact_campaign_analysis_run AS analysis USING (run_id);

CREATE VIEW vw_latest_campaign_analysis_summary AS
WITH latest_run AS (
    SELECT *
    FROM fact_campaign_analysis_run
    ORDER BY created_at DESC, run_id DESC
    LIMIT 1
), evaluation AS (
    SELECT
        result.run_id,
        max(result.estimate_value) FILTER (
            WHERE result.outcome_name = 'net_revenue'
              AND result.estimator_name = 'matched_block_bootstrap'
              AND result.period_name = 'campaign_window'
        ) AS matched_daily_net_revenue_lift,
        max(result.estimate_pct) FILTER (
            WHERE result.outcome_name = 'net_revenue'
              AND result.estimator_name = 'matched_block_bootstrap'
              AND result.period_name = 'campaign_window'
        ) AS matched_net_revenue_lift_pct,
        max(result.lower_bound) FILTER (
            WHERE result.outcome_name = 'net_revenue'
              AND result.estimator_name = 'matched_block_bootstrap'
              AND result.period_name = 'campaign_window'
        ) AS matched_net_revenue_lower_95,
        max(result.upper_bound) FILTER (
            WHERE result.outcome_name = 'net_revenue'
              AND result.estimator_name = 'matched_block_bootstrap'
              AND result.period_name = 'campaign_window'
        ) AS matched_net_revenue_upper_95,
        max(result.estimate_value) FILTER (
            WHERE result.outcome_name = 'net_demand'
              AND result.estimator_name = 'matched_block_bootstrap'
              AND result.period_name = 'campaign_window'
        ) AS matched_daily_demand_lift,
        max(result.estimate_pct) FILTER (
            WHERE result.outcome_name = 'net_demand'
              AND result.estimator_name = 'matched_block_bootstrap'
              AND result.period_name = 'campaign_window'
        ) AS matched_demand_lift_pct,
        max(result.estimate_value) FILTER (
            WHERE result.outcome_name = 'net_revenue_per_ticket'
              AND result.estimator_name = 'matched_block_bootstrap'
              AND result.period_name = 'campaign_window'
        ) AS matched_ticket_yield_lift,
        max(result.estimate_pct) FILTER (
            WHERE result.outcome_name = 'net_revenue_per_ticket'
              AND result.estimator_name = 'matched_block_bootstrap'
              AND result.period_name = 'campaign_window'
        ) AS matched_ticket_yield_lift_pct,
        max(result.estimate_pct) FILTER (
            WHERE result.outcome_name = 'net_revenue'
              AND result.estimator_name = 'regression_hac'
              AND result.period_name = 'campaign_window'
        ) AS regression_net_revenue_lift_pct,
        max(result.lower_bound) FILTER (
            WHERE result.outcome_name = 'net_revenue'
              AND result.estimator_name = 'regression_hac'
              AND result.period_name = 'campaign_window'
        ) AS regression_net_revenue_lower_95_pct,
        max(result.upper_bound) FILTER (
            WHERE result.outcome_name = 'net_revenue'
              AND result.estimator_name = 'regression_hac'
              AND result.period_name = 'campaign_window'
        ) AS regression_net_revenue_upper_95_pct,
        max(result.p_value) FILTER (
            WHERE result.outcome_name = 'net_revenue'
              AND result.estimator_name = 'regression_hac'
              AND result.period_name = 'campaign_window'
        ) AS regression_net_revenue_p_value,
        max(result.estimate_pct) FILTER (
            WHERE result.outcome_name = 'net_revenue_per_ticket'
              AND result.estimator_name = 'regression_hac'
              AND result.period_name = 'campaign_window'
        ) AS regression_ticket_yield_lift_pct,
        max(result.lower_bound) FILTER (
            WHERE result.outcome_name = 'net_revenue_per_ticket'
              AND result.estimator_name = 'regression_hac'
              AND result.period_name = 'campaign_window'
        ) AS regression_ticket_yield_lower_95_pct,
        max(result.upper_bound) FILTER (
            WHERE result.outcome_name = 'net_revenue_per_ticket'
              AND result.estimator_name = 'regression_hac'
              AND result.period_name = 'campaign_window'
        ) AS regression_ticket_yield_upper_95_pct
    FROM fact_campaign_evaluation AS result
    JOIN latest_run USING (run_id)
    GROUP BY result.run_id
)
SELECT
    analysis.*,
    evaluation.matched_daily_net_revenue_lift,
    evaluation.matched_net_revenue_lift_pct,
    evaluation.matched_net_revenue_lower_95,
    evaluation.matched_net_revenue_upper_95,
    evaluation.matched_daily_demand_lift,
    evaluation.matched_demand_lift_pct,
    evaluation.matched_ticket_yield_lift,
    evaluation.matched_ticket_yield_lift_pct,
    evaluation.regression_net_revenue_lift_pct,
    evaluation.regression_net_revenue_lower_95_pct,
    evaluation.regression_net_revenue_upper_95_pct,
    evaluation.regression_net_revenue_p_value,
    evaluation.regression_ticket_yield_lift_pct,
    evaluation.regression_ticket_yield_lower_95_pct,
    evaluation.regression_ticket_yield_upper_95_pct,
    CASE
        WHEN evaluation.matched_net_revenue_lower_95 > 0
         AND evaluation.regression_net_revenue_lower_95_pct > 0
            THEN 'positive_associated_net_revenue_lift'
        ELSE 'not_consistently_positive'
    END AS revenue_evidence,
    CASE
        WHEN evaluation.matched_ticket_yield_lift < 0
         AND evaluation.regression_ticket_yield_lower_95_pct <= 0
         AND evaluation.regression_ticket_yield_upper_95_pct >= 0
            THEN 'mixed_ticket_yield_evidence'
        ELSE 'review_ticket_yield_results'
    END AS ticket_yield_evidence
FROM latest_run AS analysis
JOIN evaluation USING (run_id);

COMMENT ON VIEW vw_latest_campaign_evaluation IS
    'Newest campaign analysis estimates by outcome, estimator, and analysis period.';
COMMENT ON VIEW vw_latest_campaign_match IS
    'Newest treatment-to-control date matches and paired outcome differences.';
COMMENT ON VIEW vw_latest_campaign_balance IS
    'Newest pre-match and post-match campaign covariate balance diagnostics.';
COMMENT ON VIEW vw_latest_campaign_analysis_summary IS
    'Newest campaign design, commercial result, robustness evidence, and decision labels.';

COMMIT;
