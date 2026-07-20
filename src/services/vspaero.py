"""VSPAERO tier-1 aero service — subprocess wrapper around domain_packs/bwb/vsp_aero.py.

Fast vortex-lattice polar: screens designs in seconds and provides the
plausibility band the evaluator checks OpenFOAM results against.
"""
import json
import os
import subprocess
import sys
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from domain_packs.bwb.schema import GeometryReport, MissionDerived  # noqa: E402

VSP_PYTHON = os.environ.get("VSP_PYTHON", "/opt/vsp-venv/bin/python")
_RUNNER = os.path.join(_REPO_ROOT, "domain_packs", "bwb", "vsp_aero.py")


class FastAeroRejected(Exception):
    def __init__(self, message: str, result: Optional[dict] = None):
        super().__init__(message)
        self.result = result


def run_fast_aero(geometry_report: GeometryReport, derived: MissionDerived,
                  outdir: str, timeout_s: int = 900) -> dict:
    """Run the VSPAERO alpha sweep for a built geometry. Returns fast_aero dict
    (polar, CL_alpha, alpha_trim, L/D, x_np, gates). Raises FastAeroRejected on
    gate failure or missing solver output."""
    os.makedirs(outdir, exist_ok=True)
    vsp3 = geometry_report.artifacts.get("vsp3")
    if not vsp3 or not os.path.exists(vsp3):
        raise FastAeroRejected(f"vsp3 not found: {vsp3}")

    reference_reynolds = (
        derived.mission.cruise_speed_ms
        * geometry_report.mean_aero_chord_m
        / derived.atmosphere.kinematic_viscosity_m2s
    )
    cmd = [VSP_PYTHON, _RUNNER,
           "--vsp3", vsp3,
           "--sref", f"{geometry_report.s_ref_m2:.6f}",
           "--cref", f"{geometry_report.mean_aero_chord_m:.6f}",
           "--bref", f"{geometry_report.params.span_m:.6f}",
           "--recref", f"{reference_reynolds:.9g}",
           "--cl-target", f"{derived.required_cl:.6f}",
           "--outdir", outdir]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)

    result_path = os.path.join(outdir, "fast_aero.json")
    result = None
    if os.path.exists(result_path):
        with open(result_path) as f:
            result = json.load(f)

    if proc.returncode != 0:
        failed = [g for g in (result or {}).get("gates", []) if not g.get("passed")]
        reasons = "; ".join(f"{g['gate']}: {g.get('reason', '')}" for g in failed) \
            or (proc.stdout + proc.stderr).strip()[-400:]
        raise FastAeroRejected(f"fast aero rejected — {reasons}", result=result)
    if result is None:
        raise FastAeroRejected("fast aero rejected — fast_aero.json was not produced")
    result.update({
        "atmosphere": derived.atmosphere.model_dump(),
        "velocity_ms": derived.mission.cruise_speed_ms,
        "reference_length_m": geometry_report.mean_aero_chord_m,
        "reference_area_m2": geometry_report.s_ref_m2,
        "reference_reynolds": reference_reynolds,
    })
    with open(result_path, "w") as f:
        json.dump(result, f, indent=2)
    return result
