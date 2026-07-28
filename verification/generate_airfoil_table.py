"""Screen the AeroSandbox airfoil library with NeuralFoil into a committed table.

WHY A TABLE AND NOT A LIVE CALL (S1-C)
--------------------------------------
`domain_packs` must stay importable from the OpenVSP py3.10 venv, where aerosandbox
is not installed (CLAUDE.md), and `estimate()` runs in tight loops that a neural-net
inference per call would make unusable. Same pattern as the US-1976 atmosphere table:
generate offline with provenance, interpolate at runtime in pure numpy.

WHY SAMPLED DRAG AND NOT A FITTED POLAR
---------------------------------------
The conceptual-design standard form is CD = a + b·CL + c·CL². Measured against
NeuralFoil over 22 airfoils spread across the library, on the ATTACHED branch only
(alpha up to CLmax, so CD(CL) is single-valued):

    Re 1.2e5   RMS  median 20.7%  p90 42.1%  worst 169.5%
    Re 4.0e5   RMS  median 26.0%  p90 61.3%  worst 271.2%
    Re 1.1e6   RMS  median 23.2%  p90 54.3%  worst  93.8%

A ~25% typical error on section drag is not usable even at a conceptual tier. The
cause is physical, not numerical: at Re 1e5-1e6 a laminar section has a DRAG BUCKET.
The boundary layer stays laminar over the forward surface until the pressure gradient
trips it, and the transition point moves abruptly with incidence, putting a kink in
CD(CL) that no smooth quadratic follows. So this table stores CD sampled on a CL
ladder and the runtime interpolates it. The ladder's own interpolation error is
MEASURED here and written into the table's provenance rather than assumed.

REYNOLDS GRID
-------------
Measured across 35 generated candidates spanning the mission envelope (payload
0.1-4 kg, 8-60 m/s, 0-4000 m): Re(MAC) min 1.15e5, median 6.64e5, max 1.09e6. The
grid below brackets that with margin at both ends.

Run:  /opt/conda/envs/FoamAgent/bin/python verification/generate_airfoil_table.py
      --out domain_packs/bwb/data/airfoils_neuralfoil.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from typing import Dict, List, Optional

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Reynolds grid — log-spaced, brackets the measured 1.15e5-1.09e6 envelope.
RE_GRID = [1.0e5, 2.0e5, 4.0e5, 7.0e5, 1.2e6]

# CD is stored at these lift coefficients. Spacing is finer than the drag bucket is
# wide, which is the point; the generator measures whether that holds.
CL_LADDER = [0.0, 0.15, 0.30, 0.45, 0.60, 0.75, 0.90]

# Sweep used to build each entry. Fine enough to locate CLmax and resolve the bucket.
#
# The top end is 30 deg, not 20: a first pass at 20 left 98 of 2171 sections with CLmax
# pinned at the sweep boundary, i.e. still climbing when the data ran out, so the stored
# "CLmax" was a truncation artifact. Measured at Re 4e5 — ah93w300 does not stall until
# 27.0 deg and dbln526 until 20.8. Stall incidence also moves with Reynolds, which is
# why ah80136 (stalls 9.8 deg at Re 4e5) only failed at Re 1.2e6.
#
# Widening alone would be treating the symptom, so each entry also records whether the
# section ACTUALLY stalled inside the sweep. A section that never stalls reports
# stalled=False and the reader flags it, rather than passing off the last swept point
# as a stall peak.
ALPHA_SWEEP = np.arange(-8.0, 30.01, 0.25)

MODEL_SIZE = "medium"


def attached_branch(cl: np.ndarray, i_zero: int) -> tuple:
    """(lo, hi) of the contiguous strictly-increasing run of CL containing alpha=0.

    This is the ONLY interval on which CD(CL) is single-valued, so it is the only
    interval a drag lookup may interpolate over. See the long note in `_entry`."""
    hi = i_zero
    while hi + 1 < cl.size and cl[hi + 1] > cl[hi]:
        hi += 1
    lo = i_zero
    while lo - 1 >= 0 and cl[lo - 1] < cl[lo]:
        lo -= 1
    return lo, hi


def _r(x: Optional[float], places: int = 5) -> Optional[float]:
    """Round for storage. Section CD is ~8e-3, so 5 decimals keeps ~4 significant
    figures — far finer than NeuralFoil's own stated error against XFoil (ln(CD) MAE
    0.020-0.078). Untrimmed float repr made the table 4.0 MB for no accuracy."""
    return None if x is None else round(float(x), places)


def _entry(af, re: float) -> Optional[Dict]:
    """One (airfoil, Reynolds) record, or None if the section cannot be evaluated."""
    import neuralfoil as nf

    r = nf.get_aero_from_airfoil(af, alpha=ALPHA_SWEEP, Re=re, model_size=MODEL_SIZE)
    cl = np.asarray(r["CL"], dtype=float)
    cd = np.asarray(r["CD"], dtype=float)
    cm = np.asarray(r["CM"], dtype=float)
    conf = np.asarray(r["analysis_confidence"], dtype=float)
    if not (np.isfinite(cl).all() and np.isfinite(cd).all()):
        return None

    i0 = int(np.argmin(np.abs(ALPHA_SWEEP)))          # alpha = 0

    # ---- the attached branch -------------------------------------------------
    # NOT `cl[:argmax(cl)]`. For a thin section at these Reynolds numbers, CL
    # collapses at stall and then CLIMBS AGAIN as the separated wake reorganises,
    # sometimes past the pre-stall peak. Measured on naca64a010 at Re 4e5:
    #
    #   alpha  8.25  CL 0.8228  CD 0.027  confidence 0.600   <- real stall
    #   alpha 10.75  CL 0.6888  CD 0.089  confidence 0.211   <- separated
    #   alpha 19.50  CL 0.8308  CD 0.233  confidence 0.175   <- recovers ABOVE the peak
    #
    # so `argmax` returned a post-stall point, the slice swallowed the whole stall,
    # and CD spanned 34x with CL multi-valued. Sorting by CL then mapped one CL onto
    # both 0.007 and 0.23. That produced ladder errors up to 862% which REFINING THE
    # LADDER DID NOT FIX (762% at 7 points, 770% at 37) — the giveaway that the data
    # was multi-valued rather than under-sampled.
    #
    # The attached branch is the contiguous strictly-increasing run of CL containing
    # alpha = 0: up to the FIRST stall, and down to the first negative-alpha stall.
    lo, hi = attached_branch(cl, i0)
    if hi - lo < 5:
        return None

    i_stall = hi
    clmax = float(cl[hi])
    cl_att, cd_att = cl[lo:hi + 1], cd[lo:hi + 1]
    # Lift-curve slope over a small linear window either side of zero incidence.
    lin = (ALPHA_SWEEP >= -2.0) & (ALPHA_SWEEP <= 4.0)
    slope = float(np.polyfit(ALPHA_SWEEP[lin], cl[lin], 1)[0]) if lin.sum() >= 3 else 0.0

    cd_ladder: List[Optional[float]] = []
    for target in CL_LADDER:
        if target > clmax:
            cd_ladder.append(None)                    # beyond this section's CLmax
        else:
            cd_ladder.append(_r(float(np.interp(target, cl_att, cd_att))))

    return {
        "re": re,
        "clmax": _r(clmax, 4),
        "alpha_clmax_deg": _r(float(ALPHA_SWEEP[i_stall]), 2),
        # False => CL was still rising at the top of the sweep, so `clmax` is a lower
        # bound set by where the data stopped, not a stall peak.
        "stalled": bool(i_stall < cl.size - 1),
        "cm0": _r(float(cm[i0])),
        "cl0": _r(float(cl[i0]), 4),
        "cl_alpha_per_deg": _r(slope),
        "cd_at_cl": cd_ladder,
        # Confidence over the ATTACHED BRANCH only. Taken over the whole sweep it was
        # 0.000 for all 2171 sections, because NeuralFoil's confidence collapses deep
        # in the post-stall region (naca64a010: 0.97 attached, 0.13 separated) and the
        # minimum picked that up. A screen with min_confidence=0.5 then rejected the
        # entire library. Third instance of one root cause in this generator: a
        # statistic taken over the full sweep rather than the branch it describes.
        "confidence": _r(float(np.nanmin(conf[lo:hi + 1]))
                         if np.isfinite(conf[lo:hi + 1]).any() else 0.0, 3),
    }


def _ladder_error(af, re: float, entry: Dict) -> Optional[float]:
    """Max % error from interpolating the CL ladder, against the full sweep.

    This is the number that says whether CL_LADDER is dense enough for the drag
    bucket. It is measured, not assumed, and lands in the table's provenance."""
    import neuralfoil as nf

    r = nf.get_aero_from_airfoil(af, alpha=ALPHA_SWEEP, Re=re, model_size=MODEL_SIZE)
    cl = np.asarray(r["CL"], dtype=float)
    cd = np.asarray(r["CD"], dtype=float)
    i0 = int(np.argmin(np.abs(ALPHA_SWEEP)))
    lo, hi = attached_branch(cl, i0)          # same definition the entry was built on
    cl_att, cd_att = cl[lo:hi + 1], cd[lo:hi + 1]

    known = [(c, d) for c, d in zip(CL_LADDER, entry["cd_at_cl"]) if d is not None]
    if len(known) < 2:
        return None
    xs = np.array([k[0] for k in known])
    ys = np.array([k[1] for k in known])

    m = (cl_att >= xs.min()) & (cl_att <= xs.max()) & (cd_att > 0)
    if m.sum() < 5:
        return None
    pred = np.interp(cl_att[m], xs, ys)
    return float(np.max(np.abs((pred - cd_att[m]) / cd_att[m])) * 100.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(
        REPO_ROOT, "domain_packs", "bwb", "data", "airfoils_neuralfoil.json"))
    ap.add_argument("--limit", type=int, default=0,
                    help="screen only the first N sections (smoke test)")
    args = ap.parse_args()

    try:
        import aerosandbox as asb
        import neuralfoil as nf
        import aerosandbox.geometry.airfoil as asb_airfoil
    except ImportError as exc:                                     # pragma: no cover
        print(f"needs the FoamAgent env (aerosandbox + neuralfoil): {exc}")
        return 2

    db = os.path.join(os.path.dirname(asb_airfoil.__file__), "airfoil_database")
    names = sorted(os.path.basename(f)[:-4]
                   for f in glob.glob(os.path.join(db, "*.dat")))
    if args.limit:
        names = names[: args.limit]
    print(f"library: {len(names)} sections x {len(RE_GRID)} Reynolds")

    sections: Dict[str, Dict] = {}
    ladder_errors: List[float] = []
    skipped: List[str] = []
    t0 = time.time()

    for i, name in enumerate(names):
        try:
            af = asb.Airfoil(name)
            tc = float(af.max_thickness())
        except Exception:
            skipped.append(name)
            continue
        entries = []
        for re in RE_GRID:
            try:
                e = _entry(af, re)
            except Exception:
                e = None
            if e is not None:
                entries.append(e)
        if not entries:
            skipped.append(name)
            continue
        # Sample the ladder error rather than measure every section: it is a property
        # of the ladder spacing, not of one airfoil, and the full sweep doubles cost.
        if i % 25 == 0:
            err = _ladder_error(af, RE_GRID[len(RE_GRID) // 2],
                                entries[len(entries) // 2])
            if err is not None:
                ladder_errors.append(err)
        sections[name] = {"tc": _r(tc, 4), "re": entries}
        if (i + 1) % 250 == 0:
            print(f"  {i + 1}/{len(names)}  ({time.time() - t0:.0f}s)")

    ladder_errors.sort()

    def _pct(p: float) -> Optional[float]:
        if not ladder_errors:
            return None
        return round(ladder_errors[min(int(len(ladder_errors) * p),
                                       len(ladder_errors) - 1)], 2)

    table = {
        "provenance": {
            "generator": "verification/generate_airfoil_table.py",
            "aerosandbox": asb.__version__,
            "neuralfoil": getattr(nf, "__version__", "unknown"),
            "model_size": MODEL_SIZE,
            "alpha_sweep_deg": [float(ALPHA_SWEEP[0]), float(ALPHA_SWEEP[-1]),
                                float(ALPHA_SWEEP[1] - ALPHA_SWEEP[0])],
            "re_grid": RE_GRID,
            "cl_ladder": CL_LADDER,
            "sections": len(sections),
            "skipped": len(skipped),
            "entries_that_never_stalled": sum(
                1 for rec in sections.values() for e in rec["re"] if not e["stalled"]),
            "cl_ladder_interpolation_error_pct": {
                "measured_on": len(ladder_errors),
                "median": _pct(0.5), "p90": _pct(0.9),
                "worst": round(ladder_errors[-1], 2) if ladder_errors else None,
            },
            "note": (
                "CD is SAMPLED on cl_ladder, not fitted. A quadratic CD(CL) was "
                "measured at median RMS 20.7-26.0% and worst 271% on the attached "
                "branch, because a laminar drag bucket at Re 1e5-1e6 puts a kink in "
                "the polar. cd_at_cl entries are null above that section's CLmax. "
                "Values past CLmax are NOT stored: CD(CL) is not single-valued there."
            ),
        },
        "sections": sections,
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(table, fh, separators=(",", ":"), sort_keys=True)
    size_mb = os.path.getsize(args.out) / 1e6
    print(f"wrote {args.out}  ({size_mb:.2f} MB, {len(sections)} sections, "
          f"{len(skipped)} skipped, {time.time() - t0:.0f}s)")
    print(f"CL-ladder interpolation error: median {_pct(0.5)}%  p90 {_pct(0.9)}%  "
          f"worst {round(ladder_errors[-1], 2) if ladder_errors else None}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
