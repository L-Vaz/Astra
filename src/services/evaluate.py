"""Evaluator service — judges whether a *successful* CFD run is actually good.

The existing Foam-Agent reviewer loop only fires on execution errors; a clean
solver exit goes straight to END. This service closes that gap: deterministic
extraction of force coefficients / residuals / yPlus from postProcessing/,
convergence + plausibility + target gates, a visual report for every run, and
a routing verdict:

    pass             -> accept into dataset / show user
    revise-numerics  -> not converged: refine mesh / schemes / more iterations
    revise-design    -> converged & plausible but misses the mission target
    reject           -> implausible physics or missing outputs; never trained on
"""
import glob
import json
import math
import os
import re
import subprocess
from typing import List, Optional

from domain_packs.bwb.schema import GateResult
from services.flow_conditions import FlowConditions

FOAM_BASHRC = os.environ.get("FOAM_BASHRC", "/opt/openfoam10/etc/bashrc")


# ---------------------------------------------------------------- extraction

def _read_dat(path: str) -> tuple:
    """OpenFOAM function-object .dat -> (colnames, rows)."""
    cols, rows = [], []
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("#"):
                toks = line[1:].split()
                if len(toks) > 1:
                    cols = toks
                continue
            toks = line.split()
            try:
                rows.append([float(x) for x in toks])
            except ValueError:
                continue
    return cols, rows


def find_force_coeffs(case_dir: str) -> Optional[dict]:
    """Find and parse a forceCoeffs-style .dat anywhere under postProcessing."""
    for path in sorted(glob.glob(os.path.join(case_dir, "postProcessing", "**", "*.dat"),
                                 recursive=True)):
        cols, rows = _read_dat(path)
        if not rows:
            continue
        low = [c.lower() for c in cols]
        if any(c.startswith("cl") for c in low) and any(c.startswith("cd") for c in low):
            def col(prefix):
                for i, c in enumerate(low):
                    if c == prefix or c.startswith(prefix + "("):
                        return [r[i] for r in rows if len(r) == len(cols)]
                return None
            return {"path": path,
                    "time": [r[0] for r in rows if len(r) == len(cols)],
                    "cl": col("cl"), "cd": col("cd"), "cm": col("cm")}
    return None


def parse_residuals(case_dir: str) -> dict:
    """Initial-residual histories per field, parsed from the solver log."""
    logs = [p for p in glob.glob(os.path.join(case_dir, "*"))
            if os.path.basename(p) in ("Allrun.out", "log.simpleFoam")
            or os.path.basename(p).startswith("log.")]
    hist = {}
    rx = re.compile(r"Solving for (\w+), Initial residual = ([\deE.+-]+)")
    for lp in logs:
        with open(lp, errors="replace") as f:
            for m in rx.finditer(f.read()):
                hist.setdefault(m.group(1), []).append(float(m.group(2)))
    return hist


def parse_yplus(case_dir: str) -> Optional[dict]:
    for path in sorted(glob.glob(os.path.join(case_dir, "postProcessing", "**", "yPlus.dat"),
                                 recursive=True)):
        # v10 rows: "<time> <patchWord> <min> <max> <average>" — the patch
        # name is a word, so parse tokens individually instead of _read_dat
        last = None
        with open(path, errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                nums = []
                for tok in line.split():
                    try:
                        nums.append(float(tok))
                    except ValueError:
                        continue
                if len(nums) >= 4:
                    last = nums
        if last:
            return {"min": last[-3], "max": last[-2], "avg": last[-1]}
    return None


# ---------------------------------------------------------------- evaluation

def _tail_stats(series: List[float], frac: float = 0.25) -> tuple:
    n = max(3, int(len(series) * frac))
    tail = series[-n:]
    mean = sum(tail) / len(tail)
    var = sum((x - mean) ** 2 for x in tail) / len(tail)
    return mean, math.sqrt(var)


def _posthoc_function_objects(case_dir: str, ref: dict, warnings: List[str]) -> None:
    """The input writer sometimes omits forceCoeffs/yPlus function objects.
    Inject them into controlDict and recompute from the saved fields with
    `simpleFoam -postProcess` — deterministic, works on any finished case."""
    import math as _m
    a = _m.radians(ref["alpha_deg"])
    fo = (
        "{ astraForceCoeffs { type forceCoeffs; libs (\"libforces.so\"); "
        "patches (aircraft); log yes; writeControl timeStep; writeInterval 1; "
        f"magUInf {ref['magUInf']}; rho rhoInf; rhoInf {ref['rho']}; "
        f"liftDir ({-_m.sin(a):.6f} 0 {_m.cos(a):.6f}); "
        f"dragDir ({_m.cos(a):.6f} 0 {_m.sin(a):.6f}); "
        "pitchAxis (0 1 0); "
        f"CofR ({ref.get('cofr_x', 0.0):.6f} 0 0); "
        f"Aref {ref['sref']}; lRef {ref['cref']}; " + "} "
        "astraYPlus { type yPlus; libs (\"libfieldFunctionObjects.so\"); "
        "writeControl writeTime; } }")
    cmds = [
        f"foamDictionary system/controlDict -entry functions -set '{fo}'",
        "simpleFoam -postProcess -latestTime",
    ]
    for cmd in cmds:
        proc = subprocess.run(
            ["bash", "-lc", f"source {FOAM_BASHRC} >/dev/null 2>&1; {cmd}"],
            cwd=case_dir, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            warnings.append(f"post-hoc function objects: `{cmd.split()[0]}` failed: "
                            f"{(proc.stderr or proc.stdout)[-200:]}")
            return


def evaluate_case(case_dir: str, outdir: str,
                  required_cl: Optional[float] = None,
                  vlm_cl: Optional[float] = None,
                  extra_slices: Optional[List[str]] = None,
                  ref: Optional[dict] = None,
                  flow_conditions: Optional[FlowConditions] = None) -> dict:
    """Run all result gates + produce the visual report. Never raises on gate
    failure — returns the report with a verdict (the caller routes on it).

    ref (optional): {sref, cref, magUInf, rho, alpha_deg, cofr_x} — enables
    post-hoc recomputation of force coefficients when the case is missing the
    function-object output."""
    os.makedirs(outdir, exist_ok=True)
    gates: List[GateResult] = []
    warnings: List[str] = []
    metrics = {}

    fc = find_force_coeffs(case_dir)
    if fc is None and ref:
        warnings.append("forceCoeffs output missing — recomputed post-hoc from saved fields")
        _posthoc_function_objects(case_dir, ref, warnings)
        fc = find_force_coeffs(case_dir)
    gates.append(GateResult(gate="force_coeffs_present", passed=fc is not None,
                            reason="" if fc else "no forceCoeffs .dat under postProcessing/"))

    residuals = parse_residuals(case_dir)
    gates.append(GateResult(gate="residual_history_present", passed=bool(residuals),
                            reason="" if residuals else "no residual lines in solver logs"))

    converged_forces = converged_resid = plausible = on_target = None
    if fc and fc["cl"]:
        cl_mean, cl_std = _tail_stats(fc["cl"])
        cd_mean, cd_std = _tail_stats(fc["cd"]) if fc["cd"] else (None, None)
        metrics.update({"cl": cl_mean, "cl_std_tail": cl_std,
                        "cd": cd_mean, "cd_std_tail": cd_std,
                        "cm": _tail_stats(fc["cm"])[0] if fc["cm"] else None,
                        "n_samples": len(fc["cl"])})
        if len(fc["cl"]) < 5:
            # single post-hoc sample: no history to judge — convergence is
            # decided by the residuals gate instead
            converged_forces = True
            gates.append(GateResult(
                gate="forces_converged", passed=True,
                reason=f"single post-hoc sample CL={cl_mean:.4f}; "
                       "convergence judged by residuals"))
        else:
            converged_forces = cl_std <= max(0.01, 0.05 * abs(cl_mean))
            gates.append(GateResult(
                gate="forces_converged", passed=converged_forces,
                reason=f"CL tail mean {cl_mean:.4f} ± {cl_std:.4f}"))
        if vlm_cl is not None:
            # generous by design: tier-2 here is a coarse wall-function tet
            # mesh (y+ can be O(100-1000)) judged against an inviscid VLM —
            # the meaningful check at this fidelity is sign + order of
            # magnitude, not percent agreement
            band = max(0.15, 0.6 * abs(vlm_cl))
            plausible = abs(cl_mean - vlm_cl) <= band
            gates.append(GateResult(
                gate="plausible_vs_vspaero", passed=plausible,
                reason=f"CFD CL {cl_mean:.3f} vs VLM {vlm_cl:.3f} (band ±{band:.3f})"))
        if required_cl:
            on_target = abs(cl_mean - required_cl) <= 0.25 * abs(required_cl)
            gates.append(GateResult(
                gate="meets_cl_target", passed=on_target,
                reason=f"CL {cl_mean:.3f} vs required {required_cl:.3f} (±25%)"))

    if residuals:
        finals = {f: h[-1] for f, h in residuals.items() if h}
        metrics["final_residuals"] = finals
        worst = max((v for k, v in finals.items() if k.lower().startswith(("ux", "uz", "p"))),
                    default=1.0)
        converged_resid = worst < 5e-3
        gates.append(GateResult(gate="residuals_converged", passed=converged_resid,
                                reason=f"worst final initial-residual {worst:.2e} (< 5e-3)"))

    yp = parse_yplus(case_dir)
    if yp:
        yp = dict(yp)
        if flow_conditions is not None:
            atmosphere = flow_conditions.atmosphere
            yp.update({
                "kinematic_viscosity_m2s": atmosphere.kinematic_viscosity_m2s,
                "atmosphere_source": atmosphere.source,
                "atmosphere_dataset_sha256": atmosphere.dataset_sha256,
            })
        metrics["yplus"] = yp
        if not (5 <= yp["avg"] <= 500):
            warnings.append(f"average y+ {yp['avg']:.0f} outside wall-function range 5-500")
    else:
        warnings.append("no yPlus output found (aircraft patch not wall, or FO missing)")

    # ------------------------------------------------ verdict routing
    if not fc:
        verdict = "reject"
    elif converged_forces is False or converged_resid is False:
        verdict = "revise-numerics"
    elif plausible is False:
        verdict = "reject"
    elif on_target is False:
        verdict = "revise-design"
    else:
        verdict = "pass"

    artifacts = {}
    try:
        artifacts.update(_plots(fc, residuals, outdir))
    except Exception as e:
        warnings.append(f"history plots failed: {e!r}")
    try:
        artifacts.update(_field_renders(case_dir, outdir, extra_slices or []))
    except Exception as e:
        warnings.append(f"field renders failed: {e!r}")

    report = {"case_dir": case_dir, "verdict": verdict, "metrics": metrics,
              "gates": [g.model_dump() for g in gates], "warnings": warnings,
              "artifacts": artifacts,
              "flow_conditions": (
                  flow_conditions.model_dump(mode="json")
                  if flow_conditions is not None else None
              )}
    with open(os.path.join(outdir, "evaluation.json"), "w") as f:
        json.dump(report, f, indent=2)
    return report


# ---------------------------------------------------------------- visuals

def _plots(fc, residuals, outdir) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    out = {}
    if fc and fc["cl"]:
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.plot(fc["time"], fc["cl"], label="CL")
        if fc["cd"]:
            ax.plot(fc["time"], fc["cd"], label="CD")
        if fc["cm"]:
            ax.plot(fc["time"], fc["cm"], label="CM")
        ax.set_xlabel("iteration"); ax.set_ylabel("coefficient")
        ax.legend(); ax.grid(alpha=0.3); ax.set_title("Force coefficients")
        p = os.path.join(outdir, "force_coeffs.png")
        fig.savefig(p, dpi=100, bbox_inches="tight"); plt.close(fig)
        out["render_force_coeffs"] = p
    if residuals:
        fig, ax = plt.subplots(figsize=(7, 4))
        for fld, h in residuals.items():
            ax.semilogy(h, label=fld)
        ax.set_xlabel("solve step"); ax.set_ylabel("initial residual")
        ax.legend(fontsize=8); ax.grid(alpha=0.3); ax.set_title("Residuals")
        p = os.path.join(outdir, "residuals.png")
        fig.savefig(p, dpi=100, bbox_inches="tight"); plt.close(fig)
        out["render_residuals"] = p
    return out


def _field_renders(case_dir, outdir, extra_slices) -> dict:
    # pick the latest time directory that actually contains a U field —
    # function objects can create field-less time dirs (e.g. only yPlus),
    # which would make -latestTime convert a time with nothing to render
    times = []
    for d in os.listdir(case_dir):
        if d.replace(".", "", 1).isdigit() and \
                os.path.exists(os.path.join(case_dir, d, "U")):
            times.append(float(d) if "." in d else int(d))
    if not times:
        raise RuntimeError("no time directory with a U field to render")
    t = max(times)
    conv = subprocess.run(
        ["bash", "-lc", f"source {FOAM_BASHRC} >/dev/null 2>&1; foamToVTK -time {t}"],
        cwd=case_dir, capture_output=True, text=True, timeout=900)
    if conv.returncode != 0:
        raise RuntimeError(f"foamToVTK failed: {conv.stderr[-150:]}")
    import pyvista as pv
    out = {}
    vols = sorted(glob.glob(os.path.join(case_dir, "VTK", f"*_{t}.vtk")))
    vols = [v for v in vols if os.path.isfile(v)]
    if not vols:
        raise RuntimeError(f"foamToVTK produced no volume file for time {t}")
    m = pv.read(vols[-1])
    if "U" not in m.array_names:
        raise RuntimeError(f"no U array in {os.path.basename(vols[-1])}")
    for normal in ["y"] + extra_slices:
        sl = m.slice(normal=normal, origin=(0, 1e-4, 1e-4))
        pl = pv.Plotter(off_screen=True)
        pl.add_mesh(sl, scalars="U", show_edges=False)
        (pl.view_xz if normal == "y" else pl.view_xy)()
        pl.camera.zoom(1.3)
        p = os.path.join(outdir, f"U_slice_{normal}.png")
        pl.screenshot(p); pl.close()
        out[f"render_U_slice_{normal}"] = p
    surf = sorted(glob.glob(os.path.join(case_dir, "VTK", "aircraft", "*.vtk")))
    if surf:
        s = pv.read(surf[-1])
        if "p" in s.array_names:
            pl = pv.Plotter(off_screen=True)
            pl.add_mesh(s, scalars="p")
            pl.view_isometric()
            p = os.path.join(outdir, "surface_pressure.png")
            pl.screenshot(p); pl.close()
            out["render_surface_pressure"] = p
    return out
