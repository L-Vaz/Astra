"""Geometry service — drives the OpenVSP builder in its dedicated venv.

The compiled OpenVSP binding is locked to python 3.10, so geometry generation
runs as a subprocess under VSP_PYTHON (default /opt/vsp-venv/bin/python) while
this service, like the rest of Foam-Agent, runs in the FoamAgent conda env.
Contract: params JSON in, geometry_report.json + artifacts out.
"""
import json
import os
import subprocess
import sys
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from domain_packs.bwb.schema import BWBParams, GeometryReport  # noqa: E402

VSP_PYTHON = os.environ.get("VSP_PYTHON", "/opt/vsp-venv/bin/python")
_BUILDER = os.path.join(_REPO_ROOT, "domain_packs", "bwb", "vsp_builder.py")


class GeometryRejected(Exception):
    """Raised when a geometry gate fails. Carries the report (or raw dict) if any."""

    def __init__(self, message: str, report: Optional[dict] = None):
        super().__init__(message)
        self.report = report


def apply_overrides(params: BWBParams, overrides: dict) -> BWBParams:
    """Apply a param delta and re-run full validation (ranges + cross-checks)."""
    # Computed thickness fields are deliberately present in ``model_dump`` for the
    # cockpit, but are not inputs.  Excluding them here also prevents a named section
    # change from carrying the previous section's now-stale computed thickness into
    # the validator.
    allowed = set(type(params).model_fields)
    unknown = set(overrides) - allowed
    if unknown:
        raise GeometryRejected(f"unknown geometry parameters: {sorted(unknown)}")
    data = params.model_dump(exclude={
        "root_thickness_pct", "mid_thickness_pct", "tip_thickness_pct",
    })
    data.update(overrides)
    return BWBParams.model_validate(data)  # ValidationError -> caller reports reject


def build_geometry(params: BWBParams, outdir: str, timeout_s: int = 600) -> GeometryReport:
    """Run the OpenVSP builder; return the parsed report. Raises GeometryRejected
    when any gate fails (reason codes inside the report)."""
    os.makedirs(outdir, exist_ok=True)
    params_path = os.path.join(outdir, "params.json")
    with open(params_path, "w") as f:
        f.write(params.model_dump_json(indent=2))

    proc = subprocess.run(
        [VSP_PYTHON, _BUILDER, "--params", params_path, "--outdir", outdir],
        capture_output=True, text=True, timeout=timeout_s,
    )

    report_path = os.path.join(outdir, "geometry_report.json")
    raw = None
    if os.path.exists(report_path):
        with open(report_path) as f:
            raw = json.load(f)

    if proc.returncode != 0:
        failed = [g for g in (raw or {}).get("gates", []) if not g.get("passed")]
        reasons = "; ".join(f"{g['gate']}: {g.get('reason', '')}" for g in failed) \
            or proc.stderr.strip()[-500:] or "builder failed without report"
        raise GeometryRejected(f"geometry rejected — {reasons}", report=raw)

    return GeometryReport.model_validate(raw)
