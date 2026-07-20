"""Table-driven U.S. Standard Atmosphere 1976 lookup for the BWB pack."""
from __future__ import annotations

import bisect
import csv
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


SOURCE_ID = "USSA1976-NOAA-S/T-76-1562-NASA-TM-X-74335"
_DATA_DIR = Path(__file__).with_name("data")
_CSV_PATH = _DATA_DIR / "us_standard_atmosphere_1976_0_4000m.csv"
_METADATA_PATH = _DATA_DIR / "us_standard_atmosphere_1976.metadata.json"
_INTERPOLATED_FIELDS = (
    "geopotential_altitude_m",
    "temperature_k",
    "pressure_pa",
    "density_kgm3",
    "dynamic_viscosity_kgms",
)


class InterpolationProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    lower_table_altitude_m: float
    upper_table_altitude_m: float
    fraction: float = Field(ge=0.0, le=1.0)
    exact_row: bool


class AtmosphereState(BaseModel):
    model_config = ConfigDict(frozen=True)

    geometric_altitude_m: float
    geopotential_altitude_m: float
    temperature_k: float
    temperature_c: float
    pressure_pa: float
    density_kgm3: float
    dynamic_viscosity_kgms: float
    kinematic_viscosity_m2s: float
    source: str
    dataset_sha256: str
    interpolation: InterpolationProvenance

    @model_validator(mode="after")
    def validate_physics_and_provenance(self):
        numeric = (
            self.geometric_altitude_m,
            self.geopotential_altitude_m,
            self.temperature_k,
            self.temperature_c,
            self.pressure_pa,
            self.density_kgm3,
            self.dynamic_viscosity_kgms,
            self.kinematic_viscosity_m2s,
        )
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("atmosphere values must be finite")
        if not 0.0 <= self.geometric_altitude_m <= 4000.0:
            raise ValueError("atmosphere altitude outside [0, 4000] m")
        if any(value <= 0.0 for value in (
            self.temperature_k,
            self.pressure_pa,
            self.density_kgm3,
            self.dynamic_viscosity_kgms,
            self.kinematic_viscosity_m2s,
        )):
            raise ValueError("atmosphere thermodynamic values must be positive")
        if not math.isclose(
            self.kinematic_viscosity_m2s,
            self.dynamic_viscosity_kgms / self.density_kgm3,
            rel_tol=2e-13,
            abs_tol=0.0,
        ):
            raise ValueError("kinematic viscosity must equal dynamic viscosity / density")
        if not math.isclose(
            self.temperature_c, self.temperature_k - 273.15,
            rel_tol=0.0, abs_tol=1e-10,
        ):
            raise ValueError("Celsius and kelvin temperatures are inconsistent")
        if self.source != SOURCE_ID:
            raise ValueError("atmosphere source is not the canonical USSA1976 source")
        if self.dataset_sha256 != DATASET_SHA256:
            raise ValueError("atmosphere dataset checksum is not canonical")
        provenance = self.interpolation
        lower = provenance.lower_table_altitude_m
        upper = provenance.upper_table_altitude_m
        if not lower <= self.geometric_altitude_m <= upper:
            raise ValueError("atmosphere interpolation bounds do not contain altitude")
        exact = math.isclose(lower, upper, rel_tol=0.0, abs_tol=0.0)
        if exact != provenance.exact_row:
            raise ValueError("atmosphere exact-row provenance is inconsistent")
        expected_fraction = 0.0 if exact else (
            (self.geometric_altitude_m - lower) / (upper - lower)
        )
        if not math.isclose(
            provenance.fraction, expected_fraction, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("atmosphere interpolation fraction is inconsistent")
        canonical = _canonical_values(self.geometric_altitude_m)
        for field in _INTERPOLATED_FIELDS:
            if not math.isclose(
                getattr(self, field), canonical[field],
                rel_tol=2e-12, abs_tol=0.0,
            ):
                raise ValueError(
                    f"atmosphere {field} does not match canonical table"
                )
        return self


class _AtmosphereRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    geometric_altitude_m: float
    geopotential_altitude_m: float
    temperature_k: float
    pressure_pa: float
    density_kgm3: float
    dynamic_viscosity_kgms: float
    kinematic_viscosity_m2s: float


def _canonical_bytes(path: Path) -> bytes:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")


@lru_cache(maxsize=1)
def _metadata() -> dict[str, Any]:
    data = json.loads(_METADATA_PATH.read_text(encoding="utf-8"))
    if data.get("source") != SOURCE_ID:
        raise ValueError(f"unexpected atmosphere source {data.get('source')!r}")
    digest = hashlib.sha256(_canonical_bytes(_CSV_PATH)).hexdigest()
    if digest != data.get("csv_sha256"):
        raise ValueError(
            f"standard-atmosphere checksum mismatch: expected "
            f"{data.get('csv_sha256')}, got {digest}"
        )
    return data


DATASET_SHA256 = str(_metadata()["csv_sha256"])


@lru_cache(maxsize=1)
def _rows() -> tuple[_AtmosphereRow, ...]:
    meta = _metadata()
    with _CSV_PATH.open(newline="", encoding="utf-8") as stream:
        rows = tuple(
            _AtmosphereRow(**{key: float(value) for key, value in raw.items()})
            for raw in csv.DictReader(stream)
        )
    if len(rows) != int(meta["row_count"]):
        raise ValueError(
            f"standard-atmosphere row count {len(rows)} != {meta['row_count']}"
        )
    altitudes = [row.geometric_altitude_m for row in rows]
    if altitudes != sorted(set(altitudes)):
        raise ValueError("standard-atmosphere altitudes must be ordered and unique")
    expected = [
        meta["minimum_altitude_m"] + i * meta["row_interval_m"]
        for i in range(int(meta["row_count"]))
    ]
    if altitudes != expected or altitudes[-1] != meta["maximum_altitude_m"]:
        raise ValueError("standard-atmosphere altitude grid is incomplete")
    for row in rows:
        values = (
            row.geopotential_altitude_m,
            row.temperature_k,
            row.pressure_pa,
            row.density_kgm3,
            row.dynamic_viscosity_kgms,
            row.kinematic_viscosity_m2s,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"non-finite atmosphere value at {row.geometric_altitude_m} m")
        if any(value <= 0.0 for value in values[1:]):
            raise ValueError(f"non-positive atmosphere value at {row.geometric_altitude_m} m")
        expected_nu = row.dynamic_viscosity_kgms / row.density_kgm3
        if not math.isclose(
            row.kinematic_viscosity_m2s, expected_nu, rel_tol=2e-12, abs_tol=0.0
        ):
            raise ValueError(f"nu != mu/rho at {row.geometric_altitude_m} m")
    return rows


def _lerp(lower: float, upper: float, fraction: float) -> float:
    return lower + fraction * (upper - lower)


def _canonical_values(altitude: float) -> dict[str, float]:
    rows = _rows()
    altitudes = tuple(row.geometric_altitude_m for row in rows)
    index = bisect.bisect_left(altitudes, altitude)
    if index < len(rows) and altitudes[index] == altitude:
        lower = upper = rows[index]
        fraction = 0.0
    else:
        lower = rows[index - 1]
        upper = rows[index]
        fraction = (altitude - lower.geometric_altitude_m) / (
            upper.geometric_altitude_m - lower.geometric_altitude_m
        )
    return {
        name: _lerp(getattr(lower, name), getattr(upper, name), fraction)
        for name in _INTERPOLATED_FIELDS
    }


def atmosphere_at(altitude_m: float) -> AtmosphereState:
    """Resolve a standard atmosphere state at geometric AMSL altitude."""
    altitude = float(altitude_m)
    meta = _metadata()
    minimum = float(meta["minimum_altitude_m"])
    maximum = float(meta["maximum_altitude_m"])
    if not math.isfinite(altitude) or not minimum <= altitude <= maximum:
        raise ValueError(
            f"geometric altitude {altitude_m!r} outside [{minimum:g}, {maximum:g}] m"
        )

    rows = _rows()
    altitudes = tuple(row.geometric_altitude_m for row in rows)
    index = bisect.bisect_left(altitudes, altitude)
    if index < len(rows) and altitudes[index] == altitude:
        lower = upper = rows[index]
        fraction = 0.0
    else:
        lower = rows[index - 1]
        upper = rows[index]
        fraction = (altitude - lower.geometric_altitude_m) / (
            upper.geometric_altitude_m - lower.geometric_altitude_m
        )

    values = {
        name: _lerp(getattr(lower, name), getattr(upper, name), fraction)
        for name in _INTERPOLATED_FIELDS
    }
    values["kinematic_viscosity_m2s"] = (
        values["dynamic_viscosity_kgms"] / values["density_kgm3"]
    )
    return AtmosphereState(
        geometric_altitude_m=altitude,
        geopotential_altitude_m=values["geopotential_altitude_m"],
        temperature_k=values["temperature_k"],
        temperature_c=values["temperature_k"] - 273.15,
        pressure_pa=values["pressure_pa"],
        density_kgm3=values["density_kgm3"],
        dynamic_viscosity_kgms=values["dynamic_viscosity_kgms"],
        kinematic_viscosity_m2s=values["kinematic_viscosity_m2s"],
        source=SOURCE_ID,
        dataset_sha256=DATASET_SHA256,
        interpolation=InterpolationProvenance(
            lower_table_altitude_m=lower.geometric_altitude_m,
            upper_table_altitude_m=upper.geometric_altitude_m,
            fraction=fraction,
            exact_row=lower is upper,
        ),
    )


def atmosphere_table_payload() -> dict[str, Any]:
    """Return the canonical rows and policy for the cockpit's JS mirror."""
    meta = dict(_metadata())
    return {
        "source": SOURCE_ID,
        "dataset_sha256": DATASET_SHA256,
        "minimum_altitude_m": float(meta["minimum_altitude_m"]),
        "maximum_altitude_m": float(meta["maximum_altitude_m"]),
        "row_interval_m": float(meta["row_interval_m"]),
        "interpolation": meta["interpolation"],
        "rows": [row.model_dump() for row in _rows()],
    }


def atmosphere_provenance(state: AtmosphereState) -> dict[str, Any]:
    """Compact self-describing fields for stage manifests and registries."""
    return {
        "atmosphere_source": state.source,
        "atmosphere_dataset_sha256": state.dataset_sha256,
        "altitude_m": state.geometric_altitude_m,
        "kinematic_viscosity_m2s": state.kinematic_viscosity_m2s,
    }
