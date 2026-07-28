"""The Stage-1 -> Stage-2 handoff artifact (S1-I).

The artifact's job is not to carry numbers — `model_dump()` does that. It is to carry
how far each number can be trusted, so that an optimiser inheriting them does not
anchor on the ones this project has explicitly declined to believe. So these tests are
mostly about the evidence block being complete and honest rather than merely present.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from domain_packs.bwb.handoff import (KNOWN_DISAGREEMENTS, SCHEMA_VERSION,
                                      Stage1Handoff, build_handoff)
from domain_packs.bwb.inverse import LockedValues, solve_requirements
from domain_packs.bwb.requirements import DesignRequirements


def _solved(locked=None):
    req = DesignRequirements(mtow_kg=3.3, cruise_speed_ms=18.0, launch="hand")
    res = solve_requirements(req, locked=locked)
    return req, res


def _handoff(locked=None) -> Stage1Handoff:
    req, res = _solved(locked)
    return build_handoff(res.params, res.panel, req, res.requirements,
                         locked, res.lock_violations,
                         altitude_m=req.cruise_altitude_m)


def test_it_round_trips_through_json():
    """The transport is JSON and Stage 2 does not exist yet, so the only thing that can
    be checked today is that nothing is lost on the way out and back."""
    h = _handoff()
    back = Stage1Handoff.model_validate_json(h.model_dump_json())
    assert back.model_dump() == h.model_dump()
    assert back.schema_version == SCHEMA_VERSION


def test_every_evidence_row_carries_a_real_grade_and_a_source():
    h = _handoff()
    assert h.evidence
    for e in h.evidence:
        assert e.grade in {"A", "B", "C", "D"}, e
        assert e.source.strip(), f"{e.quantity} has a grade with no source"
        assert len(e.source) > 20, f"{e.quantity}: a source must be locatable, not a word"


def test_every_judgment_grade_carries_a_caveat():
    """The protocol's rule: [D] is not a fact. A [D] with no caveat is a bare number
    wearing a grade, which is worse than no grade at all."""
    for e in _handoff().evidence:
        if e.grade == "D":
            assert e.caveat.strip(), f"{e.quantity} is [D] with nothing said about it"
            assert len(e.caveat) > 40, f"{e.quantity}: caveat too thin to act on"


def test_the_numbers_the_polar_actually_uses_are_graded_D():
    """The uncomfortable one, and the reason the block exists.

    `cd0` and `oswald_e` are what `cd_cruise` — and therefore every L/D on every card —
    is built from, and both are unsourced correlations. Stage 1 has better-evidenced
    alternatives for both and deliberately did not swap them in. If the handoff let
    those two through ungraded, Stage 2 would inherit the project's weakest numbers as
    though they were its strongest."""
    grades = {e.quantity: e.grade for e in _handoff().evidence}
    assert grades["cd0"] == "D"
    assert grades["oswald_e"] == "D"
    assert grades["x_np_m"] == "D"


def test_the_sourced_quantities_are_graded_A_or_B():
    grades = {e.quantity: e.grade for e in _handoff().evidence}
    assert grades["cl_max_3d"] == "A"
    assert grades["stall_station_frac"] == "A"
    assert grades["cm_trim_residual"] == "A"
    assert grades["effective_dihedral_deg"] == "B"
    assert grades["span_efficiency_planar"] == "B"


def test_evidence_values_match_the_panel_they_came_from():
    """A grade attached to a stale number is a lie with a citation."""
    req, res = _solved()
    h = build_handoff(res.params, res.panel, req, res.requirements)
    by_name = {e.quantity: e.value for e in h.evidence}
    for name in by_name:
        assert by_name[name] == pytest.approx(getattr(res.panel, name)), name


def test_the_known_disagreements_are_carried():
    """Three places where two models in this pack give different answers and Stage 1
    declined to pick. Each is a trap for an optimiser that anchors on whichever it
    reads first, so each has to travel with the artifact."""
    h = _handoff()
    assert len(h.known_disagreements) == len(KNOWN_DISAGREEMENTS) == 3
    joined = " ".join(h.known_disagreements).lower()
    assert "profile drag" in joined
    assert "span efficiency" in joined
    assert "static margin" in joined


def test_provenance_pins_the_data_the_numbers_came_from():
    """A handoff that cannot say which airfoil table produced its CLmax cannot be
    reproduced, and an irreproducible artifact is a rumour."""
    prov = _handoff().provenance
    assert len(prov["airfoil_table_sha256"]) == 64
    assert prov["airfoil_table"]["sections"] > 2000
    assert prov["airfoil_table"]["cl_ladder_interpolation_error_pct"]["median"] > 0
    assert prov["atmosphere"]["atmosphere_source"]
    assert "TM-948" in prov["methods"]["spanwise_load"]
    # the methods block must say which models are IN the polar and which are only reported
    assert "IN the polar" in prov["methods"]["parasite_drag"]
    assert "not in the polar" in prov["methods"]["span_efficiency"]


def test_locks_travel_as_hard_constraints():
    """Stage 2 inherits the operator's constraints, not Stage 1's interpretation."""
    h = _handoff(LockedValues(span_m=1.5))
    assert h.locks.span_m == 1.5
    assert h.geometry.span_m == pytest.approx(1.5, abs=1e-3)
    assert not h.lock_violations


def test_an_unhonoured_lock_travels_as_a_conflict_not_a_resolution():
    """A 2.5 m span lock cannot be held against the AR gate. Stage 2 must inherit the
    conflict — silently shipping the compromised geometry with a clean lock field would
    tell the optimiser the constraint was satisfied."""
    h = _handoff(LockedValues(span_m=2.5))
    assert h.locks.span_m == 2.5
    assert h.lock_violations
    assert h.geometry.span_m < 2.5
    assert any("aspect ratio" in v.blocked_by for v in h.lock_violations)


def test_unmet_requirements_are_carried_not_hidden():
    """An unmet target is Stage 2's objective. Dropping it would hand over an aircraft
    that looks finished."""
    req = DesignRequirements(mtow_kg=3.3, cruise_speed_ms=18.0, launch="hand",
                             ld_min=40.0)   # the schema ceiling, unreachable for this wing
    res = solve_requirements(req)
    h = build_handoff(res.params, res.panel, req, res.requirements)
    assert any(s.met is False for s in h.requirement_status)


def test_the_flight_condition_is_recorded_with_the_performance():
    """Every performance number is point data. Without the point they are decoration."""
    h = _handoff()
    fc = h.flight_condition
    assert fc["cruise_speed_ms"] > 0 and fc["weight_n"] > 0
    assert fc["launch"] in ("hand", "catapult", "runway")
    assert fc["reynolds_mac"] == pytest.approx(h.performance["reynolds_mac"])
    assert fc["cruise_speed_ms"] == pytest.approx(h.performance["v_ms"])
