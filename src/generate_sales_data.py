"""Generate reproducible synthetic commercial data for a fictional attraction.

The generator combines real public context prepared by ``download_public_data``
with documented business assumptions. Every ticket sale, price, discount,
refund, campaign result, capacity plan, and revenue target produced here is
synthetic. The data does not represent any real attraction operator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

RANDOM_SEED = 42
HISTORY_START = date(2023, 1, 1)
HISTORY_END = date(2025, 12, 31)
PLAN_END = date(2026, 1, 30)
EARLIEST_PURCHASE_DATE = date(2022, 10, 3)
MAX_REFUND_DATE = date(2026, 1, 30)
BASE_DAILY_DEMAND = 1_200
PRICE_ELASTICITY = -1.10
EXPECTED_REFUND_RATE = 0.04
PRICE_PLAN_ADJUSTMENT_STDDEV = 0.02
PRICE_PLAN_ADJUSTMENT_LIMIT = 0.05

EASTERN_TIME = ZoneInfo("America/New_York")

DAY_OF_WEEK_FACTORS = {
    1: 0.82,
    2: 0.78,
    3: 0.80,
    4: 0.88,
    5: 1.04,
    6: 1.50,
    7: 1.30,
}

SEASON_FACTORS = {
    "off_peak": 0.94,
    "shoulder": 1.00,
    "peak": 1.17,
}

YEAR_FACTORS = {
    2023: 0.97,
    2024: 1.00,
    2025: 1.03,
    2026: 1.04,
}


class SyntheticDataError(ValueError):
    """Raised when generated data fails a business or schema validation rule."""


@dataclass(frozen=True)
class ProductSpec:
    product_key: int
    product_code: str
    product_name: str
    product_type: str
    ticket_tier: str
    base_price: float
    selection_weight: float


@dataclass(frozen=True)
class ChannelSpec:
    channel_key: int
    channel_code: str
    channel_name: str
    channel_type: str


@dataclass(frozen=True)
class CampaignSpec:
    campaign_key: int
    campaign_code: str
    campaign_name: str
    campaign_type: str
    target_segment: str
    start_date: date
    end_date: date
    discount_type: str
    planned_discount_value: float
    is_no_campaign: bool
    visit_demand_lift: float
    attribution_weight: float
    base_daily_spend: float
    cost_per_thousand_impressions: float
    click_through_rate: float


PRODUCT_SPECS = (
    ProductSpec(1, "ADULT_DAY", "Adult Day Pass", "admission", "standard", 119, 0.55),
    ProductSpec(2, "CHILD_DAY", "Child Day Pass", "admission", "standard", 99, 0.25),
    ProductSpec(3, "FLEX_DAY", "Flexible Day Pass", "admission", "premium", 149, 0.12),
    ProductSpec(4, "EVENING", "Evening Pass", "admission", "value", 79, 0.08),
)

CHANNEL_SPECS = (
    ChannelSpec(1, "DIRECT_WEB", "Direct Web", "direct"),
    ChannelSpec(2, "MOBILE_APP", "Mobile App", "direct"),
    ChannelSpec(3, "ONSITE", "Onsite", "direct"),
    ChannelSpec(4, "TRAVEL_PARTNER", "Travel Partner", "partner"),
    ChannelSpec(5, "GROUP_SALES", "Group Sales", "group"),
)


# Purpose: Define the no-campaign member and four annual campaign programs.
# Used by: Module initialization to create CAMPAIGN_SPECS.
def _campaign_specs() -> tuple[CampaignSpec, ...]:
    specs = [
        CampaignSpec(
            campaign_key=1,
            campaign_code="NO_CAMPAIGN",
            campaign_name="No Campaign",
            campaign_type="none",
            target_segment="All guests",
            start_date=EARLIEST_PURCHASE_DATE,
            end_date=PLAN_END,
            discount_type="none",
            planned_discount_value=0,
            is_no_campaign=True,
            visit_demand_lift=0,
            attribution_weight=0,
            base_daily_spend=0,
            cost_per_thousand_impressions=0,
            click_through_rate=0,
        )
    ]
    key = 2
    for year in range(2023, 2026):
        annual_campaigns = (
            (
                "SPRING_SOCIAL",
                "Spring Break Paid Social",
                "paid_media",
                "Family travelers",
                date(year, 1, 15),
                date(year, 3, 31),
                "percent",
                5,
                0.08,
                0.25,
                1_400,
                12,
                0.021,
            ),
            (
                "SUMMER_BUNDLE",
                "Summer Family Bundle",
                "bundle",
                "Families with children",
                date(year, 4, 15),
                date(year, 8, 15),
                "percent",
                10,
                0.10,
                0.30,
                1_100,
                11,
                0.024,
            ),
            (
                "FL_RESIDENT",
                "Florida Resident Email",
                "email",
                "Florida residents",
                date(year, 9, 1),
                date(year, 10, 31),
                "fixed",
                10,
                0.05,
                0.22,
                500,
                9,
                0.030,
            ),
            (
                "HOLIDAY_EARLY",
                "Holiday Early Booking",
                "discount",
                "Holiday travelers",
                date(year, 10, 15),
                date(year, 12, 15),
                "percent",
                8,
                0.09,
                0.28,
                900,
                13,
                0.020,
            ),
        )
        for campaign in annual_campaigns:
            (
                code,
                name,
                campaign_type,
                segment,
                start,
                end,
                discount_type,
                discount_value,
                demand_lift,
                attribution_weight,
                daily_spend,
                cpm,
                ctr,
            ) = campaign
            specs.append(
                CampaignSpec(
                    campaign_key=key,
                    campaign_code=f"{code}_{year}",
                    campaign_name=f"{name} {year}",
                    campaign_type=campaign_type,
                    target_segment=segment,
                    start_date=start,
                    end_date=end,
                    discount_type=discount_type,
                    planned_discount_value=discount_value,
                    is_no_campaign=False,
                    visit_demand_lift=demand_lift,
                    attribution_weight=attribution_weight,
                    base_daily_spend=daily_spend,
                    cost_per_thousand_impressions=cpm,
                    click_through_rate=ctr,
                )
            )
            key += 1
    return tuple(specs)


CAMPAIGN_SPECS = _campaign_specs()


# Purpose: Convert an ISO date string to a date while accepting existing dates.
# Used by: generate_ticket_sales and validate_synthetic_outputs.
def parse_iso_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


# Purpose: Convert an integer YYYYMMDD key back into a calendar date.
# Used by: _apply_daily_refunds and validate_synthetic_outputs.
def _date_from_key(value: int | str) -> date:
    text = str(value)
    return date.fromisoformat(f"{text[:4]}-{text[4:6]}-{text[6:8]}")


# Purpose: Calculate a reproducibility checksum for an input or output file.
# Used by: _output_summary and run_generation.
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# Purpose: Convert product assumptions into the dim_product load shape.
# Used by: run_generation and the synthetic-data unit tests.
def build_product_dimension() -> pd.DataFrame:
    records = []
    for spec in PRODUCT_SPECS:
        record = asdict(spec)
        record.pop("selection_weight")
        record.update(
            {
                "valid_from": HISTORY_START.isoformat(),
                "valid_to": None,
                "is_active": True,
            }
        )
        records.append(record)
    return pd.DataFrame.from_records(records)


# Purpose: Convert channel assumptions into the dim_channel load shape.
# Used by: run_generation and the synthetic-data unit tests.
def build_channel_dimension() -> pd.DataFrame:
    records = []
    for spec in CHANNEL_SPECS:
        record = asdict(spec)
        record["is_active"] = True
        records.append(record)
    return pd.DataFrame.from_records(records)


# Purpose: Convert campaign assumptions into the dim_campaign load shape.
# Used by: run_generation and the synthetic-data unit tests.
def build_campaign_dimension() -> pd.DataFrame:
    database_fields = {
        "campaign_key",
        "campaign_code",
        "campaign_name",
        "campaign_type",
        "target_segment",
        "start_date",
        "end_date",
        "discount_type",
        "planned_discount_value",
        "is_no_campaign",
    }
    records = []
    for spec in CAMPAIGN_SPECS:
        record = asdict(spec)
        records.append(
            {
                field: (
                    record[field].isoformat()
                    if isinstance(record[field], date)
                    else record[field]
                )
                for field in database_fields
            }
        )
    columns = [
        "campaign_key",
        "campaign_code",
        "campaign_name",
        "campaign_type",
        "target_segment",
        "start_date",
        "end_date",
        "discount_type",
        "planned_discount_value",
        "is_no_campaign",
    ]
    return pd.DataFrame.from_records(records)[columns].sort_values("campaign_key")


# Purpose: Load the prepared public calendar, weather, MCO, and TDT inputs.
# Used by: run_generation.
def load_public_inputs(project_root: Path) -> dict[str, pd.DataFrame]:
    source_dir = project_root / "data" / "processed" / "public"
    paths = {
        "date": source_dir / "dim_date.csv",
        "weather": source_dir / "fact_weather.csv",
        "mco": source_dir / "mco_enplaned_passengers_monthly.csv",
        "tdt": source_dir / "orange_county_tdt_monthly.csv",
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Public inputs are missing. Run src.download_public_data first: "
            + ", ".join(missing)
        )
    return {name: pd.read_csv(path) for name, path in paths.items()}


# Purpose: Calculate the calendar-based starting point for a planned price.
# Used by: build_price_plan.
def _calendar_price_multiplier(row: Any) -> float:
    multiplier = 1.0
    if bool(row.is_weekend):
        multiplier += 0.06
    if row.season == "peak":
        multiplier += 0.08
    elif row.season == "off_peak":
        multiplier -= 0.05
    if bool(row.holiday_flag):
        multiplier += 0.04
    return round(multiplier, 4)


# Purpose: Create reproducible observed price variation around calendar pricing.
# Used by: run_generation and the synthetic-data unit tests.
def build_price_plan(
    date_dimension: pd.DataFrame, rng: np.random.Generator
) -> pd.DataFrame:
    dates = date_dimension.copy()
    dates["calendar_date"] = pd.to_datetime(dates["calendar_date"])
    dates = dates[
        dates["calendar_date"].between(
            pd.Timestamp(HISTORY_START), pd.Timestamp(PLAN_END)
        )
    ].sort_values("date_key")

    calendar_multipliers = np.array(
        [
            _calendar_price_multiplier(row)
            for row in dates.itertuples(index=False)
        ],
        dtype=float,
    )
    planned_adjustments = np.clip(
        rng.normal(0, PRICE_PLAN_ADJUSTMENT_STDDEV, len(dates)),
        -PRICE_PLAN_ADJUSTMENT_LIMIT,
        PRICE_PLAN_ADJUSTMENT_LIMIT,
    )
    dates["planned_price_multiplier"] = np.round(
        calendar_multipliers * (1 + planned_adjustments),
        4,
    )
    return dates[["date_key", "planned_price_multiplier"]].reset_index(drop=True)


# Purpose: Convert temperature, rain, and severe weather into a demand factor.
# Used by: build_demand_drivers.
def _weather_factor(
    average_temperature: float, precipitation: float, severe: bool
) -> float:
    temperature_penalty = 0.0
    if average_temperature < 65:
        temperature_penalty = min(0.12, (65 - average_temperature) * 0.004)
    elif average_temperature > 85:
        temperature_penalty = min(0.12, (average_temperature - 85) * 0.004)
    rain_penalty = min(0.18, precipitation * 0.08)
    severe_multiplier = 0.90 if severe else 1.0
    return round((1 - temperature_penalty) * (1 - rain_penalty) * severe_multiplier, 4)


# Purpose: Assign daily operating capacity from season and weekend conditions.
# Used by: build_demand_drivers and build_daily_plan.
def _available_capacity(row: Any) -> int:
    capacity = 2_500
    if row.season == "off_peak" and not bool(row.is_weekend):
        capacity = 2_350
    return capacity


# Purpose: Combine campaign windows into a capped visit-date demand factor.
# Used by: build_demand_drivers.
def _campaign_factor(
    visit_date: date, campaign_specs: tuple[CampaignSpec, ...]
) -> float:
    lift = sum(
        spec.visit_demand_lift
        for spec in campaign_specs
        if not spec.is_no_campaign and spec.start_date <= visit_date <= spec.end_date
    )
    return round(1 + min(lift, 0.15), 4)


# Purpose: Join and index the monthly MCO and TDT tourism benchmarks.
# Used by: build_demand_drivers.
def _prepare_market_benchmarks(mco: pd.DataFrame, tdt: pd.DataFrame) -> pd.DataFrame:
    mco_monthly = mco[["month_start", "enplaned_passengers"]].copy()
    tdt_monthly = tdt[["month_start", "remittance_usd"]].copy()
    market = mco_monthly.merge(tdt_monthly, on="month_start", validate="one_to_one")
    if len(market) != 36:
        raise SyntheticDataError("Expected 36 complete monthly market benchmark rows")
    market["mco_index"] = (
        market["enplaned_passengers"] / market["enplaned_passengers"].mean()
    )
    market["tdt_index"] = market["remittance_usd"] / market["remittance_usd"].mean()
    market["market_factor"] = (
        1 + 0.25 * (market["mco_index"] - 1) + 0.15 * (market["tdt_index"] - 1)
    ).clip(lower=0.85, upper=1.15)
    return market


# Purpose: Calculate every transparent factor used to generate daily demand.
# Used by: run_generation and the synthetic-data unit tests.
def build_demand_drivers(
    public_inputs: dict[str, pd.DataFrame],
    price_plan: pd.DataFrame,
    rng: np.random.Generator,
    campaign_specs: tuple[CampaignSpec, ...] = CAMPAIGN_SPECS,
) -> pd.DataFrame:
    dates = public_inputs["date"].copy()
    dates["calendar_date"] = pd.to_datetime(dates["calendar_date"])
    dates = dates[
        dates["calendar_date"].between(
            pd.Timestamp(HISTORY_START), pd.Timestamp(HISTORY_END)
        )
    ].copy()
    weather = public_inputs["weather"].copy()
    daily = dates.merge(weather, on="date_key", how="left", validate="one_to_one")
    if daily["avg_temperature_f"].isna().any():
        raise SyntheticDataError("Weather is missing for at least one historical date")
    daily = daily.merge(
        price_plan[["date_key", "planned_price_multiplier"]],
        on="date_key",
        how="left",
        validate="one_to_one",
    )
    if daily["planned_price_multiplier"].isna().any():
        raise SyntheticDataError("Price plan is missing for at least one historical date")

    daily["month_start"] = daily["calendar_date"].dt.to_period("M").dt.start_time
    market = _prepare_market_benchmarks(public_inputs["mco"], public_inputs["tdt"])
    market["month_start"] = pd.to_datetime(market["month_start"])
    daily = daily.merge(
        market[["month_start", "mco_index", "tdt_index", "market_factor"]],
        on="month_start",
        how="left",
        validate="many_to_one",
    )
    if daily["market_factor"].isna().any():
        raise SyntheticDataError("Market benchmark is missing for at least one month")

    daily["dow_factor"] = daily["day_of_week"].map(DAY_OF_WEEK_FACTORS)
    daily["season_factor"] = daily["season"].map(SEASON_FACTORS)
    daily["holiday_factor"] = np.where(daily["holiday_flag"], 1.12, 1.0)
    daily["school_break_factor"] = np.where(daily["school_break_flag"], 1.20, 1.0)
    daily["price_multiplier"] = daily["planned_price_multiplier"].astype(float)
    daily["price_factor"] = daily["price_multiplier"] ** PRICE_ELASTICITY
    daily["weather_factor"] = [
        _weather_factor(
            row.avg_temperature_f, row.precipitation_in, row.severe_weather_flag
        )
        for row in daily.itertuples(index=False)
    ]
    daily["campaign_factor"] = [
        _campaign_factor(row.calendar_date.date(), campaign_specs)
        for row in daily.itertuples(index=False)
    ]
    daily["year_factor"] = daily["year_number"].map(YEAR_FACTORS)
    noise_sigma = 0.075
    daily["random_factor"] = rng.lognormal(
        mean=-(noise_sigma**2) / 2, sigma=noise_sigma, size=len(daily)
    )
    factor_columns = [
        "dow_factor",
        "season_factor",
        "holiday_factor",
        "school_break_factor",
        "weather_factor",
        "market_factor",
        "price_factor",
        "campaign_factor",
        "year_factor",
        "random_factor",
    ]
    daily["latent_demand"] = BASE_DAILY_DEMAND * daily[factor_columns].prod(axis=1)
    daily["available_capacity"] = [
        _available_capacity(row) for row in daily.itertuples(index=False)
    ]
    daily["planned_net_demand"] = np.minimum(
        daily["latent_demand"].round().astype(int), daily["available_capacity"]
    )
    daily["booked_units"] = np.ceil(
        daily["planned_net_demand"] / (1 - EXPECTED_REFUND_RATE)
    ).astype(int)
    daily["refund_units_target"] = daily["booked_units"] - daily["planned_net_demand"]

    output_columns = [
        "date_key",
        "calendar_date",
        "day_of_week",
        "is_weekend",
        "holiday_flag",
        "school_break_flag",
        "season",
        "baseline_demand",
        *factor_columns,
        "mco_index",
        "tdt_index",
        "price_multiplier",
        "latent_demand",
        "available_capacity",
        "planned_net_demand",
        "booked_units",
        "refund_units_target",
    ]
    daily["baseline_demand"] = BASE_DAILY_DEMAND
    daily["calendar_date"] = daily["calendar_date"].dt.strftime("%Y-%m-%d")
    return daily[output_columns].sort_values("date_key").reset_index(drop=True)


# Purpose: Model the gradual shift from web and onsite sales to the mobile app.
# Used by: generate_ticket_sales.
def _channel_probabilities(year: int) -> np.ndarray:
    base = np.array([0.45, 0.25, 0.10, 0.15, 0.05], dtype=float)
    years_after_2023 = year - 2023
    base[0] -= 0.01 * years_after_2023
    base[1] += 0.025 * years_after_2023
    base[2] -= 0.01 * years_after_2023
    base[3] -= 0.005 * years_after_2023
    return base / base.sum()


# Purpose: Create a context-sensitive product mix while preserving all products.
# Used by: generate_ticket_sales and build_daily_plan.
def _product_probabilities(
    day_of_week: int,
    holiday_flag: bool,
    school_break_flag: bool,
    season: str,
    channel_key: int | None = None,
    campaign_type: str = "none",
    product_specs: tuple[ProductSpec, ...] = PRODUCT_SPECS,
) -> np.ndarray:
    weights = np.array(
        [product.selection_weight for product in product_specs], dtype=float
    )
    context_factors = {
        product.product_code: 1.0 for product in product_specs
    }

    if school_break_flag:
        context_factors["ADULT_DAY"] *= 0.90
        context_factors["CHILD_DAY"] *= 1.45
        context_factors["FLEX_DAY"] *= 1.02
        context_factors["EVENING"] *= 0.72

    if day_of_week >= 6:
        context_factors["ADULT_DAY"] *= 0.96
        context_factors["CHILD_DAY"] *= 1.04
        context_factors["FLEX_DAY"] *= 1.28
        context_factors["EVENING"] *= 0.72
    elif day_of_week <= 4:
        context_factors["EVENING"] *= 1.25

    if holiday_flag:
        context_factors["ADULT_DAY"] *= 0.94
        context_factors["CHILD_DAY"] *= 1.12
        context_factors["FLEX_DAY"] *= 1.32
        context_factors["EVENING"] *= 0.72

    if season == "peak":
        context_factors["CHILD_DAY"] *= 1.08
        context_factors["FLEX_DAY"] *= 1.10
    elif season == "off_peak":
        context_factors["CHILD_DAY"] *= 0.90
        context_factors["FLEX_DAY"] *= 0.92
        context_factors["EVENING"] *= 1.30

    if channel_key == 2:
        context_factors["FLEX_DAY"] *= 1.18
    elif channel_key == 3:
        context_factors["ADULT_DAY"] *= 0.94
        context_factors["CHILD_DAY"] *= 0.90
        context_factors["FLEX_DAY"] *= 0.90
        context_factors["EVENING"] *= 1.75
    elif channel_key == 4:
        context_factors["FLEX_DAY"] *= 1.30
        context_factors["EVENING"] *= 0.85
    elif channel_key == 5:
        context_factors["ADULT_DAY"] *= 1.08
        context_factors["CHILD_DAY"] *= 1.28
        context_factors["FLEX_DAY"] *= 0.75
        context_factors["EVENING"] *= 0.65

    if campaign_type == "paid_media":
        context_factors["CHILD_DAY"] *= 1.10
        context_factors["FLEX_DAY"] *= 1.20
    elif campaign_type == "bundle":
        context_factors["ADULT_DAY"] *= 1.08
        context_factors["CHILD_DAY"] *= 1.50
        context_factors["EVENING"] *= 0.65
    elif campaign_type == "email":
        context_factors["ADULT_DAY"] *= 1.08
        context_factors["EVENING"] *= 1.25
    elif campaign_type == "partnership":
        context_factors["ADULT_DAY"] *= 1.10
        context_factors["CHILD_DAY"] *= 1.15
    elif campaign_type == "discount":
        context_factors["CHILD_DAY"] *= 1.10
        context_factors["EVENING"] *= 1.20

    factors = np.array(
        [context_factors[product.product_code] for product in product_specs],
        dtype=float,
    )
    adjusted = weights * factors
    if (adjusted <= 0).any() or not np.isfinite(adjusted).all():
        raise SyntheticDataError("Product probabilities are invalid")
    return adjusted / adjusted.sum()


# Purpose: Sample a 1-to-6 ticket order without exceeding remaining daily units.
# Used by: generate_ticket_sales.
def _order_size(
    channel_key: int, remaining_units: int, rng: np.random.Generator
) -> int:
    if channel_key == 5:
        sampled = int(rng.choice([3, 4, 5, 6], p=[0.10, 0.25, 0.35, 0.30]))
    else:
        sampled = int(
            rng.choice([1, 2, 3, 4, 5, 6], p=[0.10, 0.25, 0.30, 0.20, 0.10, 0.05])
        )
    return min(sampled, remaining_units)


# Purpose: Sample booking lead time according to the selected sales channel.
# Used by: generate_ticket_sales.
def _lead_days(channel_key: int, rng: np.random.Generator) -> int:
    if channel_key == 1:
        return min(90, int(rng.gamma(2.2, 11)))
    if channel_key == 2:
        return min(75, int(rng.gamma(1.7, 8)))
    if channel_key == 3:
        return int(rng.integers(0, 3))
    if channel_key == 4:
        return min(90, 3 + int(rng.gamma(2.5, 13)))
    return min(90, 14 + int(rng.gamma(2.7, 14)))


# Purpose: Create a timezone-aware synthetic booking timestamp.
# Used by: generate_ticket_sales.
def _booked_timestamp(purchase_date: date, rng: np.random.Generator) -> str:
    second_of_day = int(rng.integers(7 * 3600, 23 * 3600))
    booked_time = time(
        hour=second_of_day // 3600,
        minute=(second_of_day % 3600) // 60,
        second=second_of_day % 60,
    )
    return datetime.combine(purchase_date, booked_time, tzinfo=EASTERN_TIME).isoformat()


# Purpose: Attribute an eligible purchase to one active campaign or no campaign.
# Used by: generate_ticket_sales.
def _select_campaign(
    purchase_date: date,
    campaign_specs: tuple[CampaignSpec, ...],
    rng: np.random.Generator,
) -> CampaignSpec:
    eligible = [
        spec
        for spec in campaign_specs
        if not spec.is_no_campaign and spec.start_date <= purchase_date <= spec.end_date
    ]
    if not eligible:
        return campaign_specs[0]
    attribution_probability = min(
        0.55, sum(spec.attribution_weight for spec in eligible)
    )
    if rng.random() >= attribution_probability:
        return campaign_specs[0]
    weights = np.array([spec.attribution_weight for spec in eligible], dtype=float)
    weights /= weights.sum()
    return eligible[int(rng.choice(len(eligible), p=weights))]


# Purpose: Calculate the campaign discount while preventing excess discounting.
# Used by: generate_ticket_sales.
def _discount_amount(
    gross_revenue: float, units_sold: int, campaign: CampaignSpec
) -> float:
    if campaign.discount_type == "percent":
        return round(gross_revenue * campaign.planned_discount_value / 100, 2)
    if campaign.discount_type == "fixed":
        return min(
            gross_revenue,
            round(units_sold * campaign.planned_discount_value, 2),
        )
    return 0.0


# Purpose: Allocate the exact daily refund-unit target and reconcile revenue.
# Used by: generate_ticket_sales.
def _apply_daily_refunds(
    records: list[dict[str, Any]],
    refund_units_target: int,
    visit_date: date,
    rng: np.random.Generator,
) -> None:
    remaining = refund_units_target
    candidate_indices = rng.permutation(len(records))
    for index in candidate_indices:
        if remaining == 0:
            break
        record = records[int(index)]
        units_sold = int(record["units_sold"])
        maximum = min(units_sold, remaining)
        if maximum > 1 and rng.random() < 0.55:
            units_refunded = int(rng.integers(1, maximum + 1))
        else:
            units_refunded = maximum

        discounted_revenue = round(
            float(record["gross_revenue"]) - float(record["discount_amount"]), 2
        )
        if units_refunded == units_sold:
            refund_amount = discounted_revenue
            sale_status = "cancelled" if rng.random() < 0.25 else "refunded"
        else:
            refund_amount = round(discounted_revenue * units_refunded / units_sold, 2)
            sale_status = "partially_refunded"

        purchase_date = _date_from_key(record["purchase_date_key"])
        latest_refund = min(visit_date + timedelta(days=14), MAX_REFUND_DATE)
        refund_window = max(0, (latest_refund - purchase_date).days)
        refund_date = purchase_date + timedelta(
            days=int(rng.integers(0, refund_window + 1))
        )

        record["units_refunded"] = units_refunded
        record["refund_date_key"] = int(refund_date.strftime("%Y%m%d"))
        record["refund_amount"] = refund_amount
        record["net_revenue"] = round(discounted_revenue - refund_amount, 2)
        record["sale_status"] = sale_status
        remaining -= units_refunded

    if remaining != 0:
        raise SyntheticDataError(
            f"Could not allocate {refund_units_target} refund units on {visit_date}"
        )


# Purpose: Expand daily booked units into synthetic order-line transactions.
# Used by: run_generation and the synthetic-data unit tests.
def generate_ticket_sales(
    demand_drivers: pd.DataFrame,
    rng: np.random.Generator,
    product_specs: tuple[ProductSpec, ...] = PRODUCT_SPECS,
    channel_specs: tuple[ChannelSpec, ...] = CHANNEL_SPECS,
    campaign_specs: tuple[CampaignSpec, ...] = CAMPAIGN_SPECS,
) -> pd.DataFrame:
    all_records: list[dict[str, Any]] = []
    order_sequence = 1

    for driver in demand_drivers.itertuples(index=False):
        visit_date = parse_iso_date(driver.calendar_date)
        remaining_units = int(driver.booked_units)
        daily_records: list[dict[str, Any]] = []
        channel_weights = _channel_probabilities(visit_date.year)

        while remaining_units > 0:
            channel = channel_specs[
                int(rng.choice(len(channel_specs), p=channel_weights))
            ]
            units_sold = _order_size(channel.channel_key, remaining_units, rng)
            lead_days = _lead_days(channel.channel_key, rng)
            purchase_date = max(
                EARLIEST_PURCHASE_DATE, visit_date - timedelta(days=lead_days)
            )
            campaign = _select_campaign(purchase_date, campaign_specs, rng)
            product_weights = _product_probabilities(
                day_of_week=int(driver.day_of_week),
                holiday_flag=bool(driver.holiday_flag),
                school_break_flag=bool(driver.school_break_flag),
                season=str(driver.season),
                channel_key=channel.channel_key,
                campaign_type=campaign.campaign_type,
                product_specs=product_specs,
            )
            product = product_specs[
                int(rng.choice(len(product_specs), p=product_weights))
            ]
            unit_list_price = round(product.base_price * driver.price_multiplier, 2)
            gross_revenue = round(units_sold * unit_list_price, 2)
            discount_amount = _discount_amount(gross_revenue, units_sold, campaign)
            net_revenue = round(gross_revenue - discount_amount, 2)

            daily_records.append(
                {
                    "order_id": f"ORD{order_sequence:010d}",
                    "order_line_number": 1,
                    "purchase_date_key": int(purchase_date.strftime("%Y%m%d")),
                    "visit_date_key": int(visit_date.strftime("%Y%m%d")),
                    "refund_date_key": None,
                    "product_key": product.product_key,
                    "channel_key": channel.channel_key,
                    "campaign_key": campaign.campaign_key,
                    "units_sold": units_sold,
                    "units_refunded": 0,
                    "unit_list_price": unit_list_price,
                    "gross_revenue": gross_revenue,
                    "discount_amount": discount_amount,
                    "refund_amount": 0.0,
                    "net_revenue": net_revenue,
                    "sale_status": "active",
                    "currency_code": "USD",
                    "booked_at": _booked_timestamp(purchase_date, rng),
                }
            )
            remaining_units -= units_sold
            order_sequence += 1

        _apply_daily_refunds(
            daily_records,
            int(driver.refund_units_target),
            visit_date,
            rng,
        )
        all_records.extend(daily_records)

    sales = pd.DataFrame.from_records(all_records)
    sales["refund_date_key"] = pd.array(sales["refund_date_key"], dtype="Int64")
    return sales


# Purpose: Build daily spend, funnel, and attributed-revenue campaign facts.
# Used by: run_generation and the synthetic-data unit tests.
def build_campaign_daily(
    ticket_sales: pd.DataFrame,
    rng: np.random.Generator,
    campaign_specs: tuple[CampaignSpec, ...] = CAMPAIGN_SPECS,
) -> pd.DataFrame:
    attributed = (
        ticket_sales[ticket_sales["campaign_key"] != 1]
        .groupby(["purchase_date_key", "campaign_key"], as_index=False)
        .agg(
            conversions=("order_id", "nunique"),
            attributed_revenue=("net_revenue", "sum"),
        )
        .rename(columns={"purchase_date_key": "date_key"})
    )
    attributed_lookup = {
        (int(row.date_key), int(row.campaign_key)): (
            int(row.conversions),
            round(float(row.attributed_revenue), 2),
        )
        for row in attributed.itertuples(index=False)
    }
    records: list[dict[str, Any]] = []

    for campaign in campaign_specs:
        if campaign.is_no_campaign:
            continue
        current = campaign.start_date
        while current <= campaign.end_date:
            date_key = int(current.strftime("%Y%m%d"))
            conversions, revenue = attributed_lookup.get(
                (date_key, campaign.campaign_key), (0, 0.0)
            )
            weekend_factor = 1.12 if current.isoweekday() >= 6 else 1.0
            spend = round(
                campaign.base_daily_spend * weekend_factor * rng.lognormal(0, 0.08),
                2,
            )
            impressions = max(
                round(spend / campaign.cost_per_thousand_impressions * 1_000),
                conversions,
            )
            clicks = max(round(impressions * campaign.click_through_rate), conversions)
            impressions = max(impressions, clicks)
            records.append(
                {
                    "date_key": date_key,
                    "campaign_key": campaign.campaign_key,
                    "spend": spend,
                    "impressions": impressions,
                    "clicks": clicks,
                    "conversions": conversions,
                    "attributed_revenue": revenue,
                }
            )
            current += timedelta(days=1)
    return (
        pd.DataFrame.from_records(records)
        .sort_values(["date_key", "campaign_key"])
        .reset_index(drop=True)
    )


# Purpose: Create historical and future price, capacity, target, and staffing plans.
# Used by: run_generation and the synthetic-data unit tests.
def build_daily_plan(
    date_dimension: pd.DataFrame, price_plan: pd.DataFrame
) -> pd.DataFrame:
    dates = date_dimension.copy()
    dates["calendar_date"] = pd.to_datetime(dates["calendar_date"])
    dates = dates[
        dates["calendar_date"].between(
            pd.Timestamp(HISTORY_START), pd.Timestamp(PLAN_END)
        )
    ].copy()
    dates = dates.merge(
        price_plan[["date_key", "planned_price_multiplier"]],
        on="date_key",
        how="left",
        validate="one_to_one",
    )
    if dates["planned_price_multiplier"].isna().any():
        raise SyntheticDataError("Price plan is missing for at least one plan date")
    records = []
    for row in dates.itertuples(index=False):
        capacity = _available_capacity(row)
        price_multiplier = float(row.planned_price_multiplier)
        planned_product_mix = _product_probabilities(
            day_of_week=int(row.day_of_week),
            holiday_flag=bool(row.holiday_flag),
            school_break_flag=bool(row.school_break_flag),
            season=str(row.season),
        )
        weighted_base_price = float(
            np.dot(
                planned_product_mix,
                np.array(
                    [product.base_price for product in PRODUCT_SPECS],
                    dtype=float,
                ),
            )
        )
        demand_target = round(
            BASE_DAILY_DEMAND
            * DAY_OF_WEEK_FACTORS[row.day_of_week]
            * SEASON_FACTORS[row.season]
            * (1.12 if row.holiday_flag else 1.0)
            * (1.20 if row.school_break_flag else 1.0)
            * YEAR_FACTORS[row.year_number]
            * price_multiplier**PRICE_ELASTICITY
        )
        demand_target = min(capacity, demand_target)
        revenue_target = round(
            demand_target * weighted_base_price * price_multiplier * 0.93, 2
        )
        planned_staff_hours = round(max(240, demand_target * 0.22), 2)
        records.append(
            {
                "date_key": int(row.date_key),
                "planned_price_multiplier": price_multiplier,
                "available_capacity": capacity,
                "demand_target": demand_target,
                "revenue_target": revenue_target,
                "planned_staff_hours": planned_staff_hours,
                "plan_version": "initial_plan_v2_product_mix",
            }
        )
    return pd.DataFrame.from_records(records)


# Purpose: Enforce keys, dates, finances, refunds, capacity, and campaign rules.
# Used by: run_generation and the synthetic-data unit tests.
def validate_synthetic_outputs(
    product: pd.DataFrame,
    channel: pd.DataFrame,
    campaign: pd.DataFrame,
    ticket_sales: pd.DataFrame,
    campaign_daily: pd.DataFrame,
    daily_plan: pd.DataFrame,
    date_dimension: pd.DataFrame,
) -> dict[str, float | int]:
    if product["product_key"].duplicated().any():
        raise SyntheticDataError("Duplicate product keys")
    if channel["channel_key"].duplicated().any():
        raise SyntheticDataError("Duplicate channel keys")
    if campaign["campaign_key"].duplicated().any():
        raise SyntheticDataError("Duplicate campaign keys")
    if int(campaign["is_no_campaign"].sum()) != 1:
        raise SyntheticDataError("Exactly one no-campaign member is required")
    if ticket_sales.duplicated(["order_id", "order_line_number"]).any():
        raise SyntheticDataError("Duplicate order lines")

    key_checks = (
        ("product_key", set(product["product_key"])),
        ("channel_key", set(channel["channel_key"])),
        ("campaign_key", set(campaign["campaign_key"])),
    )
    for column, valid_keys in key_checks:
        if not set(ticket_sales[column]).issubset(valid_keys):
            raise SyntheticDataError(f"Ticket sales contain an unknown {column}")

    valid_date_keys = set(date_dimension["date_key"].astype(int))
    required_date_columns = ("purchase_date_key", "visit_date_key")
    for column in required_date_columns:
        if not set(ticket_sales[column].astype(int)).issubset(valid_date_keys):
            raise SyntheticDataError(f"Ticket sales contain an unknown {column}")
    refund_date_keys = set(
        ticket_sales.loc[ticket_sales["refund_date_key"].notna(), "refund_date_key"]
        .astype(int)
        .tolist()
    )
    if not refund_date_keys.issubset(valid_date_keys):
        raise SyntheticDataError("Ticket sales contain an unknown refund_date_key")
    if not set(campaign_daily["date_key"].astype(int)).issubset(valid_date_keys):
        raise SyntheticDataError("Campaign facts contain an unknown date_key")
    if not set(daily_plan["date_key"].astype(int)).issubset(valid_date_keys):
        raise SyntheticDataError("Daily plans contain an unknown date_key")
    if not daily_plan["planned_price_multiplier"].between(0.85, 1.30).all():
        raise SyntheticDataError("Planned price multiplier is outside expected bounds")

    plan_context = daily_plan.merge(
        date_dimension[
            ["date_key", "is_weekend", "season", "holiday_flag"]
        ],
        on="date_key",
        how="left",
        validate="one_to_one",
    )
    calendar_prices = np.array(
        [
            _calendar_price_multiplier(row)
            for row in plan_context.itertuples(index=False)
        ]
    )
    planned_prices = plan_context["planned_price_multiplier"].to_numpy(dtype=float)
    if np.allclose(planned_prices, calendar_prices, atol=0.0001):
        raise SyntheticDataError(
            "Planned prices contain no variation beyond calendar pricing"
        )
    relative_adjustments = planned_prices / calendar_prices - 1
    if np.abs(relative_adjustments).max() > PRICE_PLAN_ADJUSTMENT_LIMIT + 0.0002:
        raise SyntheticDataError("Planned price adjustment exceeds its documented limit")

    if (ticket_sales["purchase_date_key"] > ticket_sales["visit_date_key"]).any():
        raise SyntheticDataError("A visit date occurs before its purchase date")
    refund_rows = ticket_sales["refund_date_key"].notna()
    if (
        ticket_sales.loc[refund_rows, "refund_date_key"].astype(int)
        < ticket_sales.loc[refund_rows, "purchase_date_key"]
    ).any():
        raise SyntheticDataError("A refund date occurs before its purchase date")

    expected_gross = (
        ticket_sales["units_sold"] * ticket_sales["unit_list_price"]
    ).round(2)
    expected_net = (
        ticket_sales["gross_revenue"]
        - ticket_sales["discount_amount"]
        - ticket_sales["refund_amount"]
    ).round(2)
    if not np.allclose(ticket_sales["gross_revenue"], expected_gross, atol=0.001):
        raise SyntheticDataError("Gross revenue does not reconcile")
    if not np.allclose(ticket_sales["net_revenue"], expected_net, atol=0.001):
        raise SyntheticDataError("Net revenue does not reconcile")
    if (ticket_sales["net_revenue"] < 0).any():
        raise SyntheticDataError("Negative net revenue")
    if (ticket_sales["units_refunded"] > ticket_sales["units_sold"]).any():
        raise SyntheticDataError("Refunded units exceed sold units")

    product_base_prices = product.set_index("product_key")["base_price"]
    visit_price_multipliers = daily_plan.set_index("date_key")[
        "planned_price_multiplier"
    ]
    unrounded_unit_prices = (
        ticket_sales["product_key"].map(product_base_prices)
        * ticket_sales["visit_date_key"].map(visit_price_multipliers)
    )
    expected_unit_prices = np.fromiter(
        (round(float(value), 2) for value in unrounded_unit_prices),
        dtype=float,
        count=len(unrounded_unit_prices),
    )
    if not np.allclose(
        ticket_sales["unit_list_price"], expected_unit_prices, atol=0.001
    ):
        raise SyntheticDataError("Ticket prices do not match the daily price plan")

    active = ticket_sales["sale_status"] == "active"
    if (
        (ticket_sales.loc[active, "units_refunded"] != 0).any()
        or (ticket_sales.loc[active, "refund_amount"] != 0).any()
        or ticket_sales.loc[active, "refund_date_key"].notna().any()
    ):
        raise SyntheticDataError("Active sales contain refund activity")
    closed = ticket_sales["sale_status"].isin(["refunded", "cancelled"])
    if (
        ticket_sales.loc[closed, "units_refunded"]
        != ticket_sales.loc[closed, "units_sold"]
    ).any():
        raise SyntheticDataError("Closed sales are not fully refunded")

    net_units_by_date = (
        ticket_sales.assign(
            net_units=ticket_sales["units_sold"] - ticket_sales["units_refunded"]
        )
        .groupby("visit_date_key", as_index=False)["net_units"]
        .sum()
    )
    capacity_check = net_units_by_date.merge(
        daily_plan[["date_key", "available_capacity"]],
        left_on="visit_date_key",
        right_on="date_key",
        validate="one_to_one",
    )
    if (capacity_check["net_units"] > capacity_check["available_capacity"]).any():
        raise SyntheticDataError("Net ticket demand exceeds capacity")
    if (daily_plan["demand_target"] > daily_plan["available_capacity"]).any():
        raise SyntheticDataError("A plan target exceeds capacity")
    if campaign_daily.duplicated(["date_key", "campaign_key"]).any():
        raise SyntheticDataError("Duplicate campaign daily rows")
    if (campaign_daily["conversions"] > campaign_daily["clicks"]).any() or (
        campaign_daily["clicks"] > campaign_daily["impressions"]
    ).any():
        raise SyntheticDataError("Campaign funnel metrics are inconsistent")

    campaign_windows = campaign.set_index("campaign_key")[["start_date", "end_date"]]
    attributed_sales = ticket_sales[ticket_sales["campaign_key"] != 1]
    for row in attributed_sales[["purchase_date_key", "campaign_key"]].itertuples(
        index=False
    ):
        purchase_date = _date_from_key(row.purchase_date_key)
        window = campaign_windows.loc[row.campaign_key]
        if not (
            parse_iso_date(window.start_date)
            <= purchase_date
            <= parse_iso_date(window.end_date)
        ):
            raise SyntheticDataError("A sale is outside its campaign window")

    total_units = int(ticket_sales["units_sold"].sum())
    refunded_units = int(ticket_sales["units_refunded"].sum())
    refund_rate = refunded_units / total_units
    if not 0.035 <= refund_rate <= 0.045:
        raise SyntheticDataError(f"Unexpected refund rate: {refund_rate:.3%}")
    return {
        "order_lines": len(ticket_sales),
        "units_sold": total_units,
        "units_refunded": refunded_units,
        "refund_rate": round(refund_rate, 6),
        "gross_revenue": round(float(ticket_sales["gross_revenue"].sum()), 2),
        "discounts": round(float(ticket_sales["discount_amount"].sum()), 2),
        "refunds": round(float(ticket_sales["refund_amount"].sum()), 2),
        "net_revenue": round(float(ticket_sales["net_revenue"].sum()), 2),
        "capacity_constrained_days": int(
            (capacity_check["net_units"] >= capacity_check["available_capacity"]).sum()
        ),
    }


# Purpose: Write one generated DataFrame without leaving a partial final CSV.
# Used by: run_generation for all synthetic tabular outputs.
def write_csv_atomic(frame: pd.DataFrame, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.with_suffix(destination.suffix + ".part")
    frame.to_csv(temporary_path, index=False, float_format="%.6f")
    temporary_path.replace(destination)


# Purpose: Record an output's portable path, row count, and checksum.
# Used by: run_generation when it builds the synthetic-data manifest.
def _output_summary(frame: pd.DataFrame, path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve().relative_to(root.resolve())),
        "rows": len(frame),
        "sha256": sha256_file(path),
    }


# Purpose: Orchestrate generation, validation, file writing, and provenance.
# Used by: main; it is also the programmatic entry point for future automation.
def run_generation(project_root: Path, *, seed: int = RANDOM_SEED) -> dict[str, Any]:
    project_root = project_root.resolve()
    output_dir = project_root / "data" / "processed" / "synthetic"
    output_dir.mkdir(parents=True, exist_ok=True)
    public_inputs = load_public_inputs(project_root)

    product = build_product_dimension()
    channel = build_channel_dimension()
    campaign = build_campaign_dimension()
    price_plan = build_price_plan(
        public_inputs["date"], np.random.default_rng(seed + 4)
    )
    demand_drivers = build_demand_drivers(
        public_inputs, price_plan, np.random.default_rng(seed + 1)
    )
    ticket_sales = generate_ticket_sales(
        demand_drivers, np.random.default_rng(seed + 2)
    )
    campaign_daily = build_campaign_daily(ticket_sales, np.random.default_rng(seed + 3))
    daily_plan = build_daily_plan(public_inputs["date"], price_plan)
    validation = validate_synthetic_outputs(
        product,
        channel,
        campaign,
        ticket_sales,
        campaign_daily,
        daily_plan,
        public_inputs["date"],
    )

    outputs = {
        "dim_product": (product, output_dir / "dim_product.csv"),
        "dim_channel": (channel, output_dir / "dim_channel.csv"),
        "dim_campaign": (campaign, output_dir / "dim_campaign.csv"),
        "fact_ticket_sales": (ticket_sales, output_dir / "fact_ticket_sales.csv"),
        "fact_campaign_daily": (
            campaign_daily,
            output_dir / "fact_campaign_daily.csv",
        ),
        "fact_daily_plan": (daily_plan, output_dir / "fact_daily_plan.csv"),
        "daily_demand_drivers": (
            demand_drivers,
            output_dir / "daily_demand_drivers.csv",
        ),
    }
    for frame, path in outputs.values():
        write_csv_atomic(frame, path)

    public_manifest_path = (
        project_root / "data" / "processed" / "public" / "public_data_manifest.json"
    )
    manifest: dict[str, Any] = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "random_seed": seed,
        "all_commercial_data_is_synthetic": True,
        "business_profile": "Fictional mid-size Orlando attraction",
        "history_period": {
            "start": HISTORY_START.isoformat(),
            "end": HISTORY_END.isoformat(),
        },
        "first_forecast_period": {
            "start": "2026-01-01",
            "end": PLAN_END.isoformat(),
        },
        "assumptions": {
            "base_daily_demand": BASE_DAILY_DEMAND,
            "approximate_daily_capacity": 2_500,
            "price_elasticity": PRICE_ELASTICITY,
            "planned_price_adjustment_stddev": PRICE_PLAN_ADJUSTMENT_STDDEV,
            "planned_price_adjustment_limit": PRICE_PLAN_ADJUSTMENT_LIMIT,
            "expected_refund_rate": EXPECTED_REFUND_RATE,
            "maximum_booking_lead_days": 90,
            "order_size_range": [1, 6],
            "product_mix_drivers": [
                "day of week",
                "season",
                "federal holiday",
                "OCPS school break",
                "sales channel",
                "attributed campaign type",
            ],
            "demand_drivers": [
                "day of week",
                "season",
                "federal holiday",
                "OCPS school break",
                "NOAA weather",
                "ticket price",
                "campaign window",
                "MCO passenger benchmark",
                "Orange County TDT benchmark",
                "year trend",
                "random variation",
            ],
        },
        "public_context_manifest": {
            "path": str(public_manifest_path.relative_to(project_root)),
            "sha256": sha256_file(public_manifest_path),
        },
        "validation": validation,
        "outputs": {
            name: _output_summary(frame, path, project_root)
            for name, (frame, path) in outputs.items()
        },
        "limitations": [
            "The commercial records are synthetic and are not operator data.",
            "MCO passengers and TDT remittances are monthly market proxies, not attraction attendance or revenue.",
            "Planned prices include seeded operational variation; downstream price analysis remains associational.",
            "Campaign lift is simulated and later analysis should describe associations rather than real-world causality.",
        ],
    }
    manifest_path = output_dir / "synthetic_data_manifest.json"
    temporary_path = manifest_path.with_suffix(".json.part")
    temporary_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary_path.replace(manifest_path)
    return manifest


# Purpose: Parse CLI options, run generation, and print business-level totals.
# Used by: The module's __main__ entry point.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--seed", type=int, default=RANDOM_SEED)
    args = parser.parse_args()

    manifest = run_generation(args.project_root, seed=args.seed)
    validation = manifest["validation"]
    print("Generated synthetic commercial data:")
    print(f"  order lines: {validation['order_lines']:,}")
    print(f"  units sold: {validation['units_sold']:,}")
    print(f"  refund rate: {validation['refund_rate']:.2%}")
    print(f"  net revenue: ${validation['net_revenue']:,.2f}")
    print(f"  capacity-constrained days: {validation['capacity_constrained_days']:,}")


if __name__ == "__main__":
    main()
