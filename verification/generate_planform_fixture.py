"""Generate tests/js/planform_fixture.json from schema.py — the Python side of the
one cross-language boundary that cannot be deduplicated (S7-I1).

The JS planform/station algebra is a mirror of `domain_packs/bwb/schema.py`. Five
copies became one per language; the remaining Python <-> JS pair is parity-tested from
both directions:

  * tests/js/planform_parity.mjs and station_parity.mjs assert the JS against this
    fixture;
  * tests/test_js_parity.py::test_planform_fixture_matches_python regenerates it and
    asserts byte-identity, so a Python change that is not mirrored fails on the Python
    side too.

    python verification/generate_planform_fixture.py            # check
    python verification/generate_planform_fixture.py --write    # regenerate
"""
import argparse
import json
import math
import os

import _common  # noqa: F401  -- puts REPO_ROOT and src on sys.path
from _common import REPO_ROOT

from domain_packs.bwb.schema import BWBParams

FIXTURE_PATH = os.path.join(REPO_ROOT, "tests", "js", "planform_fixture.json")

# Deliberately spread across the legal envelope: the reference case, both span
# extremes, a fat centre body, a thin outer panel, and the F4 worked case.
CASES = [
    dict(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28, mid_chord_frac=0.6,
         taper_outer=0.4, sweep_inner_deg=35, sweep_outer_deg=18,
         root_thickness_pct=14, tip_thickness_pct=10, root_twist_deg=1.0,
         tip_twist_deg=-3.0, dihedral_deg=3.0),
    dict(span_m=0.8, root_chord_m=0.25, centre_body_fraction=0.12, mid_chord_frac=0.35,
         taper_outer=0.7, sweep_inner_deg=60, sweep_outer_deg=40,
         root_thickness_pct=14, tip_thickness_pct=9, root_twist_deg=2.0,
         tip_twist_deg=-3.0, dihedral_deg=0.0),
    dict(span_m=3.0, root_chord_m=1.2, centre_body_fraction=0.45, mid_chord_frac=0.85,
         taper_outer=0.7, sweep_inner_deg=15, sweep_outer_deg=5,
         root_thickness_pct=20, tip_thickness_pct=14, root_twist_deg=5.0,
         tip_twist_deg=2.0, dihedral_deg=8.0),
    dict(span_m=1.8, root_chord_m=0.25, centre_body_fraction=0.45, mid_chord_frac=0.85,
         taper_outer=0.7, sweep_inner_deg=60, sweep_outer_deg=40,
         root_thickness_pct=14, tip_thickness_pct=9, root_twist_deg=2.0,
         tip_twist_deg=-3.0, dihedral_deg=2.0),
]

N1, N2 = 6, 12   # the JS station counts; this file defines the shared convention


def _stations(p: BWBParams) -> list:
    """The same 19-station table planform.js builds, in Python."""
    c_root, c_mid, c_tip = p.chords()
    b1, b2 = p.half_widths()
    tan_i = math.tan(math.radians(p.sweep_inner_deg))
    tan_o = math.tan(math.radians(p.sweep_outer_deg))
    tan_d = math.tan(math.radians(p.dihedral_deg))
    mid_thick = (p.root_thickness_pct + p.tip_thickness_pct) / 2.0
    mid_twist = (p.root_twist_deg
                 + (p.tip_twist_deg - p.root_twist_deg) * p.centre_body_fraction)

    out = []
    for i in range(N1 + 1):
        f = i / N1
        y = b1 * f
        out.append({
            "y": y,
            "chord": p.root_chord_m + (c_mid - p.root_chord_m) * f,
            "xle": y * tan_i,
            "z": y * tan_d,
            "tc": (p.root_thickness_pct
                   + (mid_thick - p.root_thickness_pct) * f) / 100.0,
            "tw": p.root_twist_deg + (mid_twist - p.root_twist_deg) * f,
        })
    for i in range(1, N2 + 1):
        f = i / N2
        y = b1 + b2 * f
        out.append({
            "y": y,
            "chord": c_mid + (c_tip - c_mid) * f,
            "xle": b1 * tan_i + (y - b1) * tan_o,
            "z": y * tan_d,
            "tc": (mid_thick + (p.tip_thickness_pct - mid_thick) * f) / 100.0,
            "tw": mid_twist + (p.tip_twist_deg - mid_twist) * f,
        })
    return out


def build() -> dict:
    cases = []
    for raw in CASES:
        p = BWBParams(**raw)
        cases.append({
            "params": raw,
            "chords": list(p.chords()),
            "half_widths": list(p.half_widths()),
            "planform_area_m2": p.planform_area_m2(),
            "mean_aero_chord_m": p.mean_aero_chord_m(),
            "aspect_ratio": p.aspect_ratio(),
            "stations": _stations(p),
        })
    return {"generated_from": "domain_packs/bwb/schema.py", "cases": cases}


def serialise(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="regenerate the committed fixture")
    args = ap.parse_args()

    text = serialise(build())
    if args.write:
        os.makedirs(os.path.dirname(FIXTURE_PATH), exist_ok=True)
        with open(FIXTURE_PATH, "w") as f:
            f.write(text)
        print(f"wrote {FIXTURE_PATH}")
        return 0

    if not os.path.exists(FIXTURE_PATH):
        print(f"missing {FIXTURE_PATH}; run with --write")
        return 1
    with open(FIXTURE_PATH) as f:
        current = f.read()
    if current != text:
        print("planform fixture is STALE: schema.py no longer matches the committed "
              "fixture the JS is tested against. Regenerate with --write and check "
              "src/cockpit/static/planform.js mirrors the change.")
        return 1
    print("planform fixture matches schema.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
