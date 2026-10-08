# Orlando Attraction Demand & Revenue Planning

An end-to-end planning system that converts daily attraction forecasts into pricing, marketing, capacity, and revenue actions.

## Business Problem

The project is designed for a revenue-planning team that needs to answer three questions before the next operating month:

1. How many tickets and how much net revenue should be expected each day?
2. Which changes are associated with price, weather, holidays, school breaks, and campaigns?
3. Which dates need a pricing, promotion, or capacity decision?

## Decision Story

The January 2026 forecast expects **37,722 tickets** and **$4.23M in net revenue**. Peak expected utilization is **71.9%**, so the current plan does not indicate a capacity constraint. Two dates need review: protect price on **Jan 10**, when demand is strongest, and monitor revenue on **Jan 19**, when forecast revenue is below plan.

[![Thirty-day demand, revenue, and recommended actions](outputs/figures/30-Day%20Outlook%20%26%20Actions.png)](tableau/orlando_demand_revenue_planning.twbx)

Rolling backtests show a daily demand error of **103.3 tickets (MAE)** and a revenue error of **7.5% (WAPE)**. Campaign dates produced **8.3% more revenue than matched similar dates** and an estimated **$11.74 net gain per $1 of spend**. This is an observational association, not a causal claim.

[![Revenue drivers, campaign comparison, and forecast confidence](outputs/figures/Revenue%20Drivers%20%26%20Confidence.png)](tableau/orlando_demand_revenue_planning.twbx)

Open the packaged [Tableau workbook](tableau/orlando_demand_revenue_planning.twbx) to explore both decision pages.

## Data & Method

- **Public context:** NOAA Orlando weather, U.S. federal holidays, selected school-break calendars, and reproducible search-interest signals
- **Synthetic commercial data:** anonymized ticket sales, prices, discounts, refunds, campaigns, spend, capacity, and targets
- **Data platform:** PostgreSQL star schema with transactional loading, dependency-aware transformations, SHA-256 source manifests, and automated quality checks
- **Demand forecast:** seasonal-naive and moving-average baselines compared with regression, gradient boosting, SARIMAX, and LSTM challengers using the same 360 rolling-origin observations
- **Revenue forecast:** selected SARIMAX demand forecast allocated by histogram gradient boosting product mix, then valued with ridge product-level net yield
- **Campaign analysis:** treated dates matched to comparable non-campaign dates, with covariate-balance checks and a regression sensitivity model

The pipeline stores versioned forecasts, uncertainty intervals, backtest results, campaign estimates, and daily business actions in PostgreSQL. It does not use or claim access to Disney, Universal, or any other operator's internal data.

## Reproduce

Requirements: Python 3.11+, PostgreSQL, and Tableau Desktop or Tableau Public.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
createdb orlando_demand_revenue

.venv/bin/python -m src.download_public_data
.venv/bin/python -m src.prepare_calendar_data
.venv/bin/python -m src.generate_sales_data
.venv/bin/python -m src.load_postgres
.venv/bin/python -m src.forecast all
.venv/bin/python -m src.forecast_revenue all
.venv/bin/python -m src.analyze_campaign
.venv/bin/python tableau/build_workbook.py
.venv/bin/python -m pytest
```

The main deliverables are the SQL model and validation suite in [`sql/`](sql/), production pipelines in [`src/`](src/), review exports in [`outputs/`](outputs/), and the two-page Tableau decision story in [`tableau/`](tableau/).
