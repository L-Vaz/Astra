"""Smoke exact file-airfoil geometry through the real VSPAERO executable.

Runtime artifacts stay below ASTRA_RUNS_DIR (or ~/astra_runs).  This is deliberately
not a golden updater: it verifies executable availability, exact section identity,
polar production, and the existing fast-aero gates without changing repository data.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from domain_packs.bwb.defaults import derive
from domain_packs.bwb.schema import BWBParams, MissionSpec
from services.geometry import build_geometry
from services.vspaero import run_fast_aero


def main() -> int:
    with (REPO_ROOT / "tests" / "data" / "aero_golden.json").open() as stream:
        reference = json.load(stream)["reference"]

    params = BWBParams.model_validate(reference["params"])
    mission = MissionSpec(
        payload_kg=1.0,
        mtow_kg=reference["weight_n"] / 9.81,
        cruise_speed_ms=reference["speed_ms"],
        cruise_altitude_m=reference["altitude_m"],
        launch=reference["launch"],
    )
    derived = derive(mission)
    runs_root = Path(os.environ.get("ASTRA_RUNS_DIR", "~/astra_runs")).expanduser()
    runs_root.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="airfoil-vspaero-smoke-", dir=runs_root))

    geometry = build_geometry(params, str(output / "geometry"))
    result = run_fast_aero(geometry, derived, str(output / "vspaero"))
    sections = geometry.airfoil_sections
    expected_names = [params.root_airfoil, params.mid_airfoil, params.tip_airfoil]
    names = [row["name"] for row in sections]
    types = [row["openvsp_type"] for row in sections]
    passed = (
        names == expected_names
        and types == ["XS_FILE_AIRFOIL"] * 3
        and len(result["polar"]) >= 3
        and all(gate["passed"] for gate in result["gates"])
    )
    print(json.dumps({
        "passed": passed,
        "artifact_dir": str(output),
        "polar_points": len(result["polar"]),
        "airfoils": names,
        "types": types,
        "gates": result["gates"],
    }, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
