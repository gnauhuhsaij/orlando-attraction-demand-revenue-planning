from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.generate_sales_data import (
    SyntheticDataError,
    _weather_factor,
    build_campaign_daily,
    build_campaign_dimension,
    build_channel_dimension,
    build_daily_plan,
    build_demand_drivers,
    build_price_plan,
    build_product_dimension,
    generate_ticket_sales,
    validate_synthetic_outputs,
)


def _public_inputs_fixture() -> dict[str, pd.DataFrame]:
    all_dates = pd.date_range("2022-10-03", "2026-01-30", freq="D")
    holiday_flag = all_dates.strftime("%m-%d").isin(["01-01", "07-04", "12-25"])
    school_break_flag = all_dates.month.isin([6, 7])
    season = np.where(
        holiday_flag | school_break_flag,
        "peak",
        np.where(all_dates.month.isin([1, 2, 9, 10]), "off_peak", "shoulder"),
    )
    date_dimension = pd.DataFrame(
        {
            "date_key": all_dates.strftime("%Y%m%d").astype(int),
            "calendar_date": all_dates.strftime("%Y-%m-%d"),
            "day_of_week": all_dates.dayofweek + 1,
            "is_weekend": (all_dates.dayofweek + 1) >= 6,
            "holiday_flag": holiday_flag,
            "school_break_flag": school_break_flag,
            "season": season,
            "year_number": all_dates.year,
        }
    )

    history_dates = pd.date_range("2023-01-01", "2025-12-31", freq="D")
    temperature = 77 + 8 * np.sin(np.arange(len(history_dates)) * 2 * np.pi / 365)
    weather = pd.DataFrame(
        {
            "date_key": history_dates.strftime("%Y%m%d").astype(int),
            "min_temperature_f": temperature - 9,
            "avg_temperature_f": temperature,
            "max_temperature_f": temperature + 9,
            "precipitation_in": np.where(history_dates.day % 9 == 0, 0.6, 0.0),
            "severe_weather_flag": history_dates.day % 37 == 0,
            "source_name": "test fixture",
            "source_station_id": "TEST",
        }
    )

    months = pd.date_range("2023-01-01", "2025-12-01", freq="MS")
    seasonal_wave = np.sin(np.arange(len(months)) * 2 * np.pi / 12)
    mco = pd.DataFrame(
        {
            "month_start": months.strftime("%Y-%m-%d"),
            "enplaned_passengers": (2_300_000 + 180_000 * seasonal_wave).round(),
        }
    )
    tdt = pd.DataFrame(
        {
            "month_start": months.strftime("%Y-%m-%d"),
            "remittance_usd": (31_000_000 + 3_000_000 * seasonal_wave).round(),
        }
    )
    return {"date": date_dimension, "weather": weather, "mco": mco, "tdt": tdt}


def test_dimensions_match_documented_business_assumptions() -> None:
    products = build_product_dimension()
    channels = build_channel_dimension()
    campaigns = build_campaign_dimension()

    assert len(products) == 4
    assert products["base_price"].tolist() == [119, 99, 149, 79]
    assert len(channels) == 5
    assert set(channels["channel_type"]) == {"direct", "partner", "group"}
    assert len(campaigns) == 13
    assert int(campaigns["is_no_campaign"].sum()) == 1
    assert campaigns["campaign_key"].is_unique


def test_demand_drivers_are_reproducible_and_capacity_limited() -> None:
    inputs = _public_inputs_fixture()
    price_plan = build_price_plan(inputs["date"], np.random.default_rng(99))
    first = build_demand_drivers(inputs, price_plan, np.random.default_rng(100))
    second = build_demand_drivers(inputs, price_plan, np.random.default_rng(100))

    pd.testing.assert_frame_equal(first, second)
    assert len(first) == 1_096
    assert (first["planned_net_demand"] <= first["available_capacity"]).all()
    assert (
        first["booked_units"] - first["refund_units_target"]
        == first["planned_net_demand"]
    ).all()
    assert first["random_factor"].nunique() > 1_000
    assert _weather_factor(78, 0, False) > _weather_factor(78, 1.5, True)
    historical_prices = price_plan[
        price_plan["date_key"].between(20230101, 20251231)
    ]
    assert np.allclose(
        first["price_multiplier"],
        historical_prices["planned_price_multiplier"],
    )


def test_price_plan_is_reproducible_and_varies_within_calendar_segments() -> None:
    inputs = _public_inputs_fixture()
    first = build_price_plan(inputs["date"], np.random.default_rng(150))
    second = build_price_plan(inputs["date"], np.random.default_rng(150))

    pd.testing.assert_frame_equal(first, second)
    assert len(first) == 1_126
    assert first["planned_price_multiplier"].between(0.85, 1.30).all()
    assert first["planned_price_multiplier"].nunique() > 100

    price_context = first.merge(
        inputs["date"][
            ["date_key", "is_weekend", "season", "holiday_flag"]
        ],
        on="date_key",
        validate="one_to_one",
    )
    within_segment_counts = price_context.groupby(
        ["is_weekend", "season", "holiday_flag"]
    )["planned_price_multiplier"].nunique()
    assert (within_segment_counts > 1).any()


def test_planned_price_changes_generated_demand() -> None:
    inputs = _public_inputs_fixture()
    base_price_plan = build_price_plan(
        inputs["date"], np.random.default_rng(175)
    )
    higher_price_plan = base_price_plan.copy()
    higher_price_plan["planned_price_multiplier"] = (
        higher_price_plan["planned_price_multiplier"] * 1.02
    ).round(4)

    base_demand = build_demand_drivers(
        inputs, base_price_plan, np.random.default_rng(176)
    )
    higher_price_demand = build_demand_drivers(
        inputs, higher_price_plan, np.random.default_rng(176)
    )

    assert (
        higher_price_demand["latent_demand"]
        < base_demand["latent_demand"]
    ).all()


def _integrated_outputs() -> tuple[
    dict[str, pd.DataFrame],
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
]:
    inputs = _public_inputs_fixture()
    price_plan = build_price_plan(inputs["date"], np.random.default_rng(199))
    drivers = build_demand_drivers(
        inputs, price_plan, np.random.default_rng(200)
    )
    sample = drivers[
        drivers["calendar_date"].between("2023-06-15", "2023-06-19")
    ].copy()
    sample["planned_net_demand"] = 120
    sample["booked_units"] = 125
    sample["refund_units_target"] = 5

    products = build_product_dimension()
    channels = build_channel_dimension()
    campaigns = build_campaign_dimension()
    sales = generate_ticket_sales(sample, np.random.default_rng(201))
    campaign_daily = build_campaign_daily(sales, np.random.default_rng(202))
    daily_plan = build_daily_plan(inputs["date"], price_plan)
    return (
        inputs,
        products,
        channels,
        campaigns,
        sales,
        campaign_daily,
        daily_plan,
    )


def test_ticket_sales_and_campaign_facts_reconcile() -> None:
    (
        inputs,
        products,
        channels,
        campaigns,
        sales,
        campaign_daily,
        daily_plan,
    ) = _integrated_outputs()

    by_visit = sales.groupby("visit_date_key").agg(
        sold=("units_sold", "sum"), refunded=("units_refunded", "sum")
    )
    assert (by_visit["sold"] == 125).all()
    assert (by_visit["refunded"] == 5).all()
    assert np.allclose(
        sales["gross_revenue"],
        (sales["units_sold"] * sales["unit_list_price"]).round(2),
    )
    assert np.allclose(
        sales["net_revenue"],
        (
            sales["gross_revenue"]
            - sales["discount_amount"]
            - sales["refund_amount"]
        ).round(2),
    )
    assert int(campaign_daily["conversions"].sum()) > 0
    assert (campaign_daily["conversions"] <= campaign_daily["clicks"]).all()

    results = validate_synthetic_outputs(
        products,
        channels,
        campaigns,
        sales,
        campaign_daily,
        daily_plan,
        inputs["date"],
    )
    assert results["units_sold"] == 625
    assert results["units_refunded"] == 25
    assert results["refund_rate"] == 0.04


def test_validation_rejects_broken_financial_relationship() -> None:
    (
        inputs,
        products,
        channels,
        campaigns,
        sales,
        campaign_daily,
        daily_plan,
    ) = _integrated_outputs()
    sales.loc[sales.index[0], "net_revenue"] += 1

    with pytest.raises(SyntheticDataError, match="Net revenue does not reconcile"):
        validate_synthetic_outputs(
            products,
            channels,
            campaigns,
            sales,
            campaign_daily,
            daily_plan,
            inputs["date"],
        )
