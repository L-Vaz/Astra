"""Validation: AeroSandbox fast tier vs VSPAERO on the same geometry.

The MDO search only trusts the AeroSandbox screen (AeroBuildup VLM + NeuralFoil) if
its trends agree with the higher-fidelity VSPAERO vortex-lattice polar on the
reference wing — the same VLM-vs-higher-fidelity handshake the Neuronautics workflow
did against CFD. This script runs the ASB evaluator on a session's built geometry and
compares L/D at trim, trim alpha, lift-curve slope, and neutral-point location against
that session's VSPAERO fast_aero.json.

The two methods differ in fidelity (NeuralFoil viscous build-up vs VSPAERO's own
skin-friction model), so tolerances check TRENDS, not identity.

Run in the FoamAgent env (py3.12, where aerosandbox is installed), against a session
that already has both a `geometry` and a `fast_aero` stage:

    python verification/verify_asb_aero.py --session <SID>
    python verification/verify_asb_aero.py --session <SID> --json report.json
"""
import argparse
import json
import math
import os

import _common  # noqa: F401  -- puts REPO_ROOT and src on sys.path

from domain_packs.bwb import asb_aero
from domain_packs.bwb.schema import GeometryReport, MissionDerived
from services import design_session as ds

# documented tolerances — TREND agreement between two fidelities, not identity
TOL = {
    "l_over_d": ("rel", 0.25),        # within 25 %
    "alpha_trim_deg": ("abs", 2.0),   # within 2 deg
    "cl_alpha_per_deg": ("rel", 0.20),
    "x_np_m_frac_mac": ("abs", 0.10),  # within 10 % MAC
}


def _load(path):
    with open(path) as f:
        return json.load(f)


def _cmp(name, asb_val, vsp_val, mac=None):
    kind, tol = TOL[name]
    if asb_val is None or vsp_val is None:
        return {"metric": name, "asb": asb_val, "vsp": vsp_val,
                "delta": None, "tol": tol, "kind": kind, "pass": False,
                "note": "missing value"}
    if name == "x_np_m_frac_mac":
        delta = abs(asb_val - vsp_val) / mac
    elif kind == "rel":
        delta = abs(asb_val - vsp_val) / max(abs(vsp_val), 1e-9)
    else:
        delta = abs(asb_val - vsp_val)
    return {"metric": name, "asb": asb_val, "vsp": vsp_val,
            "delta": delta, "tol": tol, "kind": kind, "pass": delta <= tol}


def verify_session(sid: str) -> dict:
    geo_dir = ds.latest_dir(sid, "geometry")
    plan_dir = ds.latest_dir(sid, "plan")
    fa_dir = ds.latest_dir(sid, "fast_aero")
    if not (geo_dir and plan_dir and fa_dir):
        raise SystemExit(
            f"session {sid} needs valid plan + geometry + fast_aero stages "
            f"(got plan={bool(plan_dir)} geometry={bool(geo_dir)} fast_aero={bool(fa_dir)})")

    report = GeometryReport.model_validate(_load(os.path.join(geo_dir, "geometry_report.json")))
    derived = MissionDerived.model_validate(_load(os.path.join(plan_dir, "mission_derived.json")))
    vsp = _load(os.path.join(fa_dir, "fast_aero.json"))

    aero = asb_aero.evaluate(report.params, derived)
    mac = report.mean_aero_chord_m

    rows = [
        _cmp("l_over_d", aero.l_over_d, vsp.get("ld_at_trim")),
        _cmp("alpha_trim_deg", aero.alpha_trim_deg, vsp.get("alpha_trim_deg")),
        _cmp("cl_alpha_per_deg", aero.cl_alpha_per_rad * math.pi / 180.0,
             vsp.get("cl_alpha_per_deg")),
        _cmp("x_np_m_frac_mac", aero.x_np_m, vsp.get("x_np_m"), mac=mac),
    ]
    return {
        "session": sid,
        "reference": {"s_ref_m2": report.s_ref_m2, "mac_m": mac,
                      "aspect_ratio": report.aspect_ratio,
                      "required_cl": derived.required_cl},
        "asb": aero.model_dump(),
        "comparison": rows,
        "passed": all(r["pass"] for r in rows),
        "warnings": aero.warnings,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate AeroSandbox tier against VSPAERO")
    ap.add_argument("--session", required=True, help="design session id")
    ap.add_argument("--json", help="write the full report to this path")
    args = ap.parse_args()

    result = verify_session(args.session)

    print(f"ASB vs VSPAERO — session {result['session']}  "
          f"(S={result['reference']['s_ref_m2']:.4f} m^2, MAC={result['reference']['mac_m']:.4f} m, "
          f"AR={result['reference']['aspect_ratio']:.2f}, CL*={result['reference']['required_cl']:.3f})")
    print(f"{'metric':22s} {'ASB':>12s} {'VSPAERO':>12s} {'delta':>10s} {'tol':>8s}  verdict")
    for r in result["comparison"]:
        d = "n/a" if r["delta"] is None else f"{r['delta']:.4f}"
        av = "n/a" if r["asb"] is None else f"{r['asb']:.4f}"
        vv = "n/a" if r["vsp"] is None else f"{r['vsp']:.4f}"
        print(f"{r['metric']:22s} {av:>12s} {vv:>12s} {d:>10s} {r['tol']:>8.3f}  "
              f"{'PASS' if r['pass'] else 'FAIL'} ({r['kind']})")
    if result["warnings"]:
        print("warnings:", "; ".join(result["warnings"]))
    print(f"\nOVERALL: {'PASS' if result['passed'] else 'FAIL'}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"report -> {args.json}")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
