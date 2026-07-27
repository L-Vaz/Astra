"""Gap #8 — fuzz inverse.solve_requirements' documented 'never raises' contract.

Deterministic grid + fixed-seed random sweep. Reproducible by construction.
"""
import itertools
import math
import os
import random
import sys
import time
import traceback

# Relative to this file, not a machine-specific absolute path — the previous hardcoded
# path meant this script only ran on one checkout, on one machine, and moving the
# repository broke it silently.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domain_packs.bwb.aero_estimates import LAUNCH_VSTALL_MAX
from domain_packs.bwb.inverse import (BINDING_LABELS, MET_TOLERANCES,
                                      solve_requirements)
from domain_packs.bwb.requirements import DesignRequirements
from domain_packs.bwb.schema import BWBParams

SEED = 20260725
AR_GATE = (2.5, 10.0)

GRID = dict(
    mtow_kg=[0.05, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0],
    cruise_speed_ms=[8.0, 12.0, 18.0, 25.0, 40.0, 60.0],
    cruise_altitude_m=[0.0, 100.0, 1000.0, 2500.0, 4000.0],
    launch=["hand", "catapult", "runway"],
    span_max_m=[None, 0.8, 1.0, 1.5, 2.0, 3.0],
    v_stall_max_ms=[None, 5.0, 8.0, 10.5, 15.0, 20.0],
    cl_cruise_target=[None, 0.050001, 0.2, 0.5, 0.75],
    ld_min=[None, 5.0, 15.0, 25.0, 40.0],
)


def check(req, res, timings):
    """Return list of violation strings for one solve result."""
    v = []
    p, panel = res.params, res.panel

    try:
        BWBParams.model_validate(p.model_dump())
    except Exception as e:
        v.append(f"params_revalidate:{type(e).__name__}")

    if req.span_max_m is not None and p.span_m > req.span_max_m + 1e-6:
        v.append(f"span_over_cap:{p.span_m:.4f}>{req.span_max_m}")
    if not (0.8 - 1e-9 <= p.span_m <= 3.0 + 1e-9):
        v.append(f"span_out_of_schema:{p.span_m:.4f}")
    ar = p.aspect_ratio()
    if not (AR_GATE[0] - 1e-6 <= ar <= AR_GATE[1] + 1e-6):
        v.append(f"ar_out_of_gate:{ar:.4f}")
    if not (0.25 - 1e-9 <= p.root_chord_m <= 1.2 + 1e-9):
        v.append(f"root_chord_out:{p.root_chord_m:.4f}")
    if p.chords()[2] < 0.04 - 1e-9:
        v.append(f"tip_chord_under:{p.chords()[2]:.4f}")

    s = p.planform_area_m2()
    if not math.isfinite(s) or s <= 0:
        v.append(f"area_bad:{s}")
    if abs(s - panel.s_ref_m2) > 1e-6 * max(1.0, s):
        v.append(f"area_mismatch:{s:.6f}vs{panel.s_ref_m2:.6f}")

    for field, val in panel.model_dump().items():
        if isinstance(val, float) and not math.isfinite(val):
            v.append(f"nonfinite:{field}={val}")

    unmet = [st for st in res.requirements if st.met is False]
    if unmet and res.binding_constraint is None:
        v.append("unmet_without_binding")
    if not unmet and res.binding_constraint is not None:
        v.append(f"binding_without_unmet:{res.binding_constraint}")
    if res.binding_constraint is not None:
        if res.binding_constraint not in BINDING_LABELS:
            v.append(f"unknown_binding_key:{res.binding_constraint}")
        if not res.binding_explanation:
            v.append("empty_binding_explanation")

    # met flags must agree with recomputation under MET_TOLERANCES
    for st in res.requirements:
        if st.target is None:
            continue
        if st.name == "cl_cruise_target":
            ok = abs(st.achieved - st.target) <= MET_TOLERANCES["cl_cruise_target_rel"] * st.target
        elif st.name == "ld_min":
            ok = st.achieved >= st.target - MET_TOLERANCES["ld_min"]
        elif st.name == "v_stall_max_ms":
            ok = st.achieved <= st.target + MET_TOLERANCES["v_stall_max_ms"]
        elif st.name == "span_max_m":
            ok = st.achieved <= st.target + MET_TOLERANCES["span_max_m"]
        else:
            continue
        if ok != st.met:
            v.append(f"met_flag_mismatch:{st.name}")

    # stall cap adherence (the documented user contract)
    cap = min(req.v_stall_max_ms or 1e9, LAUNCH_VSTALL_MAX[req.launch])
    if panel.v_stall_ms > cap + 0.05:
        v.append(f"STALL_CAP_VIOLATED:{panel.v_stall_ms:.2f}>{cap:.2f}")
    return v


def main():
    keys = list(GRID)
    combos = list(itertools.product(*(GRID[k] for k in keys)))
    print(f"grid combinations: {len(combos)}  seed={SEED}")

    exceptions, violations, timings = [], [], []
    stall_cases = []   # (req kwargs, violated?) for blast-radius stats
    n_ok = 0

    for combo in combos:
        kw = dict(zip(keys, combo))
        try:
            req = DesignRequirements(**kw)
        except Exception:
            continue  # not a legal requirement set; skip
        t0 = time.perf_counter()
        try:
            res = solve_requirements(req)
        except Exception as e:
            exceptions.append((kw, f"{type(e).__name__}: {e}"))
            continue
        timings.append(time.perf_counter() - t0)
        n_ok += 1
        v = check(req, res, timings)
        cap = min(req.v_stall_max_ms or 1e9, LAUNCH_VSTALL_MAX[req.launch])
        stall_cases.append((kw, res.panel.v_stall_ms > cap + 0.05))
        if v:
            violations.append((kw, v))

    print(f"legal solves: {n_ok}   exceptions: {len(exceptions)}   "
          f"cases with >=1 violation: {len(violations)}")

    if exceptions:
        print("\n=== EXCEPTIONS (first 5) ===")
        for kw, msg in exceptions[:5]:
            print(" ", kw)
            print("   ->", msg[:160])

    # violation histogram
    hist = {}
    for _kw, vs in violations:
        for x in vs:
            hist[x.split(":")[0]] = hist.get(x.split(":")[0], 0) + 1
    if hist:
        print("\n=== VIOLATION KINDS ===")
        for k, c in sorted(hist.items(), key=lambda t: -t[1]):
            print(f"  {k:28} {c:6d}  ({100*c/max(n_ok,1):.1f}% of solves)")

    # examples of each kind
    seen = set()
    print("\n=== ONE REPRODUCER PER KIND ===")
    for kw, vs in violations:
        for x in vs:
            kind = x.split(":")[0]
            if kind in seen:
                continue
            seen.add(kind)
            print(f"  {x}")
            print(f"    DesignRequirements(**{kw})")

    # stall blast radius by dimension
    print("\n=== STALL-CAP BLAST RADIUS ===")
    tot = len(stall_cases)
    bad = sum(1 for _k, b in stall_cases if b)
    print(f"  overall: {bad}/{tot} = {100*bad/max(tot,1):.1f}% of legal solves")
    for dim in ("mtow_kg", "span_max_m", "v_stall_max_ms", "launch"):
        print(f"  by {dim}:")
        vals = {}
        for kw, b in stall_cases:
            key = kw[dim]
            t, f = vals.get(key, (0, 0))
            vals[key] = (t + 1, f + (1 if b else 0))
        for key in sorted(vals, key=lambda x: (x is None, x)):
            t, f = vals[key]
            print(f"    {str(key):>8}: {f:5d}/{t:5d} = {100*f/t:5.1f}%")

    # timing
    if timings:
        timings.sort()
        print(f"\n=== TIMING ===  mean {1000*sum(timings)/len(timings):.2f} ms  "
              f"p99 {1000*timings[int(0.99*len(timings))]:.2f} ms  "
              f"worst {1000*timings[-1]:.2f} ms")

    # determinism / idempotence
    print("\n=== DETERMINISM / IDEMPOTENCE ===")
    rng = random.Random(SEED)
    unstable = 0
    for _ in range(300):
        kw = dict(
            mtow_kg=rng.uniform(0.05, 25), cruise_speed_ms=rng.uniform(8, 60),
            cruise_altitude_m=rng.uniform(0, 4000),
            launch=rng.choice(["hand", "catapult", "runway"]),
            span_max_m=rng.choice([None, rng.uniform(0.8, 3.0)]),
            v_stall_max_ms=rng.choice([None, rng.uniform(5, 20)]))
        try:
            req = DesignRequirements(**kw)
            a = solve_requirements(req)
            b = solve_requirements(req)
            if a.params.model_dump() != b.params.model_dump():
                print("  NON-DETERMINISTIC:", kw)
            c = solve_requirements(req, shape_from=a.params)
            d = solve_requirements(req, shape_from=c.params)
            if abs(c.params.span_m - d.params.span_m) > 1e-6 or \
               abs(c.params.root_chord_m - d.params.root_chord_m) > 1e-6:
                unstable += 1
        except Exception as e:
            print("  EXC in determinism sweep:", type(e).__name__, e, kw)
    print(f"  re-solve (shape_from=own output) not converged: {unstable}/300")


if __name__ == "__main__":
    main()
