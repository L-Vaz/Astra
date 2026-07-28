"""MDO optimiser service (Slice 2, block D).

Differential-evolution search over a `DesignSpace`, scored by the deterministic MDO
objective (block C) with each candidate evaluated by the AeroSandbox fast tier
(block A). Returns the best design plus a ranked list of distinct feasible runners-up
— the winners the caller writes into the candidate funnel for VSPAERO/CFD confirmation.

Determinism: with a fixed `seed`, scipy's differential_evolution reproduces the same
winner (Latin-hypercube init + seeded mutation). The winner passes the geometry gate
by construction (it is a validated BWBParams).

Reference-relative lateral-directional floor: the base wing is evaluated first and its
Cn_beta becomes the hard floor, so no design is allowed to be less directionally
stable than the reference (the "at least as stable as my reference wing" rule).

Pure Python / in-process — no OpenVSP subprocess. aerosandbox is imported via the
domain-pack evaluator only when a search actually runs.
"""
import os
import sys
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from domain_packs.bwb import objective as obj  # noqa: E402
from domain_packs.bwb.asb_aero import AsbAeroError, evaluate  # noqa: E402
from domain_packs.bwb.design_space import DesignSpace  # noqa: E402
from domain_packs.bwb.schema import BWBParams, MissionDerived  # noqa: E402

# DE minimises; an infeasible / ungateable design costs this much (>> any real -score).
_BIG = 1.0e6


class MdoWinner(BaseModel):
    rank: int
    score: float
    l_over_d: float
    static_margin: float
    params: dict
    mdo: dict = Field(description="score breakdown + key aero, for candidate evidence")


class OptimizeResult(BaseModel):
    winner: Optional[MdoWinner]
    top: List[MdoWinner]
    n_evals: int
    n_feasible: int
    seed: int
    converged: bool
    message: str
    reference: dict = Field(description="base-wing score + the Cn_beta floor used")
    weights: dict
    config: dict


def _round_key(params: dict, ndigits: int = 4) -> tuple:
    """Dedup key for a candidate geometry.

    S1-C: `params` now contains three aerofoil NAMES alongside the numbers, so the
    key keeps strings as strings instead of forcing them through float(). Dropping
    them instead would make two designs with identical planforms but different
    sections look like duplicates."""
    out = []
    for k in sorted(params):
        v = params[k]
        out.append(v if isinstance(v, str) else round(float(v), ndigits))
    return tuple(out)


def optimize(
    derived: MissionDerived,
    space: Optional[DesignSpace] = None,
    weights: Optional[obj.ObjectiveWeights] = None,
    config: Optional[obj.ObjectiveConfig] = None,
    cg_frac_mac: float = 0.25,
    spread: float = 0.15,
    seed: int = 0,
    maxiter: int = 40,
    popsize: int = 15,
    tol: float = 1e-3,
    top_n: int = 5,
    polish: bool = True,
    reference_latdir_floor: bool = True,
    on_generation: Optional[Callable[[int, float], None]] = None,
) -> OptimizeResult:
    """Search around the mission's base wing (derived.params). Returns ranked winners."""
    from scipy.optimize import differential_evolution

    base = derived.params
    space = space or DesignSpace.from_base(base, spread=spread)
    weights = weights or obj.ObjectiveWeights()
    config = config or obj.config_from_derived(derived)

    # base wing sets the reference-relative directional-stability floor
    base_aero = evaluate(base, derived, cg_frac_mac=cg_frac_mac)
    if reference_latdir_floor:
        config = config.model_copy(update={
            "cn_beta_floor": base_aero.cn_beta_per_rad,
            "enforce_latdir_floor": True})
    base_score = obj.score(base, base_aero, weights, config)

    free = space.free_names()
    bounds = space.de_bounds()
    seen: dict = {}
    stats = {"n_evals": 0, "n_feasible": 0, "best": float("-inf")}

    def objective_fn(x) -> float:
        stats["n_evals"] += 1
        try:
            params = space.vector_to_params(x)
        except Exception:
            return _BIG  # out of geometry gate -> infeasible
        try:
            aero = evaluate(params, derived, cg_frac_mac=cg_frac_mac)
        except AsbAeroError:
            return _BIG
        bd = obj.score(params, aero, weights, config)
        if not bd.feasible:
            return _BIG
        stats["n_feasible"] += 1
        if bd.score > stats["best"]:
            stats["best"] = bd.score
        key = _round_key(params.model_dump())
        if key not in seen or bd.score > seen[key]["score"]:
            seen[key] = {
                "score": bd.score,
                "l_over_d": aero.l_over_d,
                "static_margin": bd.static_margin,
                "params": params.model_dump(),
                "mdo": {
                    "score": bd.score,
                    "aero_efficiency": bd.aero_efficiency,
                    "total_penalty": bd.total_penalty,
                    "penalties": {k: v.model_dump() for k, v in bd.penalties.items()},
                    "l_over_d": aero.l_over_d,
                    "static_margin": bd.static_margin,
                    "alpha_trim_deg": aero.alpha_trim_deg,
                    "cn_beta_per_rad": aero.cn_beta_per_rad,
                    "cm_alpha_per_rad": aero.cm_alpha_per_rad,
                    "evidence": aero.evidence,
                },
            }
        return -bd.score  # maximise score

    cb = None
    if on_generation is not None:
        state = {"gen": 0}

        def cb(xk, convergence):  # scipy DE callback signature
            state["gen"] += 1
            # current top-N feasible designs, for a live gallery (no extra aero eval)
            top = sorted(seen.values(), key=lambda d: d["score"], reverse=True)[:top_n]
            tiles = [{"params": d["params"], "score": d["score"],
                      "l_over_d": d["l_over_d"], "static_margin": d["static_margin"]}
                     for d in top]
            on_generation(state["gen"], stats["best"], stats["n_evals"], tiles)

    result = differential_evolution(
        objective_fn, bounds, seed=seed, maxiter=maxiter, popsize=popsize,
        tol=tol, polish=polish, init="latinhypercube", callback=cb)

    ranked = sorted(seen.values(), key=lambda d: d["score"], reverse=True)[:top_n]
    winners = [
        MdoWinner(rank=i + 1, score=d["score"], l_over_d=d["l_over_d"],
                  static_margin=d["static_margin"], params=d["params"], mdo=d["mdo"])
        for i, d in enumerate(ranked)
    ]

    return OptimizeResult(
        winner=winners[0] if winners else None,
        top=winners,
        n_evals=stats["n_evals"],
        n_feasible=stats["n_feasible"],
        seed=seed,
        converged=bool(result.success),
        message=str(result.message),
        reference={
            "base_score": base_score.score,
            "base_feasible": base_score.feasible,
            "base_l_over_d": base_aero.l_over_d,
            "cn_beta_floor": config.cn_beta_floor,
            "enforce_latdir_floor": config.enforce_latdir_floor,
        },
        weights=weights.model_dump(),
        config=config.model_dump(),
    )
