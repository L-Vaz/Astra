"""MDO optimizer (block D) tests.

Needs the aerosandbox + scipy stack; skipped otherwise. The optimize service has no
LLM dependency, so it is loaded directly from its file (bypassing the services package
__init__, which pulls the app's LLM stack) — keeping this test runnable wherever the
aero stack is present, not only in the full FoamAgent env.

Kept deliberately small (few free params, tiny population) so it stays a fast check of
the search *contract*: gate-safe winners, improvement over the base, determinism, and
the reference-relative stability floor.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

pytest.importorskip("aerosandbox", reason="aerosandbox not installed in this env")
pytest.importorskip("scipy", reason="scipy not installed in this env")

from domain_packs.bwb.defaults import derive
from domain_packs.bwb.design_space import DESIGN_FIELDS, DesignSpace
from domain_packs.bwb.schema import BWBParams, MissionSpec


def _load_optimize():
    spec = importlib.util.spec_from_file_location(
        "optimize_service", str(_ROOT / "src" / "services" / "optimize.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


opt = _load_optimize()


@pytest.fixture(scope="module")
def derived():
    return derive(MissionSpec(payload_kg=1.0, cruise_speed_ms=18.0,
                              cruise_altitude_m=300.0, launch="hand"))


@pytest.fixture(scope="module")
def small_space(derived):
    free = ("sweep_outer_deg", "tip_twist_deg")
    frozen = [f for f in DESIGN_FIELDS if f not in free]
    return DesignSpace.from_base(derived.params, spread=0.2, frozen=frozen)


@pytest.fixture(scope="module")
def result(derived, small_space):
    return opt.optimize(derived, space=small_space, seed=11, maxiter=2, popsize=4,
                        polish=False, top_n=3)


def test_winner_passes_geometry_gate(result):
    assert result.winner is not None
    BWBParams.model_validate(result.winner.params)  # raises if out of gate
    assert result.n_feasible > 0


def test_winner_improves_on_base(result):
    assert result.winner.score >= result.reference["base_score"] - 1e-6


def test_frozen_params_unchanged(result, derived, small_space):
    wp = BWBParams.model_validate(result.winner.params)
    for n in DESIGN_FIELDS:
        if n not in small_space.free_names():
            assert getattr(wp, n) == pytest.approx(getattr(derived.params, n))


def test_top_n_distinct_and_feasible(result):
    keys = [opt._round_key(w.params) for w in result.top]
    assert len(keys) == len(set(keys))
    for w in result.top:
        assert w.score > -1e5  # all feasible (infeasible never enters `seen`)


def test_reference_latdir_floor_from_base(result, derived):
    # floor equals the base wing's Cn_beta and is enforced
    from domain_packs.bwb.asb_aero import evaluate
    base_cnb = evaluate(derived.params, derived).cn_beta_per_rad
    assert result.reference["cn_beta_floor"] == pytest.approx(base_cnb, rel=1e-6)
    assert result.reference["enforce_latdir_floor"] is True


def test_determinism_same_seed(derived, small_space):
    a = opt.optimize(derived, space=small_space, seed=11, maxiter=2, popsize=4,
                     polish=False, top_n=3)
    b = opt.optimize(derived, space=small_space, seed=11, maxiter=2, popsize=4,
                     polish=False, top_n=3)
    assert a.winner.score == pytest.approx(b.winner.score, rel=1e-9)
    assert opt._round_key(a.winner.params) == opt._round_key(b.winner.params)
