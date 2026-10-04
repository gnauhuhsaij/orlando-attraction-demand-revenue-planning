"""Download and prepare the public contextual data used by the project.

The outputs in this module are real public observations or calendars. They are
benchmarks and demand drivers, not attraction ticket sales. Synthetic commercial
facts are generated separately so their provenance remains unambiguous.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from calendar import month_name
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from pypdf import PdfReader
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.prepare_calendar_data import (
    OCPS_CALENDAR_SOURCES,
    OPM_HOLIDAY_SOURCE,
    write_date_dimension,
)

NOAA_API_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
NOAA_DATASET = "daily-summaries"
NOAA_STATION_ID = "USW00012815"
NOAA_STATION_PAGE = (
    "https://www.ncei.noaa.gov/cdo-web/datasets/GHCND/stations/GHCND:USW00012815/detail"
)
MCO_PASSENGER_URL = (
    "https://assets.ctfassets.net/qiecpoxp4bka/4yGxpCfUSlYSwUO2gzNCPD/"
    "4f9a2860522bdd0d130c6fee13bc43f0/Enplaned_by_Month.pdf"
)
MCO_TRAFFIC_PAGE = "https://web.goaa.aero/airport-business/traffic-statistics/"
TDT_WORKBOOK_URL = (
    "https://www.occompt.com/DocumentCenter/View/51848/"
    "TDT-Monthly-Remittances-Per-Fiscal-Year-XSLX"
)
TDT_LANDING_PAGE = "https://www.occompt.com/quicklinks.aspx?CID=39"

DEFAULT_WEATHER_START = date(2023, 1, 1)
DEFAULT_WEATHER_END = date(2025, 12, 31)
DEFAULT_CALENDAR_START = date(2022, 10, 3)
DEFAULT_CALENDAR_END = date(2026, 1, 30)

MONTH_LOOKUP = {name.upper(): number for number, name in enumerate(month_name) if name}


class DataValidationError(ValueError):
    """Raised when a downloaded source cannot meet the data contract."""


# Purpose: Convert a CLI ISO date string to a date while accepting existing dates.
# Used by: normalize_noaa_records, parse_mco_enplaned_text, parse_tdt_table,
# and run_pipeline.
def parse_iso_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


# Purpose: Configure one HTTP session with retries, timeouts, and proxy behavior.
# Used by: run_pipeline.
def build_http_session(*, trust_environment: bool = True) -> requests.Session:
    """Create a retrying HTTP session for idempotent public-data downloads."""

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "orlando-demand-revenue-planning/1.0 (portfolio project)"}
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.trust_env = trust_environment
    return session


# Purpose: Stream one public source to disk and replace the destination atomically.
# Used by: run_pipeline for NOAA, MCO, TDT, and OCPS source files.
def download_file(
    session: requests.Session,
    url: str,
    destination: Path,
    *,
    params: Mapping[str, str] | None = None,
    minimum_bytes: int = 100,
) -> Path:
    """Download a URL atomically and reject empty or error-page responses."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.with_suffix(destination.suffix + ".part")
    with session.get(url, params=params, stream=True, timeout=(20, 180)) as response:
        response.raise_for_status()
        with temporary_path.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)

    if temporary_path.stat().st_size < minimum_bytes:
        temporary_path.unlink(missing_ok=True)
        raise DataValidationError(f"Downloaded file is unexpectedly small: {url}")
    temporary_path.replace(destination)
    return destination


# Purpose: Calculate a reproducibility checksum for one downloaded raw file.
# Used by: run_pipeline when it builds the public-data manifest.
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# Purpose: Safely convert a NOAA field to a number, including trace precipitation.
# Used by: normalize_noaa_records.
def _numeric(value: Any, field_name: str, record_date: str) -> float:
    if value is None or str(value).strip() == "":
        raise DataValidationError(f"Missing {field_name} on {record_date}")
    if str(value).strip().upper() == "T":
        return 0.0
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError as error:
        raise DataValidationError(
            f"Invalid {field_name} value {value!r} on {record_date}"
        ) from error


# Purpose: Validate and reshape NOAA JSON records for the fact_weather table.
# Used by: run_pipeline and the public-data unit tests.
def normalize_noaa_records(
    records: Sequence[Mapping[str, Any]],
    start_date: str | date,
    end_date: str | date,
) -> pd.DataFrame:
    """Normalize NOAA daily summaries into the ``fact_weather`` load shape."""

    start = parse_iso_date(start_date)
    end = parse_iso_date(end_date)
    normalized: list[dict[str, object]] = []

    for record in records:
        record_date = str(record.get("DATE", "")).strip()
        try:
            calendar_date = date.fromisoformat(record_date)
        except ValueError as error:
            raise DataValidationError(
                f"Invalid NOAA DATE value: {record_date!r}"
            ) from error
        if not start <= calendar_date <= end:
            continue

        minimum = _numeric(record.get("TMIN"), "TMIN", record_date)
        maximum = _numeric(record.get("TMAX"), "TMAX", record_date)
        precipitation = _numeric(record.get("PRCP"), "PRCP", record_date)
        average_wind = _numeric(record.get("AWND", 0), "AWND", record_date)
        wind_gust = _numeric(record.get("WSF5", 0), "WSF5", record_date)
        if maximum < minimum:
            raise DataValidationError(f"TMAX is below TMIN on {record_date}")
        if precipitation < 0:
            raise DataValidationError(f"Negative precipitation on {record_date}")

        # This is an explicitly derived operational-risk proxy, not a NOAA
        # severe-weather classification: >=1 inch rain, >=20 mph mean wind, or
        # >=35 mph fastest five-second wind.
        severe_weather = (
            precipitation >= 1.0 or average_wind >= 20.0 or wind_gust >= 35.0
        )
        normalized.append(
            {
                "date_key": int(calendar_date.strftime("%Y%m%d")),
                "min_temperature_f": round(minimum, 2),
                "avg_temperature_f": round((minimum + maximum) / 2, 2),
                "max_temperature_f": round(maximum, 2),
                "precipitation_in": round(precipitation, 3),
                "severe_weather_flag": severe_weather,
                "source_name": "NOAA NCEI Daily Summaries",
                "source_station_id": NOAA_STATION_ID,
            }
        )

    frame = pd.DataFrame.from_records(normalized)
    if frame.empty:
        raise DataValidationError(
            "NOAA response contains no rows in the requested range"
        )
    if frame["date_key"].duplicated().any():
        raise DataValidationError("NOAA response contains duplicate dates")

    observed = pd.to_datetime(frame["date_key"].astype(str), format="%Y%m%d")
    expected = pd.date_range(start, end, freq="D")
    missing = expected.difference(observed)
    if not missing.empty:
        preview = ", ".join(day.strftime("%Y-%m-%d") for day in missing[:5])
        raise DataValidationError(
            f"NOAA data is missing {len(missing)} date(s): {preview}"
        )
    return frame.sort_values("date_key").reset_index(drop=True)


# Purpose: Extract machine-readable text from every page of a downloaded PDF.
# Used by: run_pipeline before parsing the MCO passenger report.
def extract_pdf_text(path: Path) -> str:
    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# Purpose: Generate the complete sequence of month starts expected for a range.
# Used by: _validate_monthly_coverage.
def _expected_months(start_date: date, end_date: date) -> pd.DatetimeIndex:
    return pd.date_range(start_date.replace(day=1), end_date.replace(day=1), freq="MS")


# Purpose: Reject empty, duplicated, or incomplete monthly benchmark data.
# Used by: parse_mco_enplaned_text and parse_tdt_table.
def _validate_monthly_coverage(
    frame: pd.DataFrame, start_date: date, end_date: date, dataset_name: str
) -> None:
    if frame.empty:
        raise DataValidationError(f"{dataset_name} contains no rows in range")
    observed = pd.DatetimeIndex(pd.to_datetime(frame["month_start"]))
    if observed.duplicated().any():
        raise DataValidationError(f"{dataset_name} contains duplicate months")
    expected = _expected_months(start_date, end_date)
    missing = expected.difference(observed)
    if not missing.empty:
        preview = ", ".join(month.strftime("%Y-%m") for month in missing[:5])
        raise DataValidationError(
            f"{dataset_name} is missing {len(missing)} month(s): {preview}"
        )


# Purpose: Convert the GOAA PDF text into one MCO passenger record per month.
# Used by: run_pipeline and the public-data unit tests.
def parse_mco_enplaned_text(
    text: str, start_date: str | date, end_date: str | date
) -> pd.DataFrame:
    """Parse GOAA's monthly enplaned-passenger PDF text."""

    start = parse_iso_date(start_date)
    end = parse_iso_date(end_date)
    records: list[dict[str, object]] = []
    year_pattern = re.compile(r"^(19\d{2}|20\d{2})\s+(.+)$")

    for line in text.splitlines():
        match = year_pattern.match(" ".join(line.split()))
        if not match:
            continue
        year = int(match.group(1))
        values = [
            int(value.replace(",", ""))
            for value in re.findall(r"\d[\d,]*", match.group(2))
        ]
        for month_number, passengers in enumerate(values[:12], start=1):
            month_start = date(year, month_number, 1)
            if start.replace(day=1) <= month_start <= end.replace(day=1):
                records.append(
                    {
                        "month_start": month_start.isoformat(),
                        "enplaned_passengers": passengers,
                        "source_name": "Greater Orlando Aviation Authority",
                        "source_url": MCO_TRAFFIC_PAGE,
                        "intended_use": "market benchmark and synthetic-demand calibration",
                    }
                )

    frame = pd.DataFrame.from_records(records)
    _validate_monthly_coverage(frame, start, end, "MCO passenger data")
    return frame.sort_values("month_start").reset_index(drop=True)


# Purpose: Read the starting year from a fiscal-year workbook header.
# Used by: parse_tdt_table.
def _fiscal_year_start(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*FY\s+(\d{4})-+(\d{2})\s*", value)
    return int(match.group(1)) if match else None


# Purpose: Normalize a TDT workbook row label into a calendar month number.
# Used by: parse_tdt_table.
def _month_number(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s*\(\d+\)\s*$", "", value).strip().upper()
    return MONTH_LOOKUP.get(cleaned)


# Purpose: Reshape the TDT fiscal-year matrix into validated calendar-month rows.
# Used by: read_tdt_workbook and the public-data unit tests.
def parse_tdt_table(
    table: pd.DataFrame, start_date: str | date, end_date: str | date
) -> pd.DataFrame:
    """Reshape the Comptroller fiscal-year matrix into calendar months."""

    start = parse_iso_date(start_date)
    end = parse_iso_date(end_date)
    month_records: dict[date, dict[str, object]] = {}

    for header_row in range(len(table)):
        for column in range(table.shape[1]):
            fiscal_start = _fiscal_year_start(table.iat[header_row, column])
            if fiscal_start is None:
                continue
            fiscal_label = f"{fiscal_start}-{str(fiscal_start + 1)[-2:]}"
            for row in range(header_row + 1, min(header_row + 18, len(table))):
                number = _month_number(table.iat[row, 0])
                if number is None:
                    continue
                value = table.iat[row, column]
                if pd.isna(value):
                    continue
                calendar_year = fiscal_start if number >= 10 else fiscal_start + 1
                month_start = date(calendar_year, number, 1)
                if not start.replace(day=1) <= month_start <= end.replace(day=1):
                    continue
                try:
                    remittance = round(float(value))
                except (TypeError, ValueError) as error:
                    raise DataValidationError(
                        f"Invalid TDT value {value!r} for {month_start}"
                    ) from error
                if remittance < 0:
                    raise DataValidationError(f"Negative TDT value for {month_start}")
                record = {
                    "month_start": month_start.isoformat(),
                    "fiscal_year": fiscal_label,
                    "remittance_usd": remittance,
                    "source_name": "Orange County Comptroller",
                    "source_url": TDT_LANDING_PAGE,
                    "intended_use": "market benchmark and synthetic-demand calibration",
                }
                previous = month_records.get(month_start)
                if previous and previous["remittance_usd"] != remittance:
                    raise DataValidationError(
                        f"Conflicting TDT values found for {month_start}"
                    )
                month_records[month_start] = record

    frame = pd.DataFrame.from_records(list(month_records.values()))
    _validate_monthly_coverage(frame, start, end, "Orange County TDT data")
    return frame.sort_values("month_start").reset_index(drop=True)


# Purpose: Read the official TDT Excel file without modifying the source workbook.
# Used by: run_pipeline.
def read_tdt_workbook(
    path: Path, start_date: str | date, end_date: str | date
) -> pd.DataFrame:
    table = pd.read_excel(path, sheet_name=0, header=None, engine="openpyxl")
    return parse_tdt_table(table, start_date, end_date)


# Purpose: Write a processed DataFrame without leaving a partial final CSV.
# Used by: run_pipeline for weather, MCO passenger, and TDT outputs.
def write_csv_atomic(frame: pd.DataFrame, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.with_suffix(destination.suffix + ".part")
    frame.to_csv(temporary_path, index=False)
    temporary_path.replace(destination)


# Purpose: Express an artifact path relative to the repository for portability.
# Used by: _output_summary and run_pipeline's raw-file checksum records.
def _relative(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


# Purpose: Summarize a processed dataset's path, size, and time coverage.
# Used by: run_pipeline when it builds the public-data manifest.
def _output_summary(
    frame: pd.DataFrame, path: Path, root: Path, date_column: str
) -> dict[str, object]:
    return {
        "path": _relative(path, root),
        "rows": len(frame),
        "first_period": str(frame[date_column].min()),
        "last_period": str(frame[date_column].max()),
    }


# Purpose: Orchestrate downloading, validation, transformation, and provenance.
# Used by: main; it is also the programmatic entry point for future automation.
def run_pipeline(
    project_root: Path,
    *,
    weather_start: str | date = DEFAULT_WEATHER_START,
    weather_end: str | date = DEFAULT_WEATHER_END,
    calendar_start: str | date = DEFAULT_CALENDAR_START,
    calendar_end: str | date = DEFAULT_CALENDAR_END,
    skip_download: bool = False,
    trust_environment: bool = True,
) -> dict[str, object]:
    """Download sources, prepare outputs, and write a provenance manifest."""

    project_root = project_root.resolve()
    raw_dir = project_root / "data" / "raw" / "public"
    processed_dir = project_root / "data" / "processed" / "public"
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    weather_start_date = parse_iso_date(weather_start)
    weather_end_date = parse_iso_date(weather_end)
    calendar_start_date = parse_iso_date(calendar_start)
    calendar_end_date = parse_iso_date(calendar_end)

    noaa_path = raw_dir / "noaa_mco_daily_2023_2025.json"
    mco_path = raw_dir / "mco_enplaned_passengers_by_month.pdf"
    tdt_path = raw_dir / "orange_county_tdt_monthly_remittances.xlsx"
    ocps_paths = {
        school_year: raw_dir / f"ocps_calendar_{school_year}.pdf"
        for school_year in OCPS_CALENDAR_SOURCES
    }

    if not skip_download:
        session = build_http_session(trust_environment=trust_environment)
        download_file(
            session,
            NOAA_API_URL,
            noaa_path,
            params={
                "dataset": NOAA_DATASET,
                "stations": NOAA_STATION_ID,
                "startDate": weather_start_date.isoformat(),
                "endDate": weather_end_date.isoformat(),
                "format": "json",
                "units": "standard",
                "includeAttributes": "true",
            },
            minimum_bytes=1_000,
        )
        download_file(session, MCO_PASSENGER_URL, mco_path, minimum_bytes=10_000)
        download_file(session, TDT_WORKBOOK_URL, tdt_path, minimum_bytes=10_000)
        for school_year, url in OCPS_CALENDAR_SOURCES.items():
            download_file(session, url, ocps_paths[school_year], minimum_bytes=1_000)

    required_files = [noaa_path, mco_path, tdt_path, *ocps_paths.values()]
    missing_files = [str(path) for path in required_files if not path.exists()]
    if missing_files:
        raise FileNotFoundError(
            "Missing raw source files. Run without --skip-download first: "
            + ", ".join(missing_files)
        )

    with noaa_path.open(encoding="utf-8") as source:
        noaa_records = json.load(source)
    if not isinstance(noaa_records, list):
        raise DataValidationError("NOAA JSON response is not a list of daily records")

    weather = normalize_noaa_records(noaa_records, weather_start_date, weather_end_date)
    mco = parse_mco_enplaned_text(
        extract_pdf_text(mco_path), weather_start_date, weather_end_date
    )
    tdt = read_tdt_workbook(tdt_path, weather_start_date, weather_end_date)

    date_output = processed_dir / "dim_date.csv"
    weather_output = processed_dir / "fact_weather.csv"
    mco_output = processed_dir / "mco_enplaned_passengers_monthly.csv"
    tdt_output = processed_dir / "orange_county_tdt_monthly.csv"
    date_dimension = write_date_dimension(
        date_output, calendar_start_date, calendar_end_date
    )
    write_csv_atomic(weather, weather_output)
    write_csv_atomic(mco, mco_output)
    write_csv_atomic(tdt, tdt_output)

    raw_checksums = {
        _relative(path, project_root): sha256_file(path) for path in required_files
    }
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    manifest: dict[str, object] = {
        "generated_at_utc": generated_at,
        "scope": (
            "Public contextual data only. No attraction ticket sales, prices, "
            "refunds, campaign results, or operator revenue are represented."
        ),
        "sources": [
            {
                "dataset_id": "noaa_mco_daily_weather",
                "data_type": "public_observed",
                "publisher": "NOAA National Centers for Environmental Information",
                "source_url": NOAA_STATION_PAGE,
                "station_id": NOAA_STATION_ID,
                "units": "Fahrenheit, inches, and miles per hour",
                "transformation": (
                    "Daily TMIN/TMAX average; severe_weather_flag is a derived "
                    "operational proxy based on rain and wind thresholds."
                ),
            },
            {
                "dataset_id": "us_federal_holidays",
                "data_type": "public_calendar",
                "publisher": "U.S. Office of Personnel Management",
                "source_url": OPM_HOLIDAY_SOURCE,
                "transformation": "Generated with U.S. observed-holiday rules.",
            },
            {
                "dataset_id": "ocps_school_breaks",
                "data_type": "public_calendar",
                "publisher": "Orange County Public Schools",
                "source_urls": OCPS_CALENDAR_SOURCES,
                "transformation": (
                    "Controlled transcription of Thanksgiving, winter, spring, "
                    "and summer breaks in src/prepare_calendar_data.py."
                ),
            },
            {
                "dataset_id": "mco_enplaned_passengers",
                "data_type": "public_observed_benchmark",
                "publisher": "Greater Orlando Aviation Authority",
                "source_url": MCO_TRAFFIC_PAGE,
                "limitation": "Market proxy; not attraction attendance.",
            },
            {
                "dataset_id": "orange_county_tdt_remittances",
                "data_type": "public_observed_benchmark",
                "publisher": "Orange County Comptroller",
                "source_url": TDT_LANDING_PAGE,
                "limitation": "County tourism-tax benchmark; not attraction revenue.",
            },
        ],
        "raw_file_sha256": raw_checksums,
        "outputs": {
            "dim_date": _output_summary(
                date_dimension, date_output, project_root, "calendar_date"
            ),
            "fact_weather": _output_summary(
                weather, weather_output, project_root, "date_key"
            ),
            "mco_enplaned_passengers_monthly": _output_summary(
                mco, mco_output, project_root, "month_start"
            ),
            "orange_county_tdt_monthly": _output_summary(
                tdt, tdt_output, project_root, "month_start"
            ),
        },
    }
    manifest_path = processed_dir / "public_data_manifest.json"
    temporary_manifest = manifest_path.with_suffix(".json.part")
    temporary_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary_manifest.replace(manifest_path)
    return manifest


# Purpose: Parse command-line options, run the pipeline, and print its row counts.
# Used by: the module's __main__ entry point.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--weather-start", default=DEFAULT_WEATHER_START.isoformat())
    parser.add_argument("--weather-end", default=DEFAULT_WEATHER_END.isoformat())
    parser.add_argument("--calendar-start", default=DEFAULT_CALENDAR_START.isoformat())
    parser.add_argument("--calendar-end", default=DEFAULT_CALENDAR_END.isoformat())
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Reuse existing files under data/raw/public.",
    )
    parser.add_argument(
        "--ignore-environment-proxy",
        action="store_true",
        help="Ignore HTTP_PROXY/HTTPS_PROXY when local proxy settings are stale.",
    )
    args = parser.parse_args()

    manifest = run_pipeline(
        args.project_root,
        weather_start=args.weather_start,
        weather_end=args.weather_end,
        calendar_start=args.calendar_start,
        calendar_end=args.calendar_end,
        skip_download=args.skip_download,
        trust_environment=not args.ignore_environment_proxy,
    )
    print("Prepared public data:")
    for name, details in manifest["outputs"].items():
        print(f"  {name}: {details['rows']:,} rows -> {details['path']}")


if __name__ == "__main__":
    main()
