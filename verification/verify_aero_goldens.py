"""V4 — the equation-tier physics core against its committed golden panel (S0-7/S0-8).

`domain_packs/bwb/aero_estimates.py` produces every L/D, CD0, Oswald e, CLmax, stall
speed and neutral point the user ever sees. This script re-runs the reference case and
the component ladders and compares them, field by field, with the values committed in
`tests/data/aero_golden.json`. `tests/test_bwb_aero_goldens.py` asserts the same file,
so the suite and this script can never disagree about what the physics is supposed to
return.

    python verification/verify_aero_goldens.py            # check (exit 1 on any diff)
    python verification/verify_aero_goldens.py --update   # rewrite the golden file

GOLDEN-UPDATE POLICY (spec 2026-07-25-stage1-limitations-and-hardening-design.md,
"Golden policy"): `--update` may only be run together with a ledger entry naming the
physical reason for the change and its source. A golden diff in a PR without that entry
is a rejected PR. A failing row here is not automatically a bug — it means a number the
user sees has moved. Confirm it was intended, then update deliberately.

Pure `domain_packs`: no OpenFOAM, no OpenVSP, no session required. Runs on the Windows
python and in the WSL FoamAgent env alike.
"""
import argparse
import json
import os

import _common  # noqa: F401  -- puts REPO_ROOT and src on sys.path
from _common import REPO_ROOT, Table

from domain_packs.bwb.aero_estimates import (cl_max_3d, effective_c4_sweep_deg,
                                             effective_le_sweep_deg, estimate,
                                             form_factor, mean_tc,
                                             neutral_point_m,
                                             parasite_drag_coeff,
                                             oswald_efficiency,
                                             skin_friction_coeff)
from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.schema import BWBParams

GOLDEN_PATH = os.path.join(REPO_ROOT, "tests", "data", "aero_golden.json")

# rel=1e-12 is a tripwire for real changes, not a platform-precision lottery: the same
# arithmetic on the same inputs is bit-stable well below this on both interpreters.
REL_TOL = 1e-12

# --- the reference case and the ladders every golden is measured on -----------------
# These live here rather than in the test module so that `--update` and the suite read
# one definition. The JSON carries the reference case with the values, so a golden can
# never be compared against a case it was not measured at.
REFERENCE = {
    "params": dict(
        span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28, mid_chord_frac=0.6,
        taper_outer=0.4, sweep_inner_deg=35, sweep_outer_deg=18,
        root_thickness_pct=14, tip_thickness_pct=10, root_twist_deg=1.0,
        tip_twist_deg=-3.0, dihedral_deg=3.0,
    ),
    "altitude_m": 0.0,
    "speed_ms": 18.0,
    "weight_n": 3.5 * 9.81,
    "launch": "hand",
}

# Reynolds ladder straddling the RE_FLOOR in skin_friction_coeff. 0 and -5 used to sit
# here too, because the floor laundered them into a plausible Cf; since S6-F2c they
# raise, and test_bwb_aero_goldens.py asserts that instead of pinning a value.
CF_LADDER = [1e3, 9999.0, 1e4, 1e5, 2e5, 5e5, 1e6, 2e6, 5e6]
FORM_FACTOR_LADDER = [0.05, 0.10, 0.14, 0.20]
# Includes the clamp plateaus deliberately: ~23% of the legal (AR x sweep) space is
# pinned at 0.50 or 0.95, where e has zero gradient in both AR and sweep. Pinning those
# cells means the clamped fraction cannot grow unnoticed.
OSWALD_GRID = [
    (2.5, 0.0), (2.5, 30.0), (2.5, 45.0), (2.5, 60.0),
    (3.0, 0.0), (3.0, 30.0), (3.0, 60.0),
    (4.8, 0.0), (4.8, 30.0),
    (5.0, 0.0), (5.0, 15.0), (5.0, 30.0), (5.0, 45.0), (5.0, 60.0),
    (8.0, 0.0), (8.0, 30.0), (8.0, 45.0),
    (10.0, 0.0), (10.0, 15.0), (10.0, 30.0),
]
CD0_RE_LADDER = [2e5, 5e5, 1e6, 2e6]

# The F4 worked case: both effective sweeps on a geometry where the quarter-chord sweep
# exceeds the leading-edge sweep (see the S6-F4 xfail).
SWEEP_CASE = dict(
    span_m=1.8, root_chord_m=0.25, centre_body_fraction=0.45, mid_chord_frac=0.85,
    taper_outer=0.7, sweep_inner_deg=60, sweep_outer_deg=40, root_thickness_pct=14,
    tip_thickness_pct=9, root_twist_deg=2.0, tip_twist_deg=-3.0, dihedral_deg=2.0,
)


def compute() -> dict:
    """Every golden value, straight from live code. The single source for --update."""
    params = BWBParams(**REFERENCE["params"])
    panel = estimate(params, atmosphere_at(REFERENCE["altitude_m"]),
                     REFERENCE["speed_ms"], REFERENCE["weight_n"],
                     REFERENCE["launch"])
    sweep_params = BWBParams(**SWEEP_CASE)

    return {
        "reference": REFERENCE,
        "panel": {k: v for k, v in panel.model_dump().items()
                  if isinstance(v, float)},
        "panel_launch_ok": panel.launch_ok,
        "geometry": {
            "mean_tc": mean_tc(params),
            "form_factor_mean_tc": form_factor(mean_tc(params)),
            "effective_le_sweep_deg": effective_le_sweep_deg(params),
            "effective_c4_sweep_deg": effective_c4_sweep_deg(params),
            "cl_max_3d": cl_max_3d(params),
            "neutral_point_m": neutral_point_m(params),
        },
        # Pairs, not object keys: JSON object keys are strings, and round-tripping a
        # float through str() is exactly the kind of silent precision loss a golden
        # file must not have.
        "skin_friction_coeff": [[re, skin_friction_coeff(re)] for re in CF_LADDER],
        "form_factor": [[tc, form_factor(tc)] for tc in FORM_FACTOR_LADDER],
        "oswald_efficiency": [[ar, sweep, oswald_efficiency(ar, sweep)]
                              for ar, sweep in OSWALD_GRID],
        "cd0_vs_reynolds": [[re, parasite_drag_coeff(params, re)]
                            for re in CD0_RE_LADDER],
        "sweep_case": {
            "params": SWEEP_CASE,
            "effective_le_sweep_deg": effective_le_sweep_deg(sweep_params),
            "effective_c4_sweep_deg": effective_c4_sweep_deg(sweep_params),
        },
    }


def load_goldens() -> dict:
    with open(GOLDEN_PATH) as f:
        return json.load(f)


def _matches(live, golden) -> bool:
    if golden == 0.0:
        return live == 0.0
    return abs(live - golden) <= REL_TOL * abs(golden)


def _check_table(t: Table, name: str, live: list, golden: list, key_len: int):
    """One row per component ladder, naming every entry that moved."""
    if len(live) != len(golden):
        t.add(name, False, f"ladder changed length: {len(golden)} -> {len(live)}")
        return
    bad = []
    for live_row, golden_row in zip(live, golden):
        if live_row[:key_len] != golden_row[:key_len]:
            bad.append(f"key {golden_row[:key_len]} -> {live_row[:key_len]}")
        elif not _matches(live_row[key_len], golden_row[key_len]):
            bad.append(f"{golden_row[:key_len]}: {golden_row[key_len]!r} -> "
                       f"{live_row[key_len]!r}")
    t.add(name, not bad,
          "; ".join(bad) if bad else f"{len(golden)} entries unchanged")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--update", action="store_true",
                    help="rewrite tests/data/aero_golden.json from live code "
                         "(requires a limitations-ledger entry — see module docstring)")
    args = ap.parse_args()

    live = compute()

    if args.update:
        os.makedirs(os.path.dirname(GOLDEN_PATH), exist_ok=True)
        with open(GOLDEN_PATH, "w") as f:
            json.dump(live, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"wrote {GOLDEN_PATH}")
        print("Record the physical reason and the source in the limitations ledger; "
              "a golden diff without one is a rejected PR.")
        return 0

    if not os.path.exists(GOLDEN_PATH):
        print(f"golden file missing: {GOLDEN_PATH}\n"
              f"run with --update to create it")
        return 1

    golden = load_goldens()
    t = Table("GOLDEN AERO PANEL")

    if golden.get("reference") != live["reference"]:
        t.add("reference case", False,
              "the committed reference case differs from this script's — the goldens "
              "below were measured on a different aircraft")

    for field in sorted(live["panel"]):
        if field not in golden["panel"]:
            t.add(f"panel.{field}", False, "not pinned in the golden file")
            continue
        ok = _matches(live["panel"][field], golden["panel"][field])
        t.add(f"panel.{field}", ok,
              f"{golden['panel'][field]!r}" if ok
              else f"{golden['panel'][field]!r} -> {live['panel'][field]!r}")

    unpinned = sorted(set(golden["panel"]) - set(live["panel"]))
    if unpinned:
        t.add("panel completeness", False,
              f"golden holds field(s) the panel no longer returns: {unpinned}")

    t.add("panel.launch_ok", live["panel_launch_ok"] == golden["panel_launch_ok"],
          repr(golden["panel_launch_ok"]))

    for name in sorted(live["geometry"]):
        ok = _matches(live["geometry"][name], golden["geometry"][name])
        t.add(f"geometry.{name}", ok,
              f"{golden['geometry'][name]!r}" if ok
              else f"{golden['geometry'][name]!r} -> {live['geometry'][name]!r}")

    _check_table(t, "skin_friction_coeff ladder", live["skin_friction_coeff"],
                 golden["skin_friction_coeff"], key_len=1)
    _check_table(t, "form_factor ladder", live["form_factor"],
                 golden["form_factor"], key_len=1)
    _check_table(t, "oswald_efficiency grid", live["oswald_efficiency"],
                 golden["oswald_efficiency"], key_len=2)
    _check_table(t, "cd0 vs reynolds ladder", live["cd0_vs_reynolds"],
                 golden["cd0_vs_reynolds"], key_len=1)

    for name in ("effective_le_sweep_deg", "effective_c4_sweep_deg"):
        ok = _matches(live["sweep_case"][name], golden["sweep_case"][name])
        t.add(f"sweep_case.{name}", ok,
              f"{golden['sweep_case'][name]!r}" if ok
              else f"{golden['sweep_case'][name]!r} -> {live['sweep_case'][name]!r}")

    return t.finish()


if __name__ == "__main__":
    raise SystemExit(main())
