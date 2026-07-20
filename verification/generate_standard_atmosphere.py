"""Generate Astra's frozen 0-4 km U.S. Standard Atmosphere 1976 table.

Authority: NOAA-S/T 76-1562 / NASA-TM-X-74335.  The equations and
constants below are the defining lower-atmosphere relations in that report;
dynamic viscosity is Equation 51 and kinematic viscosity is Equation 52.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "domain_packs" / "bwb" / "data"
CSV_PATH = DATA_DIR / "us_standard_atmosphere_1976_0_4000m.csv"
METADATA_PATH = DATA_DIR / "us_standard_atmosphere_1976.metadata.json"

SOURCE_ID = "USSA1976-NOAA-S/T-76-1562-NASA-TM-X-74335"
EARTH_RADIUS_M = 6_356_766.0
SEA_LEVEL_TEMPERATURE_K = 288.15
SEA_LEVEL_PRESSURE_PA = 101_325.0
LAPSE_RATE_KPM = -0.0065
STANDARD_GRAVITY_MPS2 = 9.80665
MOLAR_MASS_KGPMOL = 0.0289644
UNIVERSAL_GAS_CONSTANT = 8.31432
SUTHERLAND_BETA = 1.458e-6
SUTHERLAND_K = 110.4
PRESSURE_EXPONENT = -STANDARD_GRAVITY_MPS2 * MOLAR_MASS_KGPMOL / (
    UNIVERSAL_GAS_CONSTANT * LAPSE_RATE_KPM
)

FIELDS = (
    "geometric_altitude_m",
    "geopotential_altitude_m",
    "temperature_k",
    "pressure_pa",
    "density_kgm3",
    "dynamic_viscosity_kgms",
    "kinematic_viscosity_m2s",
)


def standard_row(geometric_altitude_m: float) -> dict[str, float]:
    geopotential = (
        EARTH_RADIUS_M
        * geometric_altitude_m
        / (EARTH_RADIUS_M + geometric_altitude_m)
    )
    temperature = SEA_LEVEL_TEMPERATURE_K + LAPSE_RATE_KPM * geopotential
    pressure = SEA_LEVEL_PRESSURE_PA * (
        temperature / SEA_LEVEL_TEMPERATURE_K
    ) ** PRESSURE_EXPONENT
    density = (
        pressure
        * MOLAR_MASS_KGPMOL
        / (UNIVERSAL_GAS_CONSTANT * temperature)
    )
    dynamic_viscosity = (
        SUTHERLAND_BETA
        * temperature**1.5
        / (temperature + SUTHERLAND_K)
    )
    return {
        "geometric_altitude_m": geometric_altitude_m,
        "geopotential_altitude_m": geopotential,
        "temperature_k": temperature,
        "pressure_pa": pressure,
        "density_kgm3": density,
        "dynamic_viscosity_kgms": dynamic_viscosity,
        "kinematic_viscosity_m2s": dynamic_viscosity / density,
    }


def csv_text() -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for altitude_m in range(0, 4001, 100):
        row = standard_row(float(altitude_m))
        # Thirteen significant digits retain far more precision than the
        # engineering inputs while removing platform-libm differences in the
        # final one or two binary64 digits (observed between Linux and Windows).
        writer.writerow({field: format(row[field], ".13g") for field in FIELDS})
    return stream.getvalue()


def metadata(csv_sha256: str) -> dict:
    return {
        "source": SOURCE_ID,
        "title": "U.S. Standard Atmosphere, 1976",
        "reports": ["NOAA-S/T 76-1562", "NASA-TM-X-74335"],
        "source_urls": [
            "https://ntrs.nasa.gov/citations/19770009539",
            "https://www.ngdc.noaa.gov/stp/space-weather/online-publications/"
            "miscellaneous/us-standard-atmosphere-1976/"
            "us-standard-atmosphere_st76-1562_noaa.pdf",
        ],
        "altitude_type": "geometric altitude above mean sea level",
        "minimum_altitude_m": 0.0,
        "maximum_altitude_m": 4000.0,
        "row_interval_m": 100.0,
        "row_count": 41,
        "units": {
            "geometric_altitude_m": "m",
            "geopotential_altitude_m": "m",
            "temperature_k": "K",
            "pressure_pa": "Pa",
            "density_kgm3": "kg/m^3",
            "dynamic_viscosity_kgms": "kg/(m*s)",
            "kinematic_viscosity_m2s": "m^2/s",
        },
        "interpolation": {
            "method": "linear",
            "fields": [
                "geopotential_altitude_m",
                "temperature_k",
                "pressure_pa",
                "density_kgm3",
                "dynamic_viscosity_kgms",
            ],
            "derived_fields": {
                "kinematic_viscosity_m2s": "dynamic_viscosity_kgms / density_kgm3"
            },
            "out_of_range": "reject",
        },
        "equations": {
            "dynamic_viscosity": "USSA1976 Equation 51",
            "kinematic_viscosity": "USSA1976 Equation 52",
        },
        "generator": "verification/generate_standard_atmosphere.py",
        "generator_version": 2,
        "checksum_encoding": "SHA-256 of UTF-8 CSV with LF line endings",
        "csv_sha256": csv_sha256,
    }


def generated_content() -> tuple[str, str]:
    table = csv_text()
    digest = hashlib.sha256(table.encode("utf-8")).hexdigest()
    meta = json.dumps(metadata(digest), indent=2, sort_keys=True) + "\n"
    return table, meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true", help="fail if committed artifacts differ"
    )
    args = parser.parse_args()
    table, meta = generated_content()
    if args.check:
        actual_table = CSV_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
        actual_meta = METADATA_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
        if actual_table != table or actual_meta != meta:
            raise SystemExit("committed standard-atmosphere artifacts are stale")
        print("standard-atmosphere artifacts match generator")
        return 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CSV_PATH.write_text(table, encoding="utf-8", newline="\n")
    METADATA_PATH.write_text(meta, encoding="utf-8", newline="\n")
    print(f"wrote {CSV_PATH.relative_to(REPO_ROOT)}")
    print(f"wrote {METADATA_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
