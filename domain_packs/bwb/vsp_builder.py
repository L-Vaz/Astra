"""BWB domain pack — OpenVSP geometry builder.

Runs under the dedicated OpenVSP venv (py3.10, /opt/vsp-venv), NOT the FoamAgent
conda env: the compiled _vsp.so binding is version-locked to python 3.10.

CLI:
    /opt/vsp-venv/bin/python vsp_builder.py --params params.json --outdir DIR

Reads a BWBParams JSON, builds a two-panel blended wing in OpenVSP, exports
bwb.vsp3 / bwb.stl / bwb_degen.csv, renders 4 PNG views from the STL, runs
mesh-level checks (watertight, bbox), and writes geometry_report.json.
Exit code 0 iff all gates pass.
"""
import argparse
import json
import math
import os
import sys

import numpy as np

# schema.py lives next to this file; make it importable when run by path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bwb.schema import BWBParams, GateResult, GeometryReport  # noqa: E402

import openvsp as vsp  # noqa: E402


def set_parm(gid: str, name: str, group: str, val: float, warnings: list) -> None:
    pid = vsp.GetParm(gid, name, group)
    if not pid:
        warnings.append(f"parm not found: {group}/{name} (skipped)")
        return
    vsp.SetParmVal(pid, val)


def build_bwb(p: BWBParams, outdir: str, warnings: list) -> dict:
    vsp.VSPCheckSetup()
    vsp.ClearVSPModel()

    wid = vsp.AddGeom("WING", "")
    vsp.SetGeomName(wid, "bwb")
    # two panels: XSec_1 = centre body, XSec_2 = outer wing
    vsp.InsertXSec(wid, 1, vsp.XS_FOUR_SERIES)

    for sec in (1, 2):
        vsp.SetDriverGroup(wid, sec, vsp.SPAN_WSECT_DRIVER,
                           vsp.ROOTC_WSECT_DRIVER, vsp.TIPC_WSECT_DRIVER)

    c_root, c_mid, c_tip = p.chords()
    b1, b2 = p.half_widths()
    mid_twist = p.root_twist_deg + (p.tip_twist_deg - p.root_twist_deg) * p.centre_body_fraction

    set_parm(wid, "Span", "XSec_1", b1, warnings)
    set_parm(wid, "Root_Chord", "XSec_1", c_root, warnings)
    set_parm(wid, "Tip_Chord", "XSec_1", c_mid, warnings)
    set_parm(wid, "Sweep", "XSec_1", p.sweep_inner_deg, warnings)
    set_parm(wid, "Dihedral", "XSec_1", p.dihedral_deg, warnings)

    set_parm(wid, "Span", "XSec_2", b2, warnings)
    set_parm(wid, "Root_Chord", "XSec_2", c_mid, warnings)
    set_parm(wid, "Tip_Chord", "XSec_2", c_tip, warnings)
    set_parm(wid, "Sweep", "XSec_2", p.sweep_outer_deg, warnings)
    set_parm(wid, "Dihedral", "XSec_2", p.dihedral_deg, warnings)

    # twist: XSec_0 = root incidence, XSec_1 = blend station, XSec_2 = tip
    set_parm(wid, "Twist", "XSec_0", p.root_twist_deg, warnings)
    set_parm(wid, "Twist", "XSec_1", mid_twist, warnings)
    set_parm(wid, "Twist", "XSec_2", p.tip_twist_deg, warnings)

    # airfoil thickness (NACA 4-series t/c), root -> blend -> tip
    mid_thick = (p.root_thickness_pct + p.tip_thickness_pct) / 2.0
    set_parm(wid, "ThickChord", "XSecCurve_0", p.root_thickness_pct / 100.0, warnings)
    set_parm(wid, "ThickChord", "XSecCurve_1", mid_thick / 100.0, warnings)
    set_parm(wid, "ThickChord", "XSecCurve_2", p.tip_thickness_pct / 100.0, warnings)

    # tessellation + closed tip cap so the STL can be watertight
    set_parm(wid, "Tess_W", "Shape", 41, warnings)
    set_parm(wid, "SectTess_U", "XSec_1", 12, warnings)
    set_parm(wid, "SectTess_U", "XSec_2", 18, warnings)
    set_parm(wid, "CapUMaxOption", "EndCap", vsp.ROUND_END_CAP, warnings)

    vsp.Update()

    paths = {
        "vsp3": os.path.join(outdir, "bwb.vsp3"),
        "stl": os.path.join(outdir, "bwb.stl"),
        "degen_csv": os.path.join(outdir, "bwb_degen.csv"),
    }
    vsp.ExportFile(paths["stl"], vsp.SET_ALL, vsp.EXPORT_STL)
    vsp.WriteVSPFile(paths["vsp3"], vsp.SET_ALL)
    vsp.SetComputationFileName(vsp.DEGEN_GEOM_CSV_TYPE, paths["degen_csv"])
    vsp.ComputeDegenGeom(vsp.SET_ALL, vsp.DEGEN_GEOM_CSV_TYPE)

    try:
        em = vsp.ErrorMgrSingleton.getInstance()
        for _ in range(em.GetNumTotalErrors()):
            warnings.append(f"openvsp: {em.PopLastError().GetErrorString()}")
    except AttributeError:
        pass  # error-manager API varies across bindings; not load-bearing
    return paths


def read_ascii_stl(path: str) -> np.ndarray:
    """-> (n,3,3) triangle vertices."""
    verts = []
    with open(path) as f:
        for line in f:
            s = line.split()
            if s and s[0] == "vertex":
                verts.append([float(s[1]), float(s[2]), float(s[3])])
    tri = np.array(verts, dtype=float)
    if len(tri) == 0 or len(tri) % 3:
        raise ValueError(f"malformed ASCII STL: {len(tri)} vertices")
    return tri.reshape(-1, 3, 3)


def watertight_check(tri: np.ndarray) -> tuple:
    """Every edge must be shared by exactly 2 facets. -> (watertight, open_edges)"""
    quant = np.round(tri, 6)
    vmap = {}
    def vid(v):
        key = (v[0], v[1], v[2])
        if key not in vmap:
            vmap[key] = len(vmap)
        return vmap[key]
    edge_count = {}
    for t in quant:
        ids = [vid(t[0]), vid(t[1]), vid(t[2])]
        for a, b in ((0, 1), (1, 2), (2, 0)):
            e = (min(ids[a], ids[b]), max(ids[a], ids[b]))
            edge_count[e] = edge_count.get(e, 0) + 1
    bad = sum(1 for c in edge_count.values() if c != 2)
    return bad == 0, bad


def render_views(tri: np.ndarray, outdir: str) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    rdir = os.path.join(outdir, "renders")
    os.makedirs(rdir, exist_ok=True)
    sample = tri[:: max(1, len(tri) // 6000)]
    flat = tri.reshape(-1, 3)
    mid = (flat.max(axis=0) + flat.min(axis=0)) / 2.0
    half = (flat.max(axis=0) - flat.min(axis=0)).max() / 2.0 * 1.05
    views = {"iso": (25, -60), "top": (90, -90), "front": (0, -90), "side": (0, 0)}
    out = {}
    for name, (elev, azim) in views.items():
        fig = plt.figure(figsize=(8, 6))
        ax = fig.add_subplot(111, projection="3d")
        ax.add_collection3d(
            Poly3DCollection(sample, facecolor="steelblue", edgecolor="gray",
                             linewidths=0.05, alpha=0.95))
        ax.set_xlim(mid[0] - half, mid[0] + half)
        ax.set_ylim(mid[1] - half, mid[1] + half)
        ax.set_zlim(mid[2] - half, mid[2] + half)
        ax.view_init(elev=elev, azim=azim)
        ax.set_axis_off()
        path = os.path.join(rdir, f"{name}.png")
        fig.savefig(path, dpi=90, bbox_inches="tight")
        plt.close(fig)
        out[f"render_{name}"] = path
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params", required=True, help="BWBParams JSON file")
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    warnings: list = []
    gates = []

    # Gate 1: parameter schema + cross-checks (pydantic)
    try:
        with open(args.params) as f:
            p = BWBParams.model_validate_json(f.read())
        gates.append(GateResult(gate="param_ranges", passed=True))
    except Exception as e:
        gates.append(GateResult(gate="param_ranges", passed=False, reason=str(e)))
        report = {"gates": [g.model_dump() for g in gates], "warnings": warnings}
        with open(os.path.join(args.outdir, "geometry_report.json"), "w") as f:
            json.dump(report, f, indent=2)
        print("GEOMETRY REJECT: param_ranges")
        return 1

    # Build
    try:
        paths = build_bwb(p, args.outdir, warnings)
        gates.append(GateResult(gate="vsp_build", passed=True))
    except Exception as e:
        gates.append(GateResult(gate="vsp_build", passed=False, reason=repr(e)))
        report = {"params": p.model_dump(), "gates": [g.model_dump() for g in gates],
                  "warnings": warnings}
        with open(os.path.join(args.outdir, "geometry_report.json"), "w") as f:
            json.dump(report, f, indent=2)
        print("GEOMETRY REJECT: vsp_build")
        return 1

    # Gate 3: STL integrity
    tri = read_ascii_stl(paths["stl"])
    watertight, open_edges = watertight_check(tri)
    gates.append(GateResult(gate="stl_watertight", passed=watertight,
                            reason="" if watertight else f"{open_edges} non-manifold edges"))

    # Gate 4: bounding box consistent with requested planform
    flat = tri.reshape(-1, 3)
    bbox = [*flat.min(axis=0), *flat.max(axis=0)]
    span_stl = bbox[4] - bbox[1]  # y extent
    chord_stl = bbox[3] - bbox[0]  # x extent
    span_ok = abs(span_stl - p.span_m) / p.span_m < 0.05
    # x extent >= root chord (sweep pushes tip back, so >= is the right test)
    chord_ok = chord_stl >= p.root_chord_m * 0.95
    bbox_ok = bool(span_ok and chord_ok)
    gates.append(GateResult(
        gate="bbox_sanity", passed=bbox_ok,
        reason="" if bbox_ok else
        f"STL span {span_stl:.3f} vs requested {p.span_m:.3f}; x-extent {chord_stl:.3f}"))

    artifacts = dict(paths)
    artifacts.update(render_views(tri, args.outdir))

    report = GeometryReport(
        params=p,
        s_ref_m2=p.planform_area_m2(),
        mean_aero_chord_m=p.mean_aero_chord_m(),
        aspect_ratio=p.aspect_ratio(),
        bbox_m=[float(x) for x in bbox],
        n_facets=int(len(tri)),
        watertight=watertight,
        open_edges=int(open_edges),
        gates=gates,
        warnings=warnings,
        artifacts=artifacts,
    )
    with open(os.path.join(args.outdir, "geometry_report.json"), "w") as f:
        f.write(report.model_dump_json(indent=2))

    ok = report.all_gates_pass
    print(f"GEOMETRY {'PASS' if ok else 'REJECT'}: "
          f"S={report.s_ref_m2:.3f} m2 AR={report.aspect_ratio:.2f} "
          f"MAC={report.mean_aero_chord_m:.3f} m facets={report.n_facets} "
          f"watertight={watertight}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
