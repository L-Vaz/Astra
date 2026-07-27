#!/usr/bin/env python3
"""Compare two independently generated Foam-Agent cavity runs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pyvista as pv


INPUT_FILES = (
    "0/U",
    "0/p",
    "constant/physicalProperties",
    "system/blockMeshDict",
    "system/controlDict",
    "system/fvSchemes",
    "system/fvSolution",
    "Allrun",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def final_u(case: Path) -> np.ndarray:
    reader = pv.OpenFOAMReader(str(case / f"{case.name}.foam"))
    reader.set_active_time_value(50.0)
    return np.asarray(reader.read()["internalMesh"].cell_data["U"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run1", type=Path)
    parser.add_argument("run2", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run1 = args.run1.resolve()
    run2 = args.run2.resolve()
    u1 = final_u(run1)
    u2 = final_u(run2)
    delta = u2 - u1

    inputs = {
        name: {
            "identical": digest(run1 / name) == digest(run2 / name),
            "run1_sha256": digest(run1 / name),
            "run2_sha256": digest(run2 / name),
        }
        for name in INPUT_FILES
    }
    result = {
        "final_field_relative_l2": float(np.linalg.norm(delta) / np.linalg.norm(u1)),
        "final_field_max_velocity_difference": float(np.max(np.linalg.norm(delta, axis=1))),
        "identical_input_files": [name for name, value in inputs.items() if value["identical"]],
        "different_input_files": [name for name, value in inputs.items() if not value["identical"]],
        "input_hashes": inputs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
