"""AeroSandbox fast aero+stability service — the MDO screening tier.

Thin, session-aware wrapper around the domain-pack evaluator
(`domain_packs/bwb/asb_aero.py`) and objective (`domain_packs/bwb/objective.py`).
Shaped like `vspaero.py`, but pure Python and in-process: no OpenVSP subprocess.

Scoring is a read-only screen — it does NOT create a pipeline stage or invalidate
mesh/CFD (see design_session.STAGES). Callers that want a persistent record pass an
`outdir`; the artifact is written there without any stage bookkeeping.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from domain_packs.bwb import objective as obj  # noqa: E402
from domain_packs.bwb.asb_aero import AsbAeroError, AsbAeroResult, evaluate  # noqa: E402
from domain_packs.bwb.schema import BWBParams, MissionDerived  # noqa: E402


class AsbAeroRejected(Exception):
    def __init__(self, message: str, result: Optional[dict] = None):
        super().__init__(message)
        self.result = result


def score_geometry(
    params: BWBParams,
    derived: MissionDerived,
    weights: Optional[obj.ObjectiveWeights] = None,
    config: Optional[obj.ObjectiveConfig] = None,
    cg_frac_mac: float = 0.25,
    outdir: Optional[str] = None,
) -> dict:
    """Evaluate + MDO-score one geometry. Returns a serialisable dict:
    {aero, breakdown, score, feasible}. Writes asb_aero.json into `outdir` if given.

    Raises AsbAeroRejected on a degenerate aero solve (not on an infeasible score —
    an infeasible design is a valid, informative result the caller may still want)."""
    weights = weights or obj.ObjectiveWeights()
    config = config or obj.config_from_derived(derived)

    try:
        aero: AsbAeroResult = evaluate(params, derived, cg_frac_mac=cg_frac_mac)
    except AsbAeroError as e:
        raise AsbAeroRejected(f"AeroSandbox solve failed: {e}")

    breakdown = obj.score(params, aero, weights, config)

    payload = {
        "evidence": aero.evidence,
        "score": breakdown.score,
        "feasible": breakdown.feasible,
        "reject_reason": breakdown.reject_reason,
        "aero": aero.model_dump(),
        "breakdown": breakdown.model_dump(),
        "weights": weights.model_dump(),
        "config": config.model_dump(),
    }

    if outdir:
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "asb_aero.json"), "w") as f:
            json.dump(payload, f, indent=2)

    return payload
