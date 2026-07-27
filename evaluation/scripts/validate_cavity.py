#!/usr/bin/env python3
"""Independent validation for a 2-D OpenFOAM lid-driven cavity case."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pyvista as pv


GHIA_U = np.array(
    [
        [0.0000, 0.00000], [0.0547, -0.03717], [0.0625, -0.04192],
        [0.0703, -0.04775], [0.1016, -0.06434], [0.1719, -0.10150],
        [0.2813, -0.15662], [0.4531, -0.21090], [0.5000, -0.20581],
        [0.6172, -0.13641], [0.7344, 0.00332], [0.8516, 0.23151],
        [0.9531, 0.68717], [0.9609, 0.73722], [0.9688, 0.78871],
        [0.9766, 0.84123], [1.0000, 1.00000],
    ]
)

GHIA_V = np.array(
    [
        [0.0000, 0.00000], [0.0625, 0.09233], [0.0703, 0.10091],
        [0.0781, 0.10890], [0.0938, 0.12317], [0.1563, 0.16077],
        [0.2266, 0.17507], [0.2344, 0.17527], [0.5000, 0.05454],
        [0.8047, -0.24533], [0.8594, -0.22445], [0.9063, -0.16914],
        [0.9453, -0.10313], [0.9531, -0.08864], [0.9609, -0.07391],
        [0.9688, -0.05906], [1.0000, 0.00000],
    ]
)


def load_internal(case: Path, time_value: float) -> pv.DataSet:
    reader = pv.OpenFOAMReader(str(case / f"{case.name}.foam"))
    reader.set_active_time_value(float(time_value))
    return reader.read()["internalMesh"]


def sample_line(mesh: pv.DataSet, start, end, resolution=800):
    sampled = mesh.sample_over_line(start, end, resolution=resolution)
    return sampled.points, np.asarray(sampled.point_data["U"])


def parse_solver_log(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    current_time = np.nan
    records = []
    continuity = []
    execution = []
    solver_re = re.compile(
        r"^(?:smoothSolver|DICPCG):\s+Solving for (Ux|Uy|p), "
        r"Initial residual = ([0-9.eE+-]+), Final residual = ([0-9.eE+-]+)"
    )
    cont_re = re.compile(
        r"sum local = ([0-9.eE+-]+), global = ([0-9.eE+-]+), cumulative = ([0-9.eE+-]+)"
    )
    for line in text.splitlines():
        if line.startswith("Time = "):
            current_time = float(line.split("=", 1)[1].rstrip("s"))
        match = solver_re.search(line)
        if match:
            records.append(
                {
                    "time": current_time,
                    "field": match.group(1),
                    "initial": float(match.group(2)),
                    "final": float(match.group(3)),
                }
            )
        match = cont_re.search(line)
        if match:
            continuity.append(
                {
                    "time": current_time,
                    "local": float(match.group(1)),
                    "global": float(match.group(2)),
                    "cumulative": float(match.group(3)),
                }
            )
        if line.startswith("ExecutionTime = "):
            match = re.search(r"ExecutionTime = ([0-9.eE+-]+) s", line)
            if match:
                execution.append(float(match.group(1)))
    return text, records, continuity, execution


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    case = args.case.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    mesh50 = load_internal(case, 50.0)
    mesh45 = load_internal(case, 45.0)

    vertical_points, vertical_u = sample_line(
        mesh50, (0.5, 0.0, 0.005), (0.5, 1.0, 0.005)
    )
    horizontal_points, horizontal_u = sample_line(
        mesh50, (0.0, 0.5, 0.005), (1.0, 0.5, 0.005)
    )
    y = vertical_points[:, 1]
    x = horizontal_points[:, 0]
    u_center = vertical_u[:, 0]
    v_center = horizontal_u[:, 1]
    u_ghia_interp = np.interp(GHIA_U[:, 0], y, u_center)
    v_ghia_interp = np.interp(GHIA_V[:, 0], x, v_center)
    u_error = u_ghia_interp - GHIA_U[:, 1]
    v_error = v_ghia_interp - GHIA_V[:, 1]

    with (output / "centerline_profiles.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["profile", "coordinate", "ghia_re100", "foamagent", "error"])
        for (coord, ref), value, error in zip(GHIA_U, u_ghia_interp, u_error):
            writer.writerow(["u_at_x_0.5", coord, ref, value, error])
        for (coord, ref), value, error in zip(GHIA_V, v_ghia_interp, v_error):
            writer.writerow(["v_at_y_0.5", coord, ref, value, error])

    fig, axes = plt.subplots(1, 2, figsize=(10, 4.6), constrained_layout=True)
    axes[0].plot(u_center, y, label="Foam-Agent 40×40", lw=2)
    axes[0].scatter(GHIA_U[:, 1], GHIA_U[:, 0], label="Ghia et al. Re=100", s=24)
    axes[0].set(xlabel="u / U_lid", ylabel="y / L", title="Vertical centreline")
    axes[0].grid(alpha=0.25)
    axes[1].plot(x, v_center, label="Foam-Agent 40×40", lw=2)
    axes[1].scatter(GHIA_V[:, 0], GHIA_V[:, 1], label="Ghia et al. Re=100", s=24)
    axes[1].set(xlabel="x / L", ylabel="v / U_lid", title="Horizontal centreline")
    axes[1].grid(alpha=0.25)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=2)
    fig.savefig(output / "ghia_centerlines.png", dpi=180)
    plt.close(fig)

    grid_n = 121
    gx = np.linspace(0.0, 1.0, grid_n)
    gy = np.linspace(0.0, 1.0, grid_n)
    xx, yy = np.meshgrid(gx, gy)
    points = np.column_stack([xx.ravel(), yy.ravel(), np.full(xx.size, 0.005)])
    sampled_grid = pv.PolyData(points).sample(mesh50)
    grid_u = np.asarray(sampled_grid.point_data["U"]).reshape(grid_n, grid_n, 3)
    ux = grid_u[:, :, 0]
    uy = grid_u[:, :, 1]
    speed = np.hypot(ux, uy)
    interior = (xx > 0.15) & (xx < 0.85) & (yy > 0.15) & (yy < 0.90)
    interior_idx = np.argmin(np.where(interior, speed, np.inf))
    vortex_y, vortex_x = np.unravel_index(interior_idx, speed.shape)

    fig, ax = plt.subplots(figsize=(6.4, 5.4), constrained_layout=True)
    contour = ax.contourf(xx, yy, speed, levels=30, cmap="viridis")
    ax.streamplot(gx, gy, ux, uy, color="white", density=1.5, linewidth=0.65)
    ax.plot(xx[vortex_y, vortex_x], yy[vortex_y, vortex_x], "r+", ms=10, mew=2)
    ax.set(xlabel="x / L", ylabel="y / L", title="Independent velocity magnitude and streamlines")
    ax.set_aspect("equal")
    fig.colorbar(contour, ax=ax, label="|U| / U_lid")
    fig.savefig(output / "velocity_streamlines.png", dpi=180)
    plt.close(fig)

    u50_cell = np.asarray(mesh50.cell_data["U"])
    u45_cell = np.asarray(mesh45.cell_data["U"])
    delta = u50_cell - u45_cell
    relative_l2 = float(np.linalg.norm(delta) / np.linalg.norm(u50_cell))
    max_delta = float(np.max(np.linalg.norm(delta, axis=1)))

    log_text, residuals, continuity, execution = parse_solver_log(case / "log.icoFoam")
    fig, ax = plt.subplots(figsize=(8.5, 4.8), constrained_layout=True)
    for field in ("Ux", "Uy", "p"):
        rows = [row for row in residuals if row["field"] == field]
        ax.semilogy([row["time"] for row in rows], [row["initial"] for row in rows], label=field)
    ax.set(xlabel="Simulation time (s)", ylabel="Initial residual", title="Solver residual history")
    ax.grid(alpha=0.25)
    ax.legend()
    fig.savefig(output / "residual_history.png", dpi=180)
    plt.close(fig)

    last_by_field = {
        field: next(row for row in reversed(residuals) if row["field"] == field)
        for field in ("Ux", "Uy", "p")
    }
    metrics = {
        "case": str(case),
        "time_values": [float(v) for v in pv.OpenFOAMReader(str(case / f"{case.name}.foam")).time_values],
        "solver_end_marker": log_text.rstrip().endswith("End"),
        "solver_execution_time_s": execution[-1],
        "residual_last": last_by_field,
        "continuity_final": continuity[-1],
        "continuity_max_local": max(row["local"] for row in continuity),
        "continuity_max_abs_global": max(abs(row["global"]) for row in continuity),
        "continuity_max_abs_cumulative": max(abs(row["cumulative"]) for row in continuity),
        "steady_45_to_50_relative_l2": relative_l2,
        "steady_45_to_50_max_velocity_change": max_delta,
        "speed_min": float(speed.min()),
        "speed_max": float(speed.max()),
        "primary_vortex_center_approx": {
            "x": float(xx[vortex_y, vortex_x]),
            "y": float(yy[vortex_y, vortex_x]),
            "speed": float(speed[vortex_y, vortex_x]),
        },
        "ghia_u_rmse": float(np.sqrt(np.mean(u_error**2))),
        "ghia_u_max_abs_error": float(np.max(np.abs(u_error))),
        "ghia_v_rmse": float(np.sqrt(np.mean(v_error**2))),
        "ghia_v_max_abs_error": float(np.max(np.abs(v_error))),
        "u_centerline_min": float(u_center.min()),
        "u_centerline_max": float(u_center.max()),
        "v_centerline_min": float(v_center.min()),
        "v_centerline_max": float(v_center.max()),
    }
    (output / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
