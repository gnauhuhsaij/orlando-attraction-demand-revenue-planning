-- Purpose: Compare monthly demand and revenue by product and sales channel.
-- Grain: One row per purchase month, product, and channel.
-- Used by: Marketing & Revenue dashboard segment filters.

SET search_path TO analytics, public;

SELECT
    month_start,
    product_name,
    ticket_tier,
    channel_name,
    channel_type,
    orders,
    net_units,
    net_revenue,
    average_list_price,
    average_net_revenue_per_ticket,
    discount_rate,
    unit_refund_rate
FROM vw_monthly_channel_product_performance
ORDER BY month_start, product_name, channel_name;
