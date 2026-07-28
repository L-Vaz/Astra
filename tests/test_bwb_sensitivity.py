"""Sensitivity-table tests: physical sign checks and bound handling."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.defaults import derive
from domain_packs.bwb.requirements import DesignRequirements
from domain_packs.bwb.schema import BWBParams, MissionSpec
from domain_packs.bwb.sensitivity import (HUMAN_STEPS,
                                          requirement_sensitivity_table,
                                          sensitivity_table)

CTX = dict(atmosphere=atmosphere_at(100.0), v=25.0,
           weight_n=5.0 * 9.81, launch="hand")


@pytest.fixture(scope="module")
def params():
    return derive(MissionSpec(payload_kg=1.5, cruise_speed_ms=25.0,
                              cruise_altitude_m=100.0, launch="hand")).params


def _row(rows, param):
    return next(r for r in rows if r.param == param)


def test_sign_checks(params):
    rows = sensitivity_table(params, **CTX)
    # span up at fixed chord -> more area AND more AR; AR must rise
    assert _row(rows, "span_m").deltas["aspect_ratio"] > 0
    assert _row(rows, "span_m").deltas["s_ref_m2"] > 0
    assert _row(rows, "sweep_outer_deg").deltas["cl_max_3d"] < 0  # sweep up -> CLmax down
    assert _row(rows, "sweep_outer_deg").deltas["v_stall_ms"] > 0
    assert _row(rows, "sweep_outer_deg").deltas["x_np_m"] > 0     # sweep up -> np aft
    # S1-C: the "thicker -> more drag" row is gone. Thickness is set by the section,
    # so there is no continuous knob to perturb — nudging t/c by a notch would mean
    # silently swapping the aerofoil. Section choice is a discrete variable and is
    # not part of the sensitivity table.
    assert "root_thickness_pct" not in {r.param for r in rows}
    assert _row(rows, "root_chord_m").deltas["s_ref_m2"] > 0


def test_all_params_covered(params):
    rows = sensitivity_table(params, **CTX)
    assert {r.param for r in rows} == set(HUMAN_STEPS)


def test_clamped_at_bound():
    # dihedral at its 0-deg lower bound: central diff must degrade one-sided
    base = derive(MissionSpec(payload_kg=1.5, cruise_speed_ms=25.0,
                              cruise_altitude_m=100.0, launch="hand")).params
    p = BWBParams.model_validate({**base.model_dump(), "dihedral_deg": 0.0})
    rows = sensitivity_table(p, **CTX)
    assert _row(rows, "dihedral_deg").clamped is True


def test_requirement_sensitivities(params):
    req = DesignRequirements(mtow_kg=5.0, cruise_speed_ms=25.0,
                             cruise_altitude_m=100.0, launch="hand")
    rows = requirement_sensitivity_table(req, params)
    speed = _row(rows, "cruise_speed_ms")
    assert speed.deltas["cl_cruise"] < 0    # faster -> less CL needed
    mtow = _row(rows, "mtow_kg")
    assert mtow.deltas["v_stall_ms"] > 0    # heavier -> stalls faster
    altitude = _row(rows, "cruise_altitude_m")
    assert altitude.clamped is True          # 100 - 125 m would be out of range
