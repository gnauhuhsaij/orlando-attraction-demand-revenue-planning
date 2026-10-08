-- Orlando Attraction Demand & Revenue Planning
-- PostgreSQL 14+
--
-- All commercial facts in this portfolio project are synthetic. Weather and
-- calendar attributes may be loaded from documented public sources.

BEGIN;

CREATE SCHEMA IF NOT EXISTS analytics;
SET search_path TO analytics, public;

CREATE TABLE IF NOT EXISTS dim_date (
    date_key              integer PRIMARY KEY,
    calendar_date         date NOT NULL UNIQUE,
    day_of_week           smallint NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),
    day_name              varchar(9) NOT NULL,
    day_of_year           smallint NOT NULL CHECK (day_of_year BETWEEN 1 AND 366),
    week_start            date NOT NULL,
    week_of_year          smallint NOT NULL CHECK (week_of_year BETWEEN 1 AND 53),
    month_number          smallint NOT NULL CHECK (month_number BETWEEN 1 AND 12),
    month_name            varchar(9) NOT NULL,
    quarter_number        smallint NOT NULL CHECK (quarter_number BETWEEN 1 AND 4),
    year_number           smallint NOT NULL CHECK (year_number BETWEEN 2000 AND 2100),
    is_weekend            boolean NOT NULL,
    holiday_flag          boolean NOT NULL DEFAULT false,
    holiday_name          varchar(100),
    school_break_flag     boolean NOT NULL DEFAULT false,
    season                varchar(20) NOT NULL
                          CHECK (season IN ('peak', 'shoulder', 'off_peak')),
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_dim_date_key_format
        CHECK (date_key = to_char(calendar_date, 'YYYYMMDD')::integer),
    CONSTRAINT ck_dim_date_holiday_name
        CHECK (
            (holiday_flag AND holiday_name IS NOT NULL)
            OR (NOT holiday_flag AND holiday_name IS NULL)
        )
);

CREATE TABLE IF NOT EXISTS dim_product (
    product_key           integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_code          varchar(30) NOT NULL UNIQUE,
    product_name          varchar(100) NOT NULL,
    product_type          varchar(40) NOT NULL,
    ticket_tier           varchar(20) NOT NULL
                          CHECK (ticket_tier IN ('value', 'standard', 'premium')),
    base_price            numeric(10, 2) NOT NULL CHECK (base_price > 0),
    valid_from            date NOT NULL,
    valid_to              date,
    is_active             boolean NOT NULL DEFAULT true,
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_dim_product_valid_dates
        CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE IF NOT EXISTS dim_channel (
    channel_key           integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_code          varchar(30) NOT NULL UNIQUE,
    channel_name          varchar(100) NOT NULL,
    channel_type          varchar(20) NOT NULL
                          CHECK (channel_type IN ('direct', 'partner', 'group')),
    is_active             boolean NOT NULL DEFAULT true,
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dim_campaign (
    campaign_key          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_code         varchar(40) NOT NULL UNIQUE,
    campaign_name         varchar(120) NOT NULL,
    campaign_type         varchar(20) NOT NULL
                          CHECK (
                              campaign_type IN (
                                  'none', 'discount', 'paid_media', 'email',
                                  'partnership', 'bundle'
                              )
                          ),
    target_segment        varchar(80) NOT NULL,
    start_date            date NOT NULL,
    end_date              date NOT NULL,
    discount_type         varchar(10) NOT NULL DEFAULT 'none'
                          CHECK (discount_type IN ('none', 'percent', 'fixed')),
    planned_discount_value numeric(10, 2) NOT NULL DEFAULT 0
                          CHECK (planned_discount_value >= 0),
    is_no_campaign        boolean NOT NULL DEFAULT false,
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_dim_campaign_dates
        CHECK (end_date >= start_date),
    CONSTRAINT ck_dim_campaign_discount
        CHECK (
            (discount_type = 'none' AND planned_discount_value = 0)
            OR (discount_type <> 'none' AND planned_discount_value > 0)
        ),
    CONSTRAINT ck_dim_campaign_percent_discount
        CHECK (discount_type <> 'percent' OR planned_discount_value <= 100),
    CONSTRAINT ck_dim_campaign_no_campaign
        CHECK (NOT is_no_campaign OR campaign_type = 'none')
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_campaign_single_no_campaign
    ON dim_campaign (is_no_campaign)
    WHERE is_no_campaign;

CREATE TABLE IF NOT EXISTS fact_ticket_sales (
    ticket_sale_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id              varchar(40) NOT NULL,
    order_line_number     smallint NOT NULL CHECK (order_line_number > 0),
    purchase_date_key     integer NOT NULL REFERENCES dim_date (date_key),
    visit_date_key        integer NOT NULL REFERENCES dim_date (date_key),
    refund_date_key       integer REFERENCES dim_date (date_key),
    product_key           integer NOT NULL REFERENCES dim_product (product_key),
    channel_key           integer NOT NULL REFERENCES dim_channel (channel_key),
    campaign_key          integer NOT NULL REFERENCES dim_campaign (campaign_key),
    units_sold            integer NOT NULL CHECK (units_sold > 0),
    units_refunded        integer NOT NULL DEFAULT 0,
    net_units             integer GENERATED ALWAYS AS
                          (units_sold - units_refunded) STORED,
    unit_list_price       numeric(10, 2) NOT NULL CHECK (unit_list_price > 0),
    gross_revenue         numeric(12, 2) NOT NULL CHECK (gross_revenue >= 0),
    discount_amount       numeric(12, 2) NOT NULL DEFAULT 0
                          CHECK (discount_amount >= 0),
    refund_amount         numeric(12, 2) NOT NULL DEFAULT 0
                          CHECK (refund_amount >= 0),
    net_revenue           numeric(12, 2) NOT NULL CHECK (net_revenue >= 0),
    sale_status           varchar(20) NOT NULL DEFAULT 'active'
                          CHECK (
                              sale_status IN (
                                  'active', 'partially_refunded',
                                  'refunded', 'cancelled'
                              )
                          ),
    currency_code         char(3) NOT NULL DEFAULT 'USD'
                          CHECK (currency_code = 'USD'),
    booked_at             timestamptz NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_fact_ticket_sales_order_line
        UNIQUE (order_id, order_line_number),
    CONSTRAINT ck_fact_ticket_sales_visit_after_purchase
        CHECK (visit_date_key >= purchase_date_key),
    CONSTRAINT ck_fact_ticket_sales_refund_date
        CHECK (refund_date_key IS NULL OR refund_date_key >= purchase_date_key),
    CONSTRAINT ck_fact_ticket_sales_refunded_units
        CHECK (units_refunded BETWEEN 0 AND units_sold),
    CONSTRAINT ck_fact_ticket_sales_gross_revenue
        CHECK (gross_revenue = round(units_sold * unit_list_price, 2)),
    CONSTRAINT ck_fact_ticket_sales_discount_limit
        CHECK (discount_amount <= gross_revenue),
    CONSTRAINT ck_fact_ticket_sales_refund_limit
        CHECK (discount_amount + refund_amount <= gross_revenue),
    CONSTRAINT ck_fact_ticket_sales_net_revenue
        CHECK (
            net_revenue = round(
                gross_revenue - discount_amount - refund_amount,
                2
            )
        ),
    CONSTRAINT ck_fact_ticket_sales_active_status
        CHECK (
            sale_status <> 'active'
            OR (refund_amount = 0 AND units_refunded = 0 AND refund_date_key IS NULL)
        ),
    CONSTRAINT ck_fact_ticket_sales_partial_refund_status
        CHECK (
            sale_status <> 'partially_refunded'
            OR (refund_amount > 0 AND refund_date_key IS NOT NULL AND net_revenue > 0)
        ),
    CONSTRAINT ck_fact_ticket_sales_closed_status
        CHECK (
            sale_status NOT IN ('refunded', 'cancelled')
            OR (
                units_refunded = units_sold
                AND refund_amount = gross_revenue - discount_amount
                AND net_revenue = 0
                AND refund_date_key IS NOT NULL
            )
        )
);

CREATE TABLE IF NOT EXISTS fact_weather (
    date_key              integer PRIMARY KEY REFERENCES dim_date (date_key),
    min_temperature_f     numeric(5, 2) NOT NULL
                          CHECK (min_temperature_f BETWEEN -20 AND 130),
    avg_temperature_f     numeric(5, 2) NOT NULL
                          CHECK (avg_temperature_f BETWEEN -20 AND 130),
    max_temperature_f     numeric(5, 2) NOT NULL
                          CHECK (max_temperature_f BETWEEN -20 AND 130),
    precipitation_in      numeric(6, 3) NOT NULL DEFAULT 0
                          CHECK (precipitation_in BETWEEN 0 AND 100),
    severe_weather_flag   boolean NOT NULL DEFAULT false,
    source_name           varchar(80) NOT NULL,
    source_station_id     varchar(40),
    loaded_at             timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_fact_weather_temperature_order
        CHECK (
            min_temperature_f <= avg_temperature_f
            AND avg_temperature_f <= max_temperature_f
        )
);

CREATE TABLE IF NOT EXISTS fact_campaign_daily (
    date_key              integer NOT NULL REFERENCES dim_date (date_key),
    campaign_key          integer NOT NULL REFERENCES dim_campaign (campaign_key),
    spend                 numeric(12, 2) NOT NULL DEFAULT 0 CHECK (spend >= 0),
    impressions           integer NOT NULL DEFAULT 0 CHECK (impressions >= 0),
    clicks                integer NOT NULL DEFAULT 0 CHECK (clicks >= 0),
    conversions           integer NOT NULL DEFAULT 0 CHECK (conversions >= 0),
    attributed_revenue    numeric(12, 2) NOT NULL DEFAULT 0
                          CHECK (attributed_revenue >= 0),
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date_key, campaign_key),
    CONSTRAINT ck_fact_campaign_daily_funnel
        CHECK (conversions <= clicks AND clicks <= impressions)
);

CREATE TABLE IF NOT EXISTS fact_daily_plan (
    date_key              integer PRIMARY KEY REFERENCES dim_date (date_key),
    planned_price_multiplier numeric(7, 4) NOT NULL DEFAULT 1.0000,
    available_capacity    integer NOT NULL CHECK (available_capacity > 0),
    demand_target         integer NOT NULL CHECK (demand_target >= 0),
    revenue_target        numeric(12, 2) NOT NULL CHECK (revenue_target >= 0),
    planned_staff_hours   numeric(10, 2) NOT NULL CHECK (planned_staff_hours >= 0),
    plan_version          varchar(30) NOT NULL DEFAULT 'initial_plan',
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_fact_daily_plan_price
        CHECK (planned_price_multiplier BETWEEN 0.85 AND 1.30),
    CONSTRAINT ck_fact_daily_plan_target_capacity
        CHECK (demand_target <= available_capacity)
);

-- Keep existing local databases compatible when the rerunnable schema is applied.
ALTER TABLE fact_daily_plan
    ADD COLUMN IF NOT EXISTS planned_price_multiplier numeric(7, 4)
    NOT NULL DEFAULT 1.0000;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_fact_daily_plan_price'
          AND conrelid = 'fact_daily_plan'::regclass
    ) THEN
        ALTER TABLE fact_daily_plan
            ADD CONSTRAINT ck_fact_daily_plan_price
            CHECK (planned_price_multiplier BETWEEN 0.85 AND 1.30);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS fact_forecast (
    forecast_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id                varchar(64) NOT NULL,
    forecast_run_type     varchar(15) NOT NULL,
    forecast_created_date_key integer NOT NULL REFERENCES dim_date (date_key),
    training_end_date_key integer NOT NULL REFERENCES dim_date (date_key),
    target_date_key       integer NOT NULL REFERENCES dim_date (date_key),
    model_name            varchar(80) NOT NULL,
    model_version         varchar(40) NOT NULL,
    model_role            varchar(15) NOT NULL
                          CHECK (model_role IN ('baseline', 'challenger', 'selected')),
    forecast_horizon_days smallint NOT NULL
                          CHECK (forecast_horizon_days BETWEEN 1 AND 365),
    predicted_demand      numeric(12, 2) NOT NULL CHECK (predicted_demand >= 0),
    lower_bound           numeric(12, 2) CHECK (lower_bound >= 0),
    upper_bound           numeric(12, 2) CHECK (upper_bound >= 0),
    interval_confidence   numeric(4, 3)
                          CHECK (interval_confidence > 0 AND interval_confidence < 1),
    interval_method       varchar(40),
    calibration_observations integer,
    actual_demand         integer CHECK (actual_demand >= 0),
    actual_loaded_at      timestamptz,
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_fact_forecast_run_target
        UNIQUE (run_id, target_date_key),
    CONSTRAINT ck_fact_forecast_run_type
        CHECK (forecast_run_type IN ('backtest', 'production')),
    CONSTRAINT ck_fact_forecast_calibration_observations
        CHECK (calibration_observations >= 0),
    CONSTRAINT ck_fact_forecast_date_order
        CHECK (
            training_end_date_key <= forecast_created_date_key
            AND forecast_created_date_key < target_date_key
        ),
    CONSTRAINT ck_fact_forecast_bounds
        CHECK (
            (
                lower_bound IS NULL
                AND upper_bound IS NULL
                AND interval_confidence IS NULL
                AND interval_method IS NULL
                AND calibration_observations IS NULL
            )
            OR (
                lower_bound IS NOT NULL
                AND upper_bound IS NOT NULL
                AND interval_confidence IS NOT NULL
                AND interval_method IS NOT NULL
                AND calibration_observations IS NOT NULL
                AND lower_bound <= predicted_demand
                AND predicted_demand <= upper_bound
            )
        ),
    CONSTRAINT ck_fact_forecast_actual_loaded
        CHECK (
            (actual_demand IS NULL AND actual_loaded_at IS NULL)
            OR (actual_demand IS NOT NULL AND actual_loaded_at IS NOT NULL)
        )
);

CREATE TABLE IF NOT EXISTS fact_revenue_forecast (
    revenue_forecast_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id                varchar(64) NOT NULL,
    forecast_run_type     varchar(15) NOT NULL,
    forecast_created_date_key integer NOT NULL REFERENCES dim_date (date_key),
    training_end_date_key integer NOT NULL REFERENCES dim_date (date_key),
    target_date_key       integer NOT NULL REFERENCES dim_date (date_key),
    revenue_model_name    varchar(120) NOT NULL,
    model_version         varchar(40) NOT NULL,
    model_role            varchar(15) NOT NULL
                          CHECK (model_role IN ('baseline', 'challenger', 'selected')),
    demand_model_name     varchar(80) NOT NULL,
    product_mix_model_name varchar(80) NOT NULL,
    product_yield_model_name varchar(80) NOT NULL,
    forecast_horizon_days smallint NOT NULL
                          CHECK (forecast_horizon_days BETWEEN 1 AND 365),
    predicted_demand      numeric(12, 2) NOT NULL CHECK (predicted_demand >= 0),
    predicted_net_revenue numeric(14, 2) NOT NULL
                          CHECK (predicted_net_revenue >= 0),
    lower_bound           numeric(14, 2) CHECK (lower_bound >= 0),
    upper_bound           numeric(14, 2) CHECK (upper_bound >= 0),
    interval_confidence   numeric(4, 3)
                          CHECK (interval_confidence > 0 AND interval_confidence < 1),
    interval_method       varchar(40),
    calibration_observations integer,
    actual_demand         integer CHECK (actual_demand >= 0),
    actual_net_revenue    numeric(14, 2) CHECK (actual_net_revenue >= 0),
    actual_loaded_at      timestamptz,
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_fact_revenue_forecast_run_target
        UNIQUE (run_id, target_date_key),
    CONSTRAINT ck_fact_revenue_forecast_run_type
        CHECK (forecast_run_type IN ('backtest', 'production')),
    CONSTRAINT ck_fact_revenue_forecast_calibration
        CHECK (calibration_observations >= 0),
    CONSTRAINT ck_fact_revenue_forecast_date_order
        CHECK (
            training_end_date_key <= forecast_created_date_key
            AND forecast_created_date_key < target_date_key
        ),
    CONSTRAINT ck_fact_revenue_forecast_bounds
        CHECK (
            (
                lower_bound IS NULL
                AND upper_bound IS NULL
                AND interval_confidence IS NULL
                AND interval_method IS NULL
                AND calibration_observations IS NULL
            )
            OR (
                lower_bound IS NOT NULL
                AND upper_bound IS NOT NULL
                AND interval_confidence IS NOT NULL
                AND interval_method IS NOT NULL
                AND calibration_observations IS NOT NULL
                AND lower_bound <= predicted_net_revenue
                AND predicted_net_revenue <= upper_bound
            )
        ),
    CONSTRAINT ck_fact_revenue_forecast_actuals
        CHECK (
            (
                actual_demand IS NULL
                AND actual_net_revenue IS NULL
                AND actual_loaded_at IS NULL
            )
            OR (
                actual_demand IS NOT NULL
                AND actual_net_revenue IS NOT NULL
                AND actual_loaded_at IS NOT NULL
            )
        )
);

CREATE TABLE IF NOT EXISTS fact_product_revenue_forecast (
    product_revenue_forecast_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id                varchar(64) NOT NULL,
    target_date_key       integer NOT NULL REFERENCES dim_date (date_key),
    product_key           integer NOT NULL REFERENCES dim_product (product_key),
    predicted_product_share numeric(12, 8) NOT NULL
                          CHECK (
                              predicted_product_share >= 0
                              AND predicted_product_share <= 1
                          ),
    predicted_product_demand numeric(14, 4) NOT NULL
                          CHECK (predicted_product_demand >= 0),
    predicted_product_net_yield numeric(12, 4) NOT NULL
                          CHECK (predicted_product_net_yield > 0),
    predicted_product_net_revenue numeric(14, 2) NOT NULL
                          CHECK (predicted_product_net_revenue >= 0),
    actual_product_share  numeric(12, 8)
                          CHECK (
                              actual_product_share >= 0
                              AND actual_product_share <= 1
                          ),
    actual_product_demand integer CHECK (actual_product_demand >= 0),
    actual_product_net_yield numeric(12, 4)
                          CHECK (actual_product_net_yield > 0),
    actual_product_net_revenue numeric(14, 2)
                          CHECK (actual_product_net_revenue >= 0),
    actual_loaded_at      timestamptz,
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_fact_product_revenue_forecast_run_target_product
        UNIQUE (run_id, target_date_key, product_key),
    CONSTRAINT fk_fact_product_revenue_forecast_daily
        FOREIGN KEY (run_id, target_date_key)
        REFERENCES fact_revenue_forecast (run_id, target_date_key)
        ON DELETE CASCADE,
    CONSTRAINT ck_fact_product_revenue_forecast_actuals
        CHECK (
            (
                actual_product_share IS NULL
                AND actual_product_demand IS NULL
                AND actual_product_net_yield IS NULL
                AND actual_product_net_revenue IS NULL
                AND actual_loaded_at IS NULL
            )
            OR (
                actual_product_share IS NOT NULL
                AND actual_product_demand IS NOT NULL
                AND actual_product_net_yield IS NOT NULL
                AND actual_product_net_revenue IS NOT NULL
                AND actual_loaded_at IS NOT NULL
            )
        )
);

CREATE TABLE IF NOT EXISTS fact_campaign_analysis_run (
    run_id                varchar(64) PRIMARY KEY,
    campaign_family       varchar(40) NOT NULL,
    analysis_version      varchar(40) NOT NULL,
    treatment_definition  text NOT NULL,
    control_definition    text NOT NULL,
    primary_outcome       varchar(40) NOT NULL,
    treatment_days        integer NOT NULL CHECK (treatment_days > 0),
    control_pool_days     integer NOT NULL CHECK (control_pool_days > 0),
    matched_pairs         integer NOT NULL CHECK (matched_pairs > 0),
    matching_ratio        smallint NOT NULL CHECK (matching_ratio > 0),
    balance_threshold     numeric(8, 6) NOT NULL
                          CHECK (balance_threshold > 0),
    matched_balance_passed boolean NOT NULL,
    bootstrap_samples     integer NOT NULL CHECK (bootstrap_samples >= 100),
    bootstrap_block_days  smallint NOT NULL CHECK (bootstrap_block_days > 0),
    marketing_spend       numeric(14, 2) NOT NULL CHECK (marketing_spend >= 0),
    attributed_discount_amount numeric(14, 2) NOT NULL
                          CHECK (attributed_discount_amount >= 0),
    estimated_incremental_net_revenue numeric(16, 2) NOT NULL,
    incremental_revenue_lower_95 numeric(16, 2) NOT NULL,
    incremental_revenue_upper_95 numeric(16, 2) NOT NULL,
    estimated_net_revenue_after_marketing numeric(16, 2) NOT NULL,
    associated_revenue_on_marketing_spend numeric(14, 6),
    associated_return_after_marketing_spend numeric(14, 6),
    pre_period_revenue_placebo_p_value numeric(12, 10) NOT NULL
                          CHECK (
                              pre_period_revenue_placebo_p_value BETWEEN 0 AND 1
                          ),
    causal_claim_allowed  boolean NOT NULL DEFAULT false
                          CHECK (NOT causal_claim_allowed),
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_fact_campaign_analysis_run_interval
        CHECK (
            incremental_revenue_lower_95
            <= estimated_incremental_net_revenue
            AND estimated_incremental_net_revenue
            <= incremental_revenue_upper_95
        ),
    CONSTRAINT ck_fact_campaign_analysis_run_matching
        CHECK (matched_pairs = treatment_days * matching_ratio)
);

CREATE TABLE IF NOT EXISTS fact_campaign_evaluation (
    campaign_evaluation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id                varchar(64) NOT NULL
                          REFERENCES fact_campaign_analysis_run (run_id)
                          ON DELETE CASCADE,
    outcome_name          varchar(40) NOT NULL,
    estimator_name        varchar(40) NOT NULL
                          CHECK (
                              estimator_name IN (
                                  'matched_block_bootstrap',
                                  'regression_hac'
                              )
                          ),
    period_name           varchar(30) NOT NULL
                          CHECK (
                              period_name IN (
                                  'campaign_window',
                                  'pre_period_placebo'
                              )
                          ),
    observations          integer NOT NULL CHECK (observations > 0),
    estimate_value        numeric(18, 6) NOT NULL,
    estimate_pct          numeric(14, 6) NOT NULL,
    lower_bound           numeric(18, 6) NOT NULL,
    upper_bound           numeric(18, 6) NOT NULL,
    estimate_unit         varchar(30) NOT NULL,
    p_value               numeric(12, 10)
                          CHECK (p_value BETWEEN 0 AND 1),
    effect_size           numeric(14, 8),
    interval_excludes_zero boolean NOT NULL,
    is_primary_outcome    boolean NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_fact_campaign_evaluation_result
        UNIQUE (run_id, outcome_name, estimator_name, period_name),
    CONSTRAINT ck_fact_campaign_evaluation_interval
        CHECK (lower_bound <= estimate_value AND estimate_value <= upper_bound)
);

CREATE TABLE IF NOT EXISTS fact_campaign_match (
    run_id                varchar(64) NOT NULL
                          REFERENCES fact_campaign_analysis_run (run_id)
                          ON DELETE CASCADE,
    treatment_date_key    integer NOT NULL REFERENCES dim_date (date_key),
    control_date_key      integer NOT NULL REFERENCES dim_date (date_key),
    campaign_year         smallint NOT NULL,
    season                varchar(20) NOT NULL,
    day_of_week           smallint NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),
    match_distance        numeric(14, 8) NOT NULL CHECK (match_distance >= 0),
    treated_net_demand    integer NOT NULL CHECK (treated_net_demand >= 0),
    control_net_demand    integer NOT NULL CHECK (control_net_demand >= 0),
    demand_lift           integer NOT NULL,
    treated_net_revenue   numeric(14, 2) NOT NULL CHECK (treated_net_revenue >= 0),
    control_net_revenue   numeric(14, 2) NOT NULL CHECK (control_net_revenue >= 0),
    net_revenue_lift      numeric(14, 2) NOT NULL,
    treated_net_revenue_per_ticket numeric(12, 4) NOT NULL
                          CHECK (treated_net_revenue_per_ticket > 0),
    control_net_revenue_per_ticket numeric(12, 4) NOT NULL
                          CHECK (control_net_revenue_per_ticket > 0),
    net_revenue_per_ticket_lift numeric(12, 4) NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, treatment_date_key),
    CONSTRAINT ck_fact_campaign_match_distinct_dates
        CHECK (treatment_date_key <> control_date_key),
    CONSTRAINT ck_fact_campaign_match_demand_lift
        CHECK (demand_lift = treated_net_demand - control_net_demand),
    CONSTRAINT ck_fact_campaign_match_revenue_lift
        CHECK (
            net_revenue_lift
            = round(treated_net_revenue - control_net_revenue, 2)
        )
);

CREATE TABLE IF NOT EXISTS fact_campaign_balance (
    run_id                varchar(64) NOT NULL
                          REFERENCES fact_campaign_analysis_run (run_id)
                          ON DELETE CASCADE,
    covariate_name        varchar(60) NOT NULL,
    raw_smd               numeric(14, 8) NOT NULL,
    matched_smd           numeric(14, 8) NOT NULL,
    balance_threshold     numeric(8, 6) NOT NULL
                          CHECK (balance_threshold > 0),
    balance_passed        boolean NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, covariate_name),
    CONSTRAINT ck_fact_campaign_balance_pass
        CHECK (
            balance_passed
            = (abs(matched_smd) <= balance_threshold)
        )
);

-- Keep pre-forecast local databases compatible when this schema is reapplied.
ALTER TABLE fact_forecast
    ADD COLUMN IF NOT EXISTS forecast_run_type varchar(15)
    NOT NULL DEFAULT 'production';
ALTER TABLE fact_forecast ALTER COLUMN forecast_run_type DROP DEFAULT;
ALTER TABLE fact_forecast
    ADD COLUMN IF NOT EXISTS interval_method varchar(40);
ALTER TABLE fact_forecast
    ADD COLUMN IF NOT EXISTS calibration_observations integer;
ALTER TABLE fact_forecast ALTER COLUMN lower_bound DROP NOT NULL;
ALTER TABLE fact_forecast ALTER COLUMN upper_bound DROP NOT NULL;
ALTER TABLE fact_forecast ALTER COLUMN interval_confidence DROP NOT NULL;
ALTER TABLE fact_forecast ALTER COLUMN interval_confidence DROP DEFAULT;
ALTER TABLE fact_forecast DROP CONSTRAINT IF EXISTS ck_fact_forecast_bounds;
ALTER TABLE fact_forecast
    ADD CONSTRAINT ck_fact_forecast_bounds
    CHECK (
        (
            lower_bound IS NULL
            AND upper_bound IS NULL
            AND interval_confidence IS NULL
            AND interval_method IS NULL
            AND calibration_observations IS NULL
        )
        OR (
            lower_bound IS NOT NULL
            AND upper_bound IS NOT NULL
            AND interval_confidence IS NOT NULL
            AND interval_method IS NOT NULL
            AND calibration_observations IS NOT NULL
            AND lower_bound <= predicted_demand
            AND predicted_demand <= upper_bound
        )
    );

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_fact_forecast_run_type'
          AND conrelid = 'fact_forecast'::regclass
    ) THEN
        ALTER TABLE fact_forecast
            ADD CONSTRAINT ck_fact_forecast_run_type
            CHECK (forecast_run_type IN ('backtest', 'production'));
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_fact_forecast_calibration_observations'
          AND conrelid = 'fact_forecast'::regclass
    ) THEN
        ALTER TABLE fact_forecast
            ADD CONSTRAINT ck_fact_forecast_calibration_observations
            CHECK (calibration_observations >= 0);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_fact_ticket_sales_purchase_date
    ON fact_ticket_sales (purchase_date_key);
CREATE INDEX IF NOT EXISTS ix_fact_ticket_sales_visit_date
    ON fact_ticket_sales (visit_date_key);
CREATE INDEX IF NOT EXISTS ix_fact_ticket_sales_product
    ON fact_ticket_sales (product_key);
CREATE INDEX IF NOT EXISTS ix_fact_ticket_sales_channel
    ON fact_ticket_sales (channel_key);
CREATE INDEX IF NOT EXISTS ix_fact_ticket_sales_campaign
    ON fact_ticket_sales (campaign_key);
CREATE INDEX IF NOT EXISTS ix_fact_forecast_target_date
    ON fact_forecast (target_date_key);
CREATE INDEX IF NOT EXISTS ix_fact_forecast_model
    ON fact_forecast (model_name, model_version);
CREATE INDEX IF NOT EXISTS ix_fact_forecast_run_type_created
    ON fact_forecast (forecast_run_type, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_fact_revenue_forecast_target_date
    ON fact_revenue_forecast (target_date_key);
CREATE INDEX IF NOT EXISTS ix_fact_revenue_forecast_run_type_created
    ON fact_revenue_forecast (forecast_run_type, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_fact_product_revenue_forecast_target
    ON fact_product_revenue_forecast (target_date_key, product_key);
CREATE INDEX IF NOT EXISTS ix_fact_campaign_analysis_created
    ON fact_campaign_analysis_run (created_at DESC, run_id DESC);
CREATE INDEX IF NOT EXISTS ix_fact_campaign_evaluation_run
    ON fact_campaign_evaluation (run_id, outcome_name, estimator_name);
CREATE INDEX IF NOT EXISTS ix_fact_campaign_match_dates
    ON fact_campaign_match (treatment_date_key, control_date_key);

COMMENT ON SCHEMA analytics IS
    'Analytical star schema for the Orlando attraction planning portfolio project.';

COMMENT ON TABLE dim_date IS
    'One row per calendar date; public calendar attributes and documented break indicators.';
COMMENT ON TABLE dim_product IS
    'One row per synthetic ticket product.';
COMMENT ON TABLE dim_channel IS
    'One row per synthetic sales channel.';
COMMENT ON TABLE dim_campaign IS
    'One row per synthetic campaign; spend is stored by date in fact_campaign_daily.';
COMMENT ON TABLE fact_ticket_sales IS
    'One synthetic order line for one product; purchase and planned visit dates are both retained.';
COMMENT ON COLUMN fact_ticket_sales.net_units IS
    'Forecast actual: units_sold minus units_refunded, aggregated by visit_date_key.';
COMMENT ON TABLE fact_weather IS
    'One row per Orlando calendar date from a documented weather source.';
COMMENT ON TABLE fact_campaign_daily IS
    'One row per campaign per calendar date; contains synthetic delivery and spend measures.';
COMMENT ON TABLE fact_daily_plan IS
    'One row per visit date containing synthetic planned price, capacity, revenue target, and staffing plan.';
COMMENT ON TABLE fact_forecast IS
    'One row per model run and target date; historical runs are retained for backtesting.';
COMMENT ON COLUMN fact_forecast.forecast_run_type IS
    'Separates historical out-of-sample backtests from live production-horizon forecasts.';
COMMENT ON COLUMN fact_forecast.actual_demand IS
    'Observed net ticket demand by visit date, populated only when that target is available.';
COMMENT ON TABLE fact_revenue_forecast IS
    'One row per final revenue model run and target date, retaining backtests and production forecasts.';
COMMENT ON TABLE fact_product_revenue_forecast IS
    'Product-level demand share, net yield, and revenue components for each retained revenue forecast.';
COMMENT ON TABLE fact_campaign_analysis_run IS
    'One versioned observational campaign analysis run with design and commercial summary metadata.';
COMMENT ON TABLE fact_campaign_evaluation IS
    'Outcome estimates from matched-pair and regression sensitivity estimators for each campaign analysis run.';
COMMENT ON TABLE fact_campaign_match IS
    'One treatment date and its selected no-campaign control date for each campaign analysis run.';
COMMENT ON TABLE fact_campaign_balance IS
    'Pre-match and post-match standardized mean differences for each observed campaign covariate.';
COMMENT ON COLUMN fact_revenue_forecast.actual_net_revenue IS
    'Observed net revenue by visit date, populated only when the target date has actuals.';

COMMIT;
