# Orlando Attraction Demand & Revenue Planning

An end-to-end analytics project that forecasts daily attraction demand and translates the results into pricing, marketing, capacity, and revenue-planning recommendations.

## Business Questions

1. How much demand should the business expect on each of the next 30 days?
2. Are changes in demand most associated with price, weather, holidays, school breaks, or marketing activity?
3. Which dates are likely to be capacity-constrained, and which dates may require promotion?

## Data

The project combines documented public contextual data with transparently generated synthetic commercial data.

- Public data: Orlando weather, federal holidays, selected school-break calendars, and search-interest signals where reproducible
- Synthetic data: ticket sales, prices, discounts, refunds, campaigns, marketing spend, capacity, and revenue targets
- Derived data: model features, forecasts, forecast errors, campaign estimates, and daily action recommendations

This portfolio project does not use or claim access to Disney, Universal, or another attraction operator's internal data.

## Approach

- Model the analytical data in PostgreSQL using a documented star schema
- Validate primary keys, foreign keys, date coverage, and financial relationships with automated SQL and Python checks
- Compare seasonal-naive and moving-average baselines with interpretable statistical and machine-learning models
- Evaluate forecasts through rolling time-based backtests using MAE, RMSE, WAPE, MAPE, and bias
- Estimate campaign-associated demand and net-revenue lift while controlling for observable demand drivers
- Convert forecasts into capacity-risk, demand-gap, promotion, and weather-risk actions

Core tools: PostgreSQL, SQL, Python, Tableau, Excel, and GitHub Actions.

## Deliverables

- Reproducible data-generation and ETL pipeline
- SQL schema, validation tests, and business analysis queries
- Versioned 30-day demand and product-level revenue forecasts with uncertainty ranges
- Daily pricing, marketing, and operating action table
- Tableau decision dashboard and Excel planning workbook
- Executive recommendation memo and technical documentation
