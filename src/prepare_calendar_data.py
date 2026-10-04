"""Build the project date dimension from public calendar sources.

Federal holidays are generated with the ``holidays`` package using U.S.
observed-holiday rules. Multi-day Orange County Public Schools (OCPS) breaks
are controlled transcriptions from the official district calendars linked
below. Summer break starts the day after the last student day and ends the day
before the next first student day.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import holidays
import pandas as pd

OCPS_CALENDAR_SOURCES = {
    "2022-23": (
        "https://files.smartsites.parentsquare.com/6888/"
        "2022-23_ocps_district_calendar_1.pdf"
    ),
    "2023-24": (
        "https://files.smartsites.parentsquare.com/6888/"
        "2023-24_ocps_district_calendar.pdf"
    ),
    "2024-25": (
        "https://files.smartsites.parentsquare.com/6888/"
        "2024-25_ocps_district_calendar_2.pdf"
    ),
    "2025-26": (
        "https://files.smartsites.parentsquare.com/6888/2025-2026_school_calendar.pdf"
    ),
}

OPM_HOLIDAY_SOURCE = (
    "https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/"
)


@dataclass(frozen=True)
class SchoolBreak:
    """A multi-day OCPS break used as a local tourism-demand signal."""

    school_year: str
    name: str
    start_date: date
    end_date: date


def _break(school_year: str, name: str, start_date: str, end_date: str) -> SchoolBreak:
    return SchoolBreak(
        school_year=school_year,
        name=name,
        start_date=date.fromisoformat(start_date),
        end_date=date.fromisoformat(end_date),
    )


SCHOOL_BREAKS = (
    _break("2022-23", "Thanksgiving break", "2022-11-21", "2022-11-25"),
    _break("2022-23", "Winter break", "2022-12-19", "2023-01-02"),
    _break("2022-23", "Spring break", "2023-03-13", "2023-03-17"),
    _break("2022-23", "Summer break", "2023-05-27", "2023-08-09"),
    _break("2023-24", "Thanksgiving break", "2023-11-20", "2023-11-24"),
    _break("2023-24", "Winter break", "2023-12-25", "2024-01-05"),
    _break("2023-24", "Spring break", "2024-03-18", "2024-03-22"),
    _break("2023-24", "Summer break", "2024-05-25", "2024-08-11"),
    _break("2024-25", "Thanksgiving break", "2024-11-25", "2024-11-29"),
    _break("2024-25", "Winter break", "2024-12-23", "2025-01-03"),
    _break("2024-25", "Spring break", "2025-03-17", "2025-03-21"),
    _break("2024-25", "Summer break", "2025-05-29", "2025-08-10"),
    _break("2025-26", "Thanksgiving break", "2025-11-24", "2025-11-28"),
    _break("2025-26", "Winter break", "2025-12-22", "2026-01-02"),
    _break("2025-26", "Spring break", "2026-03-16", "2026-03-20"),
)


def parse_iso_date(value: str | date) -> date:
    """Return a date from an ISO string or an existing date."""

    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _school_break_name(calendar_date: date) -> str | None:
    matches = [
        period.name
        for period in SCHOOL_BREAKS
        if period.start_date <= calendar_date <= period.end_date
    ]
    if len(matches) > 1:
        raise ValueError(f"Overlapping school-break periods on {calendar_date}")
    return matches[0] if matches else None


def _season(calendar_date: date, *, holiday_flag: bool, school_break_flag: bool) -> str:
    """Assign the transparent business-season label used by the star schema."""

    if holiday_flag or school_break_flag:
        return "peak"
    if calendar_date.month in {1, 2, 9, 10}:
        return "off_peak"
    return "shoulder"


def build_date_dimension(start_date: str | date, end_date: str | date) -> pd.DataFrame:
    """Build a continuous date dimension with holiday and school-break flags."""

    start = parse_iso_date(start_date)
    end = parse_iso_date(end_date)
    if end < start:
        raise ValueError("end_date must be on or after start_date")

    federal_holidays = holidays.UnitedStates(
        years=range(start.year, end.year + 1), observed=True
    )
    records: list[dict[str, object]] = []
    current = start

    while current <= end:
        holiday_name = federal_holidays.get(current)
        break_name = _school_break_name(current)
        holiday_flag = holiday_name is not None
        school_break_flag = break_name is not None
        iso_calendar = current.isocalendar()
        week_start = current - timedelta(days=current.isoweekday() - 1)

        records.append(
            {
                "date_key": int(current.strftime("%Y%m%d")),
                "calendar_date": current.isoformat(),
                "day_of_week": current.isoweekday(),
                "day_name": current.strftime("%A"),
                "day_of_year": current.timetuple().tm_yday,
                "week_start": week_start.isoformat(),
                "week_of_year": iso_calendar.week,
                "month_number": current.month,
                "month_name": current.strftime("%B"),
                "quarter_number": ((current.month - 1) // 3) + 1,
                "year_number": current.year,
                "is_weekend": current.isoweekday() >= 6,
                "holiday_flag": holiday_flag,
                "holiday_name": holiday_name,
                "school_break_flag": school_break_flag,
                "season": _season(
                    current,
                    holiday_flag=holiday_flag,
                    school_break_flag=school_break_flag,
                ),
            }
        )
        current += timedelta(days=1)

    frame = pd.DataFrame.from_records(records)
    expected_rows = (end - start).days + 1
    if len(frame) != expected_rows or frame["date_key"].duplicated().any():
        raise ValueError("Date dimension is not continuous and unique")
    return frame


def write_date_dimension(
    output_path: Path, start_date: str | date, end_date: str | date
) -> pd.DataFrame:
    """Build and atomically write the date dimension as CSV."""

    frame = build_date_dimension(start_date, end_date)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".part")
    frame.to_csv(temporary_path, index=False)
    temporary_path.replace(output_path)
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2022-10-03")
    parser.add_argument("--end-date", default="2026-01-30")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/processed/public/dim_date.csv"),
    )
    args = parser.parse_args()

    frame = write_date_dimension(args.output, args.start_date, args.end_date)
    print(f"Wrote {len(frame):,} dates to {args.output}")


if __name__ == "__main__":
    main()
