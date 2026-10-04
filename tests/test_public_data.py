from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.download_public_data import (
    DataValidationError,
    normalize_noaa_records,
    parse_mco_enplaned_text,
    parse_tdt_table,
)
from src.prepare_calendar_data import build_date_dimension


def test_date_dimension_marks_holidays_and_ocps_breaks() -> None:
    frame = build_date_dimension("2023-12-24", "2024-01-10")
    by_date = frame.set_index("calendar_date")

    assert bool(by_date.loc["2023-12-25", "holiday_flag"])
    assert "Christmas" in by_date.loc["2023-12-25", "holiday_name"]
    assert bool(by_date.loc["2024-01-05", "school_break_flag"])
    assert not bool(by_date.loc["2024-01-09", "school_break_flag"])
    assert by_date.loc["2024-01-01", "week_start"] == "2024-01-01"
    assert by_date.loc["2024-01-06", "day_of_week"] == 6
    assert by_date["date_key"].is_unique


def test_noaa_normalization_and_severe_weather_proxy() -> None:
    records = [
        {
            "DATE": "2023-01-01",
            "TMIN": "60",
            "TMAX": "80",
            "PRCP": "T",
            "AWND": "4.0",
            "WSF5": "12.0",
        },
        {
            "DATE": "2023-01-02",
            "TMIN": "64",
            "TMAX": "82",
            "PRCP": "1.25",
            "AWND": "8.0",
            "WSF5": "22.0",
        },
    ]

    frame = normalize_noaa_records(records, "2023-01-01", "2023-01-02")

    assert frame.loc[0, "avg_temperature_f"] == 70.0
    assert frame.loc[0, "precipitation_in"] == 0.0
    assert not bool(frame.loc[0, "severe_weather_flag"])
    assert bool(frame.loc[1, "severe_weather_flag"])
    assert frame["source_station_id"].unique().tolist() == ["USW00012815"]


def test_noaa_rejects_missing_dates() -> None:
    records = [
        {
            "DATE": "2023-01-01",
            "TMIN": "60",
            "TMAX": "80",
            "PRCP": "0",
            "AWND": "0",
            "WSF5": "0",
        }
    ]

    with pytest.raises(DataValidationError, match="missing 1 date"):
        normalize_noaa_records(records, "2023-01-01", "2023-01-02")


def test_mco_pdf_text_parser() -> None:
    monthly_values = " ".join(f"{2_000_000 + month:,}" for month in range(1, 13))
    text = f"January February March\n2023 {monthly_values}\nSource: reports"

    frame = parse_mco_enplaned_text(text, "2023-01-01", "2023-12-31")

    assert len(frame) == 12
    assert frame.iloc[0]["month_start"] == "2023-01-01"
    assert frame.iloc[-1]["month_start"] == "2023-12-01"
    assert frame.iloc[-1]["enplaned_passengers"] == 2_000_012


def test_tdt_fiscal_year_matrix_is_converted_to_calendar_months() -> None:
    month_labels = [
        "OCTOBER",
        "NOVEMBER",
        "DECEMBER",
        "JANUARY",
        "FEBRUARY (1)",
        "MARCH",
        "APRIL",
        "MAY",
        "JUNE",
        "JULY",
        "AUGUST",
        "SEPTEMBER",
    ]
    rows: list[list[object]] = [[None, "FY 2022-23", None, "FY 2023-24"]]
    for offset, label in enumerate(month_labels):
        rows.append([label, 100 + offset, None, 200 + offset])
    table = pd.DataFrame(rows)

    frame = parse_tdt_table(table, "2023-01-01", "2023-12-31")

    assert len(frame) == 12
    assert frame.iloc[0].to_dict()["remittance_usd"] == 103
    assert frame.iloc[-1].to_dict()["remittance_usd"] == 202
    assert frame["month_start"].tolist() == [
        month.strftime("%Y-%m-%d")
        for month in pd.date_range(date(2023, 1, 1), date(2023, 12, 1), freq="MS")
    ]


def test_tdt_parser_rejects_incomplete_range() -> None:
    table = pd.DataFrame(
        [
            [None, "FY 2022-23"],
            ["JANUARY", 10],
        ]
    )

    with pytest.raises(DataValidationError, match="missing 11 month"):
        parse_tdt_table(table, "2023-01-01", "2023-12-31")
