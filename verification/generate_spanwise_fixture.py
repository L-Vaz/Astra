"""Generate tests/js/spanwise_fixture.json from spanwise.py — the Python side of the
Schrenk parity boundary (S1-C2).

WHY A FIXTURE AND NOT A HAND-WRITTEN EXPECTATION. The two mirror bugs found earlier in
this stage — skin friction 9.5% low, LE sweep averaged in angle space instead of
tangent space — both survived a parity test, because that test's expected values had
been copied out of the JS's own output. It compared the mirror against itself. Every
number in this fixture is produced by the Python module, and the JS is never consulted.

    python verification/generate_spanwise_fixture.py            # check
    python verification/generate_spanwise_fixture.py --write    # regenerate

Two directions, as with the planform fixture:
  * tests/js/spanwise_parity.mjs asserts the JS against this file;
  * tests/test_js_parity.py::test_spanwise_fixture_matches_python asserts this file
    against spanwise.py, so a Python change nobody mirrored fails on the Python side.
"""
import argparse
import json
import os

import _common  # noqa: F401  -- puts REPO_ROOT and src on sys.path
from _common import REPO_ROOT

from domain_packs.bwb.aero_estimates import cl_max_3d, stall_solution
from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.schema import BWBParams
from domain_packs.bwb.spanwise import curve_payload, solve_stall

FIXTURE_PATH = os.path.join(REPO_ROOT, "tests", "js", "spanwise_fixture.json")

# Chosen to exercise the parts of the method that can silently disagree:
#   1  the golden reference — washout -3 deg, stalls outboard at eta 0.647
#   2  UNIFORM twist: the chord-weighted mean twist equals the local twist everywhere,
#      so cl_basic must be EXACTLY zero at every station. A mirror that dropped the
#      zero-lift reference, or weighted it by span instead of chord, cannot fake this
#   3  HEAVY washout (-7 deg tip, the schema floor): the basic loading at its strongest,
#      where a mirror that used the ellipse instead of the zero line is off by 2x
#   4  small span, high sweep, low Reynolds: pushes the tip off the bottom of the
#      table's Reynolds grid, testing the flat extrapolation
#   5  three DIFFERENT sections root/mid/tip with a fat centre body, so the panel
#      break sits well inboard and the interpolation is genuinely piecewise
#
# Wash-in is deliberately absent: BWBParams rejects tip twist above root twist as
# unstable for a tailless configuration, so it is not a reachable state to test.
CASES = [
    dict(label="reference",
         params=dict(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                     mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                     sweep_outer_deg=18, root_airfoil="e474", mid_airfoil="e472",
                     tip_airfoil="s5010", root_twist_deg=1.0, tip_twist_deg=-3.0,
                     dihedral_deg=3.0),
         reynolds_mac=409000.0, weight_n=39.24),
    dict(label="uniform-twist",
         params=dict(span_m=1.8, root_chord_m=0.45, centre_body_fraction=0.30,
                     mid_chord_frac=0.7, taper_outer=0.5, sweep_inner_deg=30,
                     sweep_outer_deg=12, root_airfoil="e474", mid_airfoil="e474",
                     tip_airfoil="e474", root_twist_deg=2.0, tip_twist_deg=2.0,
                     dihedral_deg=0.0),
         reynolds_mac=300000.0, weight_n=25.0),
    dict(label="heavy-washout",
         params=dict(span_m=1.8, root_chord_m=0.45, centre_body_fraction=0.30,
                     mid_chord_frac=0.7, taper_outer=0.5, sweep_inner_deg=30,
                     sweep_outer_deg=12, root_airfoil="e474", mid_airfoil="e472",
                     tip_airfoil="s5010", root_twist_deg=2.0, tip_twist_deg=-7.0,
                     dihedral_deg=0.0),
         reynolds_mac=300000.0, weight_n=25.0),
    dict(label="small-span-high-sweep",
         params=dict(span_m=0.8, root_chord_m=0.25, centre_body_fraction=0.12,
                     mid_chord_frac=0.35, taper_outer=0.7, sweep_inner_deg=60,
                     sweep_outer_deg=40, root_airfoil="e474", mid_airfoil="e472",
                     tip_airfoil="oa209", root_twist_deg=2.0, tip_twist_deg=-3.0,
                     dihedral_deg=0.0),
         reynolds_mac=90000.0, weight_n=8.0),
    dict(label="fat-centre-body",
         params=dict(span_m=3.0, root_chord_m=1.2, centre_body_fraction=0.45,
                     mid_chord_frac=0.85, taper_outer=0.7, sweep_inner_deg=15,
                     sweep_outer_deg=5, root_airfoil="e475", mid_airfoil="e474",
                     tip_airfoil="oa212", root_twist_deg=5.0, tip_twist_deg=2.0,
                     dihedral_deg=8.0),
         reynolds_mac=900000.0, weight_n=120.0),
]

# Sea level, the atmosphere the cockpit boots at.
ALTITUDE_M = 0.0


def build() -> dict:
    atm = atmosphere_at(ALTITUDE_M)
    cases = []
    for raw in CASES:
        p = BWBParams(**raw["params"])
        sol = solve_stall(p, raw["reynolds_mac"])
        v_stall, clmax_3d, self_consistent = stall_solution(p, atm, raw["weight_n"])
        cases.append({
            "label": raw["label"],
            # model_dump(), not the raw kwargs: it carries the computed thickness the
            # sections imply, which is what the server actually sends the browser.
            "params": p.model_dump(),
            "reynolds_mac": raw["reynolds_mac"],
            "weight_n": raw["weight_n"],
            "curves": curve_payload(p.root_airfoil, p.mid_airfoil, p.tip_airfoil),
            # At a FIXED Reynolds — isolates the Schrenk algebra from the fixed point.
            "at_fixed_reynolds": {
                "cl_max_wing": sol.cl_max_wing,
                # cl_max_wing with the cos(Lc/4) SWEEP PATCH applied. TM-948 has no
                # sweep term; the patch is a separate step on both sides and is
                # asserted separately so a mirror cannot fold it into the method.
                "cl_max_3d": cl_max_3d(p, raw["reynolds_mac"]),
                "stall_eta": sol.stall_eta,
                "stall_is_outboard": sol.stall_is_outboard,
                "margin_at_root": sol.margin_at_root,
                "n_stations": len(sol.stations),
                # A sparse sample rather than all 80: enough to catch a wrong grid or
                # a wrong interpolation, without a 4x file for no extra signal.
                "sample_stations": [
                    {"i": i, "y": s.y, "eta": s.eta, "chord": s.chord,
                     "twist_deg": s.twist_deg, "reynolds": s.reynolds,
                     "cl_add": s.cl_add, "cl_basic": s.cl_basic, "cl_max": s.cl_max,
                     "cm0": s.cm0, "x_c4": s.x_c4}
                    for i, s in enumerate(sol.stations)
                    if i % 8 == 0 or i == len(sol.stations) - 1
                ],
            },
            # The self-consistent fixed point — CLmax <- Re <- V_stall <- CLmax. The
            # browser must iterate it too or it reads ~6% optimistic.
            "self_consistent": {
                "altitude_m": ALTITUDE_M,
                "v_stall_ms": v_stall,
                "cl_max_3d": clmax_3d,
                "stall_eta": self_consistent.stall_eta,
                "stall_is_outboard": self_consistent.stall_is_outboard,
            },
        })
    return {"generated_from": "domain_packs/bwb/spanwise.py", "cases": cases}


FIXTURE_SIG_FIGS = 12
"""Significant figures the fixture is stored to, and the reason it is not full repr.

The station grid is COSINE-SPACED, and `math.cos` is not bit-identical across C
libraries. Running the generator under glibc (the WSL FoamAgent env) and under the MSVC
runtime (the Windows fast loop) produces station positions that differ in the LAST BIT:

    y        0.07798497079789511  vs  0.07798497079789513
    cl_basic 0.06224213805806669  vs  0.06224213805806668

which is a relative difference of ~3e-16 and physically meaningless — but a byte-exact
fixture cannot be reproduced in both environments, so the suite passed on Windows and
failed in WSL on the same source. Rounding to 12 significant figures puts the stored
values three orders of magnitude coarser than that disagreement and still an order of
magnitude FINER than tests/js/spanwise_parity.mjs's 1e-11 comparison tolerance, so the
fixture keeps all of its drift-detecting power and gains reproducibility.

Nothing else in the project rounds: the aero goldens compare at rel=1e-12 against live
values and never round-trip through a cosine grid, so they do not hit this."""


_EXACT_KEYS = ("params", "curves", "reynolds_mac", "weight_n")
"""Blocks that must NEVER be rounded: they are the INPUTS the JS is fed.

Learned by breaking it. The first version of `_round` walked the whole payload, so the
browser ran Schrenk on 12-significant-figure section curves while Python ran it on the
full-precision ones, and the two disagreed by 2.2e-11 on the basic loading — a
mismatch the rounding was supposed to remove, manufactured by the rounding itself.
Rounding an expected OUTPUT loses nothing a 1e-11 comparison can see. Rounding an INPUT
means the two sides are no longer solving the same problem."""


def _round(value, exact: bool = False):
    if exact:
        return value
    if isinstance(value, float):
        return float(f"%.{FIXTURE_SIG_FIGS}g" % value)
    if isinstance(value, dict):
        return {k: _round(v, exact=k in _EXACT_KEYS) for k, v in value.items()}
    if isinstance(value, list):
        return [_round(v) for v in value]
    return value


def serialise(payload: dict) -> str:
    return json.dumps(_round(payload), indent=2, sort_keys=True) + "\n"


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
        print("spanwise fixture is STALE: spanwise.py no longer matches the committed "
              "fixture the JS is tested against. Regenerate with --write and check "
              "src/cockpit/static/spanwise.js mirrors the change.")
        return 1
    print("spanwise fixture matches spanwise.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
