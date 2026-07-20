import math
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from domain_packs.bwb.atmosphere import (
    DATASET_SHA256,
    SOURCE_ID,
    atmosphere_at,
    atmosphere_provenance,
    atmosphere_table_payload,
)


def test_atmosphere_dataset_is_in_distribution_package_data():
    repository = Path(__file__).parent.parent
    config = tomllib.loads((repository / "pyproject.toml").read_text())

    includes = config["tool"]["setuptools"]["packages"]["find"]["include"]
    package_data = config["tool"]["setuptools"]["package-data"]
    assert "domain_packs" in includes
    assert "domain_packs.*" in includes
    assert "data/*.csv" in package_data["domain_packs.bwb"]
    assert "data/*.json" in package_data["domain_packs.bwb"]
    assert "static/*.js" in package_data["src.cockpit"]
    assert "static/*.css" in package_data["src.cockpit"]
    assert "static/*.html" in package_data["src.cockpit"]


def test_serialized_atmosphere_rejects_corrupt_physics_and_provenance():
    state = atmosphere_at(1275.0)
    corrupt_nu = state.model_dump()
    corrupt_nu["kinematic_viscosity_m2s"] *= 2
    with pytest.raises(ValidationError, match="kinematic viscosity"):
        type(state).model_validate(corrupt_nu)

    corrupt_source = state.model_dump()
    corrupt_source["dataset_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="checksum"):
        type(state).model_validate(corrupt_source)


def test_manifest_provenance_summary_is_self_describing():
    state = atmosphere_at(1275.0)

    assert atmosphere_provenance(state) == {
        "atmosphere_source": state.source,
        "atmosphere_dataset_sha256": state.dataset_sha256,
        "altitude_m": state.geometric_altitude_m,
        "kinematic_viscosity_m2s": state.kinematic_viscosity_m2s,
    }


@pytest.mark.parametrize(
    ("altitude_m", "temperature_k", "pressure_pa", "density", "mu", "nu"),
    [
        (0.0, 288.15, 101325.0, 1.2249991559, 1.7893802781e-5, 1.4607196009e-5),
        (1000.0, 281.6510224, 89876.2852, 1.1116589851, 1.7578504776e-5, 1.5812857191e-5),
        (2000.0, 275.1540888, 79501.4246, 1.0065532170, 1.7259816220e-5, 1.7147445290e-5),
        (4000.0, 262.1663502, 61660.4441, 0.8193463087, 1.6611900406e-5, 2.0274577710e-5),
    ],
)
def test_standard_atmosphere_checkpoints(
    altitude_m, temperature_k, pressure_pa, density, mu, nu
):
    state = atmosphere_at(altitude_m)

    assert state.geometric_altitude_m == altitude_m
    assert state.temperature_k == pytest.approx(temperature_k, rel=2e-10)
    assert state.pressure_pa == pytest.approx(pressure_pa, rel=2e-9)
    assert state.density_kgm3 == pytest.approx(density, rel=2e-9)
    assert state.dynamic_viscosity_kgms == pytest.approx(mu, rel=2e-9)
    assert state.kinematic_viscosity_m2s == pytest.approx(nu, rel=2e-9)
    assert state.kinematic_viscosity_m2s == pytest.approx(
        state.dynamic_viscosity_kgms / state.density_kgm3, rel=1e-13
    )
    assert state.source == SOURCE_ID
    assert state.dataset_sha256 == DATASET_SHA256
    assert state.interpolation.exact_row is True
    assert state.interpolation.fraction == 0.0


def test_interpolation_uses_surrounding_rows_and_derives_kinematic_viscosity():
    state = atmosphere_at(1275.0)
    lower = atmosphere_at(1200.0)
    upper = atmosphere_at(1300.0)

    assert state.interpolation.lower_table_altitude_m == 1200.0
    assert state.interpolation.upper_table_altitude_m == 1300.0
    assert state.interpolation.fraction == pytest.approx(0.75)
    assert state.interpolation.exact_row is False
    assert state.temperature_k == pytest.approx(
        lower.temperature_k + 0.75 * (upper.temperature_k - lower.temperature_k)
    )
    assert state.dynamic_viscosity_kgms == pytest.approx(
        lower.dynamic_viscosity_kgms
        + 0.75 * (upper.dynamic_viscosity_kgms - lower.dynamic_viscosity_kgms)
    )
    assert state.kinematic_viscosity_m2s == pytest.approx(
        state.dynamic_viscosity_kgms / state.density_kgm3, rel=1e-13
    )


@pytest.mark.parametrize("altitude_m", [-1.0, 4000.01, math.nan, math.inf, -math.inf])
def test_invalid_altitudes_are_rejected(altitude_m):
    with pytest.raises(ValueError, match="outside"):
        atmosphere_at(altitude_m)


def test_table_payload_is_complete_and_checksum_identified():
    payload = atmosphere_table_payload()

    assert payload["source"] == SOURCE_ID
    assert payload["dataset_sha256"] == DATASET_SHA256
    assert payload["minimum_altitude_m"] == 0.0
    assert payload["maximum_altitude_m"] == 4000.0
    assert payload["row_interval_m"] == 100.0
    assert len(payload["rows"]) == 41
    assert payload["rows"][0]["geometric_altitude_m"] == 0.0
    assert payload["rows"][-1]["geometric_altitude_m"] == 4000.0
