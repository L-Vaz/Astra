"""Objective-preset tests — pure. The 3-priority -> 9-weight mapping and proposal."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from domain_packs.bwb.objective import ObjectiveWeights
from domain_packs.bwb.objective_presets import (NAMED_PRESETS, priorities_for,
                                                propose, weights_from_priorities)


def test_balanced_weights_are_sane():
    w = weights_from_priorities(0.5, 0.5, 0.5)
    assert isinstance(w, ObjectiveWeights)
    assert w.w6_latdir > 0 and w.w7_pitch > 0 and w.w9_geometry > 0
    # structural weights stay dormant
    assert w.w1_m_root == 0 and w.w5_shape_3d == 0


def test_stability_raises_stability_penalties():
    lo = weights_from_priorities(0.5, 0.1, 0.5)
    hi = weights_from_priorities(0.5, 0.9, 0.5)
    assert hi.w6_latdir > lo.w6_latdir
    assert hi.w7_pitch > lo.w7_pitch


def test_buildable_raises_geometry_penalty():
    lo = weights_from_priorities(0.5, 0.5, 0.1)
    hi = weights_from_priorities(0.5, 0.5, 0.9)
    assert hi.w9_geometry > lo.w9_geometry


def test_efficiency_softens_penalties():
    lo_eff = weights_from_priorities(0.1, 0.5, 0.5)
    hi_eff = weights_from_priorities(0.9, 0.5, 0.5)
    # more efficiency emphasis -> smaller penalty weights (L/D dominates)
    assert hi_eff.w6_latdir < lo_eff.w6_latdir
    assert hi_eff.w9_geometry < lo_eff.w9_geometry


def test_priorities_clamped():
    w = weights_from_priorities(5.0, -3.0, 0.5)  # out of range
    ref = weights_from_priorities(1.0, 0.0, 0.5)
    assert w.model_dump() == ref.model_dump()


def test_all_named_presets_valid():
    for key in NAMED_PRESETS:
        pr = priorities_for(key)
        w = weights_from_priorities(**pr)
        assert isinstance(w, ObjectiveWeights)


def test_propose_payload_shape():
    p = propose()
    assert p["default_preset"] in NAMED_PRESETS
    assert {pp["key"] for pp in p["presets"]} == set(NAMED_PRESETS)
    assert 0 < p["spread"] <= 0.5
    # weights round-trip into the model
    ObjectiveWeights(**p["weights"])
    assert any(q["key"] == "freeze" for q in p["questions"])
