"""Vehicle constraints (span/stall caps) must reach candidate generation."""
import pytest

from domain_packs.bwb.aero_estimates import LAUNCH_VSTALL_MAX
from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.inverse import solve_requirements
from domain_packs.bwb.requirements import DesignRequirements
from domain_packs.bwb.schema import MissionDerived, MissionSpec
from domain_packs.bwb.variants import generate_candidates as _generate_candidates


AIRFOIL_APPROVAL = {"schedule": {
    "root": "e474", "mid": "e472", "tip": "s5010"}}


def generate_candidates(derived, requirements=None):
    return _generate_candidates(derived, AIRFOIL_APPROVAL, requirements)


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
    """A tighter stall cap must not SHRINK any wing.

    NOTE: this proves the area moves in the right direction. It does NOT prove the
    resulting design meets the cap — see the parametrized adherence test below. The
    original version of this test also asserted `v_stall_ms <= 8.3` here, which passed
    only because (mtow 2 kg, default 3.0 m span) is a benign combination; a solver fuzz
    over 648,000 legal requirement sets later showed the cap is breached in 33.1% of
    the envelope. That assertion was false confidence and has been moved, honestly
    marked, into test_every_candidate_honours_the_effective_stall_cap.
    """
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


# Measured violating cases from the solver fuzz (verification/fuzz_inverse_solver.py,
# seed 20260725) and the API probe. Each is a requirement set a user can legitimately
# express, for which every generated concept breaches the effective stall cap.
_STALL_CAP_CASES = [
    pytest.param(dict(mtow_kg=20.0, launch="hand", span_max_m=0.9),
                 id="heavy-tight-span"),           # measured 29.3-30.2 vs 10.5
    pytest.param(dict(mtow_kg=25.0, launch="hand", v_stall_max_ms=5.0),
                 id="heavy-tight-stall"),          # measured 13.4-14.4 vs 5.0
    pytest.param(dict(mtow_kg=2.0, launch="hand", span_max_m=0.8),
                 id="light-min-span"),             # measured 1 of 5 violating
    pytest.param(dict(mtow_kg=0.5, launch="hand", span_max_m=0.8,
                      v_stall_max_ms=5.0),
                 id="fuzz-minimal-reproducer"),    # measured 5.24 vs 5.00
]


@pytest.mark.parametrize("kw", _STALL_CAP_CASES)
def test_every_candidate_honours_the_effective_stall_cap(kw):
    """The cap the user sees is min(their stall row, the launch method's tolerance).

    NO THIRD OUTCOME (S4-C1): every candidate either meets that cap or carries a
    `violations` entry naming stall. What it may never do — and did, in 11 of 13
    probed scenarios — is breach the cap while reporting `passed: True`.

    This test was written in Stage 0 as `xfail(strict=True)` asserting that Stage 4
    would make every candidate MEET the cap. That expectation was wrong, and measuring
    it is what showed why: at a 0.9 m span cap with 20 kg MTOW the stall cap needs
    2.569 m^2 of wing, while the largest wing the schema permits at that span is
    0.324 m^2 — a factor of eight. Three of these four scenarios are unreachable by
    any legal geometry, so "satisfy the cap" is not a contract the code can keep.
    "Satisfy it or say you cannot" is, and it is the one the plan's own hypothesis
    property specifies.
    """
    req = DesignRequirements(cruise_speed_ms=18.0, cruise_altitude_m=100.0, **kw)
    cap = min(req.v_stall_max_ms or float("inf"), LAUNCH_VSTALL_MAX[req.launch])
    for c in generate_candidates(_derived(req), req):
        if c.panel.v_stall_ms > cap + 0.05:
            stall = [v for v in c.violations if v.cap == "stall"]
            assert stall, (
                f"candidate '{c.key}' stalls at {c.panel.v_stall_ms:.2f} m/s against "
                f"a {cap} m/s cap and reports no violation")
            assert stall[0].achieved == pytest.approx(c.panel.v_stall_ms, abs=1e-6)
            assert stall[0].required == pytest.approx(cap)
        else:
            assert not [v for v in c.violations if v.cap == "stall"], \
                f"candidate '{c.key}' meets the cap but reports a stall violation"


@pytest.mark.parametrize("kw", _STALL_CAP_CASES)
def test_a_breaching_candidate_never_reports_passed(kw):
    """The flag the gallery reads. `evidence.equation.passed` was unconditionally
    True (S4-C4)."""
    req = DesignRequirements(cruise_speed_ms=18.0, cruise_altitude_m=100.0, **kw)
    for c in generate_candidates(_derived(req), req):
        cap = min(req.v_stall_max_ms or float("inf"), LAUNCH_VSTALL_MAX[req.launch])
        if c.panel.v_stall_ms > cap + 0.05:
            assert c.violations, f"'{c.key}' breaches its cap with no violation"


def test_notes_do_not_contradict_each_other():
    """The candidates used to narrate the contradiction themselves: "Wing grown to
    0.419 m^2 to honour stall <= 10.5 m/s" directly followed by "Wing area capped at
    0.400 m^2 by the AR gate". Whichever way that conflict resolves, it must not be
    reported as two facts."""
    for kw in (dict(mtow_kg=20.0, launch="hand", span_max_m=0.9),
               dict(mtow_kg=25.0, launch="hand", v_stall_max_ms=5.0),
               dict(mtow_kg=2.0, launch="hand", span_max_m=0.8)):
        req = DesignRequirements(cruise_speed_ms=18.0, cruise_altitude_m=100.0, **kw)
        for c in generate_candidates(_derived(req), req):
            notes = " | ".join(c.constraint_notes)
            grown = "to honour stall" in notes
            capped = "capped at" in notes and "m^2" in notes
            if grown and capped:
                assert "CANNOT MEET" in notes, (
                    f"'{c.key}' claims both that the wing grew for the stall cap and "
                    f"that its area was capped, with no resolution: {notes}")


def test_a_reachable_cap_is_actually_reached():
    """Guard the guard: "report the violation" must not become an excuse for never
    meeting a cap. 2 kg at the default 3.0 m span has room to spare."""
    req = DesignRequirements(mtow_kg=2.0, cruise_speed_ms=18.0,
                             cruise_altitude_m=100.0, launch="hand",
                             v_stall_max_ms=9.0)
    for c in generate_candidates(_derived(req), req):
        assert c.panel.v_stall_ms <= 9.0 + 0.05, \
            f"'{c.key}' stalls at {c.panel.v_stall_ms:.2f} with room to grow"
        assert not c.violations, c.violations


def test_user_stall_note_names_the_requirement():
    req = DesignRequirements(mtow_kg=2.0, cruise_speed_ms=18.0,
                             cruise_altitude_m=100.0, launch="hand",
                             v_stall_max_ms=8.0)
    cands = generate_candidates(_derived(req), req)
    notes = " ".join(n for c in cands for n in c.constraint_notes)
    assert "stall" in notes.lower()
    assert "8" in notes
