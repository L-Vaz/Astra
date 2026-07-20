import pytest

from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.defaults import derive, isa_density
from domain_packs.bwb.schema import MissionDerived, MissionSpec


MISSION = MissionSpec(
    payload_kg=1.5,
    cruise_speed_ms=25.0,
    cruise_altitude_m=1275.0,
    launch="hand",
)


def test_derive_carries_the_resolved_atmosphere():
    derived = derive(MISSION)

    assert derived.atmosphere.geometric_altitude_m == 1275.0
    assert derived.air_density_kgm3 == derived.atmosphere.density_kgm3
    assert derived.reynolds_mac == pytest.approx(
        MISSION.cruise_speed_ms
        * derived.params.mean_aero_chord_m()
        / derived.atmosphere.kinematic_viscosity_m2s,
        rel=1e-13,
    )


def test_legacy_mission_derived_reconstructs_missing_atmosphere():
    current = derive(MISSION)
    legacy = current.model_dump(exclude={"atmosphere"})

    loaded = MissionDerived.model_validate(legacy)

    assert loaded.atmosphere == atmosphere_at(MISSION.cruise_altitude_m)
    assert loaded.air_density_kgm3 == legacy["air_density_kgm3"]


def test_mission_rejects_atmosphere_from_different_altitude():
    current = derive(MISSION)
    corrupt = current.model_dump()
    corrupt["atmosphere"] = atmosphere_at(2000.0).model_dump()

    with pytest.raises(ValueError, match="mission altitude"):
        MissionDerived.model_validate(corrupt)


def test_isa_density_compatibility_wrapper_uses_the_table():
    assert isa_density(1275.0) == atmosphere_at(1275.0).density_kgm3
