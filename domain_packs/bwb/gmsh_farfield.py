"""BWB domain pack — deterministic external-aero farfield mesh via Gmsh.

Takes the watertight STL from the geometry stage, builds a box farfield around
it, and writes a Gmsh v2.2 .msh with physical groups (inlet / outlet /
farfield / aircraft / internal) ready for Foam-Agent's custom-mesh path
(gmshToFoam). Runs in the FoamAgent conda env (gmsh installed there).

CLI:
    python gmsh_farfield.py --stl bwb.stl --mac 0.48 --span 2.29 \
        --out bwb.msh [--level coarse|medium|fine] [--max-cells 3e6]
"""
import argparse
import json
import math
import os
import sys

import gmsh

LEVELS = {
    # near-surface size, far size, as fractions of MAC / span
    "coarse": {"near_mac": 0.055, "far_span": 0.28},
    "medium": {"near_mac": 0.035, "far_span": 0.20},
    "fine":   {"near_mac": 0.022, "far_span": 0.14},
}


def build(stl: str, mac: float, span: float, out: str, level: str,
          max_cells: float) -> dict:
    near = LEVELS[level]["near_mac"] * mac
    far = LEVELS[level]["far_span"] * span

    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 1)
    gmsh.merge(stl)

    # Use the watertight STL triangles directly as the boundary mesh (no
    # surface remeshing — reparametrization creates overlapping facets at the
    # sharp trailing edge). createTopology makes the discrete surface usable
    # as a volume boundary.
    gmsh.model.mesh.removeDuplicateNodes()
    gmsh.model.mesh.createTopology()
    aircraft_surfs = [s[1] for s in gmsh.model.getEntities(2)]
    if not aircraft_surfs:
        raise RuntimeError("STL import produced no surfaces")
    aircraft_loop = gmsh.model.geo.addSurfaceLoop(aircraft_surfs)

    # farfield box: 8*MAC upstream/lateral/vertical, 20*MAC downstream
    bb = gmsh.model.getBoundingBox(-1, -1)
    x0, y0, z0, x1, y1, z1 = bb
    up, down, lat = 8 * mac, 20 * mac, max(8 * mac, 0.75 * span)
    bx0, bx1 = x0 - up, x1 + down
    by0, by1 = y0 - lat, y1 + lat
    bz0, bz1 = z0 - lat, z1 + lat

    g = gmsh.model.geo
    p = [g.addPoint(bx0, by0, bz0), g.addPoint(bx1, by0, bz0),
         g.addPoint(bx1, by1, bz0), g.addPoint(bx0, by1, bz0),
         g.addPoint(bx0, by0, bz1), g.addPoint(bx1, by0, bz1),
         g.addPoint(bx1, by1, bz1), g.addPoint(bx0, by1, bz1)]
    # bottom loop, top loop, verticals
    lb = [g.addLine(p[0], p[1]), g.addLine(p[1], p[2]),
          g.addLine(p[2], p[3]), g.addLine(p[3], p[0])]
    lt = [g.addLine(p[4], p[5]), g.addLine(p[5], p[6]),
          g.addLine(p[6], p[7]), g.addLine(p[7], p[4])]
    lv = [g.addLine(p[0], p[4]), g.addLine(p[1], p[5]),
          g.addLine(p[2], p[6]), g.addLine(p[3], p[7])]

    def plane(lines):
        return g.addPlaneSurface([g.addCurveLoop(lines)])

    s_bottom = plane([lb[0], lb[1], lb[2], lb[3]])
    s_top = plane([lt[0], lt[1], lt[2], lt[3]])
    s_front = plane([lb[0], lv[1], -lt[0], -lv[0]])   # y = by0
    s_back = plane([lb[2], lv[3], -lt[2], -lv[2]])    # y = by1
    s_inlet = plane([lb[3], lv[0], -lt[3], -lv[3]])   # x = bx0
    s_outlet = plane([lb[1], lv[2], -lt[1], -lv[1]])  # x = bx1

    box_surfs = [s_bottom, s_top, s_front, s_back, s_inlet, s_outlet]
    box_loop = g.addSurfaceLoop(box_surfs)
    g.addVolume([box_loop, aircraft_loop])
    g.synchronize()

    vol = gmsh.model.getEntities(3)[0][1]
    gmsh.model.addPhysicalGroup(2, [s_inlet], name="inlet")
    gmsh.model.addPhysicalGroup(2, [s_outlet], name="outlet")
    gmsh.model.addPhysicalGroup(2, [s_bottom, s_top, s_front, s_back], name="farfield")
    gmsh.model.addPhysicalGroup(2, aircraft_surfs, name="aircraft")
    gmsh.model.addPhysicalGroup(3, [vol], name="internal")

    # size: near-surface refinement decaying to farfield
    f_dist = gmsh.model.mesh.field.add("Distance")
    gmsh.model.mesh.field.setNumbers(f_dist, "SurfacesList", aircraft_surfs)
    gmsh.model.mesh.field.setNumber(f_dist, "Sampling", 120)
    f_thr = gmsh.model.mesh.field.add("Threshold")
    gmsh.model.mesh.field.setNumber(f_thr, "InField", f_dist)
    gmsh.model.mesh.field.setNumber(f_thr, "SizeMin", near)
    gmsh.model.mesh.field.setNumber(f_thr, "SizeMax", far)
    gmsh.model.mesh.field.setNumber(f_thr, "DistMin", 2 * near)
    gmsh.model.mesh.field.setNumber(f_thr, "DistMax", 0.8 * span)
    gmsh.model.mesh.field.setAsBackgroundMesh(f_thr)
    gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
    gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
    # Delaunay: slower than HXT but deterministic and robust — HXT showed
    # intermittent silent failures (0 tets) under system load
    gmsh.option.setNumber("Mesh.Algorithm3D", 1)

    gmsh.model.mesh.generate(3)
    gmsh.model.mesh.optimize("")
    # Netgen pass cuts avg non-orthogonality ~22->18 and max skewness ~2.6->1.5;
    # without it the raw Delaunay tets cannot hold a steady RANS solution
    gmsh.model.mesh.optimize("Netgen")

    _, elem_tags, _ = gmsh.model.mesh.getElements(3)
    n_cells = sum(len(t) for t in elem_tags)
    if n_cells == 0:
        gmsh.finalize()
        raise RuntimeError("volume meshing produced 0 cells")
    if n_cells > max_cells:
        gmsh.finalize()
        raise RuntimeError(
            f"cell budget exceeded: {n_cells:.0f} > {max_cells:.0f} — coarsen the level")

    gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
    gmsh.write(out)
    gmsh.finalize()
    return {"n_cells": int(n_cells), "near_size_m": near, "far_size_m": far,
            "level": level, "box": [bx0, by0, bz0, bx1, by1, bz1],
            "patches": ["inlet", "outlet", "farfield", "aircraft"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stl", required=True)
    ap.add_argument("--mac", type=float, required=True)
    ap.add_argument("--span", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--level", choices=list(LEVELS), default="coarse")
    ap.add_argument("--max-cells", type=float, default=3e6)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    try:
        info = build(args.stl, args.mac, args.span, args.out, args.level,
                     args.max_cells)
    except Exception as e:
        print(f"MESH REJECT: {e}")
        return 1
    with open(os.path.splitext(args.out)[0] + "_meshinfo.json", "w") as f:
        json.dump(info, f, indent=2)
    print(f"MESH PASS: {info['n_cells']} cells (level={args.level}, "
          f"near={info['near_size_m']*1000:.1f} mm, far={info['far_size_m']*1000:.0f} mm)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
