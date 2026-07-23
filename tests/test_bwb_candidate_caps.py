"""Vehicle constraints (span/stall caps) must reach candidate generation."""
import pytest

from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.inverse import solve_requirements
from domain_packs.bwb.requirements import DesignRequirements
from domain_packs.bwb.schema import MissionDerived, MissionSpec
from domain_packs.bwb.variants import generate_candidates


def _derived(req: DesignRequirements) -> MissionDerived:
    """MissionDerived the way the stage-1 generate endpoint builds it: from
    the same inverse solve the intake feasibility ran."""
    res = solve_requirements(req)
    atmo = atmosphere_at(req.cruise_altitude_m)
    mission = MissionSpec(payload_kg=req.mtow_kg * 0.3,
                          cruise_speed_ms=req.cruise_speed_ms,
                          cruise_altitude_m=req.cruise_altitude_m,
                          mtow_kg=req.mtow_kg, launch=req.launch)
    return MissionDerived(
        mission=mission, atmosphere=atmo, mtow_kg=req.mtow_kg,
        air_density_kgm3=atmo.density_kgm3, weight_n=req.mtow_kg * 9.81,
        wing_loading_kgm2=res.panel.wing_loading_kgm2,
        s_ref_target_m2=res.panel.s_ref_m2,
        required_cl=res.panel.cl_cruise,
        reynolds_mac=(req.cruise_speed_ms * res.params.mean_aero_chord_m()
                      / atmo.kinematic_viscosity_m2s),
        params=res.params, assumptions=[])


def test_span_cap_reaches_every_candidate():
    req = DesignRequirements(mtow_kg=2.0, cruise_speed_ms=18.0,
                             cruise_altitude_m=100.0, launch="hand",
                             span_max_m=1.2)
    cands = generate_candidates(_derived(req), req)
    assert len(cands) >= 5
    for c in cands:
        assert c.params.span_m <= 1.2 + 1e-6, \
            f"candidate '{c.key}' span {c.params.span_m:.3f} exceeds the 1.2 m cap"


def test_without_requirements_schema_cap_applies():
    req = DesignRequirements(mtow_kg=2.0, cruise_speed_ms=18.0,
                             cruise_altitude_m=100.0, launch="hand")
    cands = generate_candidates(_derived(req), None)
    for c in cands:
        assert c.params.span_m <= 3.0 + 1e-6


def test_tighter_stall_cap_grows_candidate_wings():
    base_req = DesignRequirements(mtow_kg=2.0, cruise_speed_ms=18.0,
                                  cruise_altitude_m=100.0, launch="hand")
    tight_req = base_req.model_copy(update={"v_stall_max_ms": 8.0})
    base = {c.key: c for c in generate_candidates(_derived(base_req), base_req)}
    tight = {c.key: c for c in generate_candidates(_derived(tight_req), tight_req)}
    # every CL*-sized lens must grow (or hold) its wing under the tighter cap
    grew = 0
    for key, c in tight.items():
        if key == "baseline":
            continue
        assert c.params.planform_area_m2() >= \
            base[key].params.planform_area_m2() - 1e-9
        if c.params.planform_area_m2() > base[key].params.planform_area_m2() + 1e-6:
            grew += 1
    assert grew >= 1, "a 8 m/s stall cap should bind at least one lens"
    for c in tight.values():
        assert c.panel.v_stall_ms <= 8.0 + 0.3  # small tolerance: clmax refines


def test_user_stall_note_names_the_requirement():
    req = DesignRequirements(mtow_kg=2.0, cruise_speed_ms=18.0,
                             cruise_altitude_m=100.0, launch="hand",
                             v_stall_max_ms=8.0)
    cands = generate_candidates(_derived(req), req)
    notes = " ".join(n for c in cands for n in c.constraint_notes)
    assert "stall" in notes.lower()
    assert "8" in notes
