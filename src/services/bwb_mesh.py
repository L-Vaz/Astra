"""Mesh stage service — deterministic farfield mesh for the BWB, reviewable.

Pipeline: gmsh_farfield.py (deterministic, no LLM) -> .msh -> scratch OpenFOAM
conversion (gmshToFoam + aircraft patch set to wall) -> checkMesh gates ->
foamToVTK -> PyVista review renders.

The .msh is the artifact handed to the existing Foam-Agent graph via its
custom-mesh path; the scratch case here exists only for validation/review.
"""
import json
import os
import re
import subprocess
import sys
from typing import List, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from domain_packs.bwb.schema import GateResult  # noqa: E402

_MESHER = os.path.join(_REPO_ROOT, "domain_packs", "bwb", "gmsh_farfield.py")
FOAM_BASHRC = os.environ.get("FOAM_BASHRC", "/opt/openfoam10/etc/bashrc")

_MIN_CELLS = 20_000
EXPECTED_PATCHES = ["inlet", "outlet", "farfield", "aircraft"]


class MeshRejected(Exception):
    def __init__(self, message: str, report: Optional[dict] = None):
        super().__init__(message)
        self.report = report


def _foam(cmd: str, cwd: str, timeout_s: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-lc", f"source {FOAM_BASHRC} >/dev/null 2>&1; {cmd}"],
        cwd=cwd, capture_output=True, text=True, timeout=timeout_s)


_CONTROLDICT = """FoamFile
{
    format ascii;
    class dictionary;
    object controlDict;
}
application simpleFoam;
startFrom startTime;
startTime 0;
stopAt endTime;
endTime 1;
deltaT 1;
writeControl timeStep;
writeInterval 1;
"""


def generate_mesh(stl_path: str, mac_m: float, span_m: float, outdir: str,
                  level: str = "coarse", max_cells: float = 3e6,
                  timeout_s: int = 1800) -> dict:
    """Build + validate the farfield mesh. Returns mesh report dict; raises
    MeshRejected when a gate fails (reason codes in report['gates'])."""
    os.makedirs(outdir, exist_ok=True)
    gates: List[GateResult] = []
    report = {"level": level, "gates": [], "artifacts": {}, "stats": {}}

    def reject(msg):
        report["gates"] = [g.model_dump() for g in gates]
        with open(os.path.join(outdir, "mesh_report.json"), "w") as f:
            json.dump(report, f, indent=2)
        raise MeshRejected(msg, report=report)

    # 1) deterministic gmsh farfield mesh
    msh_path = os.path.join(outdir, "bwb.msh")
    proc = subprocess.run(
        [sys.executable, _MESHER, "--stl", stl_path, "--mac", f"{mac_m:.6f}",
         "--span", f"{span_m:.6f}", "--out", msh_path, "--level", level,
         "--max-cells", f"{max_cells:.0f}"],
        capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0 or not os.path.exists(msh_path):
        tail = [l for l in proc.stdout.splitlines() if "MESH REJECT" in l]
        gates.append(GateResult(gate="gmsh_generate", passed=False,
                                reason=tail[-1] if tail else proc.stderr[-300:]))
        reject(f"mesh generation failed — {gates[-1].reason}")
    gates.append(GateResult(gate="gmsh_generate", passed=True))
    with open(os.path.join(outdir, "bwb_meshinfo.json")) as f:
        report["stats"].update(json.load(f))
    report["artifacts"]["msh"] = msh_path

    # 2) scratch conversion exactly as the Foam-Agent graph will do it
    case = os.path.join(outdir, "check_case")
    os.makedirs(os.path.join(case, "system"), exist_ok=True)
    with open(os.path.join(case, "system", "controlDict"), "w") as f:
        f.write(_CONTROLDICT)
    subprocess.run(["cp", msh_path, os.path.join(case, "bwb.msh")], check=True)
    conv = _foam("gmshToFoam bwb.msh", case)
    boundary_file = os.path.join(case, "constant", "polyMesh", "boundary")
    ok = conv.returncode == 0 and os.path.exists(boundary_file)
    gates.append(GateResult(gate="gmshToFoam", passed=ok,
                            reason="" if ok else conv.stderr[-300:]))
    if not ok:
        reject("gmshToFoam conversion failed")

    with open(boundary_file) as f:
        btxt = f.read()
    missing = [p for p in EXPECTED_PATCHES if p not in btxt]
    gates.append(GateResult(gate="patches_present", passed=not missing,
                            reason=f"missing: {missing}" if missing else ""))
    if missing:
        reject(f"expected patches missing from converted mesh: {missing}")

    # aircraft must be a wall for wall functions / yPlus — same command the
    # enriched requirement instructs the graph's Allrun to run
    wallfix = _foam(
        "foamDictionary constant/polyMesh/boundary -entry entry0/aircraft/type -set wall",
        case)
    gates.append(GateResult(gate="aircraft_wall_type", passed=wallfix.returncode == 0,
                            reason=wallfix.stderr[-200:] if wallfix.returncode else ""))

    # 3) checkMesh gates
    chk = _foam("checkMesh", case)
    log = chk.stdout
    with open(os.path.join(outdir, "checkMesh.log"), "w") as f:
        f.write(log + chk.stderr)
    mesh_ok = "Mesh OK" in log
    m_cells = re.search(r"cells:\s+(\d+)", log)
    m_ortho = re.search(r"non-orthogonality Max:\s*([\d.]+)", log)
    m_skew = re.search(r"skewness\s*=\s*([\d.]+)", log)
    n_cells = int(m_cells.group(1)) if m_cells else 0
    report["stats"].update({
        "cells": n_cells,
        "max_non_orthogonality": float(m_ortho.group(1)) if m_ortho else None,
        "max_skewness": float(m_skew.group(1)) if m_skew else None,
    })
    gates.append(GateResult(gate="checkMesh", passed=mesh_ok,
                            reason="" if mesh_ok else "checkMesh reported failures (see checkMesh.log)"))
    gates.append(GateResult(
        gate="cell_count_sane", passed=_MIN_CELLS <= n_cells <= max_cells,
        reason=f"{n_cells} cells (bounds {_MIN_CELLS}..{max_cells:.0f})"))
    if not mesh_ok or not (_MIN_CELLS <= n_cells <= max_cells):
        reject("mesh quality/count gate failed")

    # 4) review renders
    try:
        report["artifacts"].update(_render(case, outdir))
        gates.append(GateResult(gate="renders", passed=True))
    except Exception as e:
        gates.append(GateResult(gate="renders", passed=False, reason=repr(e)[:200]))

    report["gates"] = [g.model_dump() for g in gates]
    report["passed"] = all(g.passed for g in gates)
    with open(os.path.join(outdir, "mesh_report.json"), "w") as f:
        json.dump(report, f, indent=2)
    if not report["passed"]:
        raise MeshRejected("mesh gates failed", report=report)
    return report


def _render(case: str, outdir: str) -> dict:
    conv = _foam("foamToVTK -constant", case)
    if conv.returncode != 0:
        raise RuntimeError(f"foamToVTK failed: {conv.stderr[-200:]}")
    import glob

    import pyvista as pv
    out = {}
    vols = glob.glob(os.path.join(case, "VTK", "*_0.vtk"))
    if vols:
        m = pv.read(vols[0])
        for name, normal, viewer in (("mesh_slice_y", "y", "view_xz"),
                                     ("mesh_slice_z", "z", "view_xy")):
            sl = m.slice(normal=normal, origin=(0, 1e-4, 1e-4))
            pl = pv.Plotter(off_screen=True)
            pl.add_mesh(sl, show_edges=True, color="lightsteelblue", edge_color="gray")
            getattr(pl, viewer)()
            pl.camera.zoom(1.3)
            path = os.path.join(outdir, f"{name}.png")
            pl.screenshot(path)
            pl.close()
            out[f"render_{name}"] = path
    surf = glob.glob(os.path.join(case, "VTK", "aircraft", "*.vtk"))
    if surf:
        s = pv.read(surf[0])
        pl = pv.Plotter(off_screen=True)
        pl.add_mesh(s, show_edges=True, color="steelblue", edge_color="black")
        pl.view_isometric()
        path = os.path.join(outdir, "mesh_aircraft_surface.png")
        pl.screenshot(path)
        pl.close()
        out["render_mesh_aircraft_surface"] = path
    return out
