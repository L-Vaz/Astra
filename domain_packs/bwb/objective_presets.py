"""BWB domain pack — objective presets (the "fewer, useful options" layer).

The raw MDO objective has nine penalty weights. Exposing all nine to a user is the
complexity the cockpit is meant to hide. This module collapses them to **three
plain-language priorities** the user actually cares about —

    efficiency   how hard to chase L/D
    stability    how docile / self-righting it must be
    buildable    how much to respect area / geometry limits (and, later, weight)

— each a 0..1 knob, plus a handful of named quick-picks. Everything else (structural
weights, CG, the reference floor, search budget) stays defaulted and hidden.

Deterministic and dependency-light (imports only the weights model + math). The
mapping is engineering-judgment, documented, and easy to retune.
"""
from typing import Dict

from .objective import ObjectiveWeights

# quick-pick presets: (efficiency, stability, buildable), each 0..1
NAMED_PRESETS: Dict[str, Dict] = {
    "balanced":   {"label": "Balanced",       "efficiency": 0.5, "stability": 0.5, "buildable": 0.5,
                   "blurb": "A sensible all-rounder."},
    "efficiency": {"label": "Max efficiency",  "efficiency": 1.0, "stability": 0.35, "buildable": 0.35,
                   "blurb": "Chase the best glide; accept a livelier wing."},
    "stable":     {"label": "Docile & stable", "efficiency": 0.35, "stability": 1.0, "buildable": 0.45,
                   "blurb": "Calm in gusts and easy to fly; a little slower."},
    "buildable":  {"label": "Easy to build",   "efficiency": 0.45, "stability": 0.45, "buildable": 1.0,
                   "blurb": "Stay close to the target size and simple shape."},
}

DEFAULT_PRESET = "balanced"
DEFAULT_SPREAD = 0.15  # search half-width per parameter (fraction of its schema range)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def weights_from_priorities(efficiency: float = 0.5, stability: float = 0.5,
                            buildable: float = 0.5) -> ObjectiveWeights:
    """Map three 0..1 priorities to the nine penalty weights.

    - stability scales the lateral-directional and pitch penalties (0.5x .. 2.0x);
    - buildable scales the geometry (area/gate) penalty (1.0x .. 4.0x);
    - efficiency *softens* all penalties (1.3x .. 0.7x), so higher efficiency lets L/D
      dominate the score.
    Structural weights stay 0 (dormant until the beam surrogate lands)."""
    e, s, b = _clamp01(efficiency), _clamp01(stability), _clamp01(buildable)
    soften = 1.3 - 0.6 * e          # 1.3 .. 0.7
    stab = 0.5 + 1.5 * s            # 0.5 .. 2.0
    build = 1.0 + 3.0 * b           # 1.0 .. 4.0
    return ObjectiveWeights(
        w6_latdir=round(soften * stab * 1.0, 3),
        w7_pitch=round(soften * stab * 1.0, 3),
        w8_trim=round(soften * 0.5, 3),
        w9_geometry=round(soften * build * 2.0, 3),
    )


def priorities_for(preset: str) -> Dict[str, float]:
    p = NAMED_PRESETS.get(preset, NAMED_PRESETS[DEFAULT_PRESET])
    return {"efficiency": p["efficiency"], "stability": p["stability"],
            "buildable": p["buildable"]}


def propose(derived=None) -> Dict:
    """The payload the cockpit / `propose_objective` uses to auto-fill the panel:
    named quick-picks, the default priorities, the resulting weights, a suggested
    search spread, and the plain-language questions that customise it.

    `derived` is accepted for future mission-aware tuning; the v1 proposal is a
    balanced default (the search's reference-relative stability floor already adapts
    to the wing, so the weights need not)."""
    priorities = priorities_for(DEFAULT_PRESET)
    weights = weights_from_priorities(**priorities)
    return {
        "presets": [
            {"key": k, "label": v["label"], "blurb": v["blurb"],
             "priorities": priorities_for(k)}
            for k, v in NAMED_PRESETS.items()
        ],
        "default_preset": DEFAULT_PRESET,
        "priorities": priorities,
        "weights": weights.model_dump(),
        "spread": DEFAULT_SPREAD,
        "questions": [
            {"key": "priorities", "prompt": "What matters most for this wing?",
             "options": ["efficiency", "stability", "buildable"]},
            {"key": "spread", "prompt": "Explore widely, or stay close to your wing?"},
            {"key": "freeze", "prompt": "Anything to hold fixed (e.g. span)?"},
        ],
        "note": "Structural penalties are dormant (weight 0) until the beam surrogate "
                "is enabled; only the options that change the answer are shown.",
    }
