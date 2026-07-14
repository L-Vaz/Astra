"""BWB domain pack — VSPAERO tier-1 (vortex lattice) aero analysis.

Runs under the OpenVSP venv (/opt/vsp-venv), same as vsp_builder.py.

CLI:
    /opt/vsp-venv/bin/python vsp_aero.py --vsp3 bwb.vsp3 --sref S --cref MAC \
        --bref span --recref RE --cl-target 0.18 --outdir DIR
        [--alpha-start -2 --alpha-end 8 --alpha-npts 6] [--mach 0.07]

Writes fast_aero.json: polar (alpha, CL, CD, CM, L/D), CL_alpha slope,
neutral point, trim alpha for the CL target, and gate results.
"""
import argparse
import json
import math
import os
import sys

import openvsp as vsp


def find_vspaero() -> str:
    for cand in ("/opt/OpenVSP", os.path.dirname(vsp.__file__)):
        if os.path.exists(os.path.join(cand, "vspaero")):
            return cand
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vsp3", required=True)
    ap.add_argument("--sref", type=float, required=True)
    ap.add_argument("--cref", type=float, required=True)
    ap.add_argument("--bref", type=float, required=True)
    ap.add_argument("--recref", type=float, required=True)
    ap.add_argument("--cl-target", type=float, required=True)
    ap.add_argument("--mach", type=float, default=0.07)
    ap.add_argument("--alpha-start", type=float, default=-2.0)
    ap.add_argument("--alpha-end", type=float, default=8.0)
    ap.add_argument("--alpha-npts", type=int, default=6)
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    vsp.VSPCheckSetup()
    vsp.ClearVSPModel()

    vspaero_path = find_vspaero()
    if vspaero_path:
        try:
            vsp.SetVSPAEROPath(vspaero_path)
        except AttributeError:
            pass

    # work on a copy inside outdir so VSPAERO scratch files stay contained
    import shutil
    local_vsp3 = os.path.join(args.outdir, "aero_model.vsp3")
    shutil.copy2(args.vsp3, local_vsp3)
    vsp.ReadVSPFile(local_vsp3)
    vsp.Update()

    # generate the VSPAERO geometry input (.vspgeom) before the sweep
    # thin (camber-surface) representation only — thick+thin together makes
    # VSPAERO see every wing panel twice (duplicate-edge warnings, bad polar)
    comp = "VSPAEROComputeGeometry"
    vsp.SetAnalysisInputDefaults(comp)
    vsp.SetIntAnalysisInput(comp, "GeomSet", [vsp.SET_NONE])
    vsp.SetIntAnalysisInput(comp, "ThinGeomSet", [vsp.SET_ALL])
    print("Computing VSPAERO geometry...")
    vsp.ExecAnalysis(comp)

    analysis = "VSPAEROSweep"
    vsp.SetAnalysisInputDefaults(analysis)

    def set_in(name, val, kind):
        try:
            if kind == "d":
                vsp.SetDoubleAnalysisInput(analysis, name, [float(val)])
            else:
                vsp.SetIntAnalysisInput(analysis, name, [int(val)])
        except Exception as e:
            print(f"warn: analysis input {name} not set ({e})")

    set_in("GeomSet", vsp.SET_NONE, "i")
    set_in("ThinGeomSet", vsp.SET_ALL, "i")
    set_in("RefFlag", 0, "i")            # manual reference quantities
    set_in("Sref", args.sref, "d")
    set_in("cref", args.cref, "d")
    set_in("bref", args.bref, "d")
    set_in("ReCref", args.recref, "d")
    set_in("MachStart", args.mach, "d")
    set_in("MachNpts", 1, "i")
    set_in("AlphaStart", args.alpha_start, "d")
    set_in("AlphaEnd", args.alpha_end, "d")
    set_in("AlphaNpts", args.alpha_npts, "i")
    set_in("Xcg", 0.0, "d")              # CM about x=0 -> x_np = -dCM/dCL * cref
    set_in("WakeNumIter", 3, "i")

    print("Executing VSPAEROSweep...")
    vsp.ExecAnalysis(analysis)

    # Parse the .polar file directly — the results-API container names vary
    # across OpenVSP versions, the file format does not.
    polar_path = os.path.join(args.outdir, "aero_model.polar")
    if not os.path.exists(polar_path):
        print("FAST-AERO REJECT: VSPAERO produced no .polar file")
        return 1
    header, rows = None, []
    with open(polar_path) as f:
        for line in f:
            toks = line.split()
            if not toks:
                continue
            if header is None:
                if toks[0] == "Beta":
                    header = toks
                continue
            try:
                rows.append([float(x) for x in toks[:len(header)]])
            except ValueError:
                continue
    if not header or not rows:
        print("FAST-AERO REJECT: could not parse .polar")
        return 1
    idx = {name: i for i, name in enumerate(header)}
    def column(*names):
        for nm in names:
            if nm in idx:
                return [r[idx[nm]] for r in rows]
        return []
    alpha = column("AoA")
    cl = column("CLtot", "CL")
    cd = column("CDtot")
    cdo = column("CDo")   # VSPAERO's own skin-friction estimate
    cdi = column("CDi")   # induced component alone (what the VLM actually solves)
    cm = column("CMytot", "CMy")
    if not alpha or not cl or len(cl) != len(alpha):
        print(f"FAST-AERO REJECT: polar missing AoA/CL columns (have {list(idx)[:12]}...)")
        return 1

    # least-squares slopes over the sweep
    n = len(alpha)
    ma = sum(alpha) / n
    mcl = sum(cl) / n
    denom = sum((a - ma) ** 2 for a in alpha) or 1e-12
    cl_alpha_deg = sum((a - ma) * (c - mcl) for a, c in zip(alpha, cl)) / denom
    dcm_dcl = 0.0
    if cm and len(cm) == n:
        mcm = sum(cm) / n
        dcl = sum((c - mcl) ** 2 for c in cl) or 1e-12
        dcm_dcl = sum((c - mcl) * (m - mcm) for c, m in zip(cl, cm)) / dcl
    x_np = -dcm_dcl * args.cref  # CM referenced at x=0

    # trim alpha for the CL target (linear interp/extrapolation on the fit)
    alpha_trim = ma + (args.cl_target - mcl) / cl_alpha_deg if cl_alpha_deg else None

    def interp_at_trim(vals):
        # quadratic-free: interp linearly between bracketing sweep points
        if alpha_trim is None or not vals or len(vals) != n:
            return None
        pts = sorted(zip(alpha, vals))
        lo = max((p for p in pts if p[0] <= alpha_trim), default=pts[0])
        hi = min((p for p in pts if p[0] >= alpha_trim), default=pts[-1])
        return lo[1] if hi[0] == lo[0] else \
            lo[1] + (hi[1] - lo[1]) * (alpha_trim - lo[0]) / (hi[0] - lo[0])

    cd_at_trim = interp_at_trim(cd)
    cdo_at_trim = interp_at_trim(cdo)
    cdi_at_trim = interp_at_trim(cdi)

    gates = []
    def gate(name, ok, reason=""):
        gates.append({"gate": name, "passed": bool(ok), "reason": reason})

    gate("polar_present", True)
    gate("lift_slope_sane", 0.02 <= cl_alpha_deg <= 0.15,
         f"CL_alpha={cl_alpha_deg:.4f} /deg (expect ~0.05-0.09)")
    gate("trim_in_sweep", alpha_trim is not None
         and args.alpha_start - 2 <= alpha_trim <= args.alpha_end + 2,
         f"alpha_trim={alpha_trim if alpha_trim is None else round(alpha_trim, 2)} deg")

    result = {
        "polar": [{"alpha": a, "CL": c, "CD": (cd[i] if i < len(cd) else None),
                   "CM": (cm[i] if i < len(cm) else None)}
                  for i, (a, c) in enumerate(zip(alpha, cl))],
        "cl_alpha_per_deg": cl_alpha_deg,
        "dcm_dcl": dcm_dcl,
        "x_np_m": x_np,
        "alpha_trim_deg": alpha_trim,
        "cd_at_trim": cd_at_trim,
        "cdo_at_trim": cdo_at_trim,
        "cdi_at_trim": cdi_at_trim,
        "ld_at_trim": (args.cl_target / cd_at_trim) if cd_at_trim else None,
        "cl_target": args.cl_target,
        "sref": args.sref, "cref": args.cref, "bref": args.bref,
        "gates": gates,
    }
    with open(os.path.join(args.outdir, "fast_aero.json"), "w") as f:
        json.dump(result, f, indent=2)

    ok = all(g["passed"] for g in gates)
    print(f"FAST-AERO {'PASS' if ok else 'REJECT'}: CL_alpha={cl_alpha_deg:.4f}/deg "
          f"alpha_trim={alpha_trim if alpha_trim is None else round(alpha_trim,2)} deg "
          f"L/D_trim={result['ld_at_trim'] if result['ld_at_trim'] is None else round(result['ld_at_trim'],1)} "
          f"x_np={x_np:.3f} m")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
