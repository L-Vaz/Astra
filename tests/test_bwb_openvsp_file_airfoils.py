"""Real OpenVSP proof that selected names become exact file-airfoil curves."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from domain_packs.bwb.schema import BWBParams


VSP_PYTHON = Path("/opt/vsp-venv/bin/python")
pytestmark = [
    pytest.mark.wsl,
    pytest.mark.skipif(
        not VSP_PYTHON.exists(),
        reason="OpenVSP Python 3.10 environment unavailable outside configured WSL",
    ),
]


def _params() -> BWBParams:
    return BWBParams(
        span_m=1.6,
        root_chord_m=0.55,
        centre_body_fraction=0.28,
        mid_chord_frac=0.60,
        taper_outer=0.40,
        sweep_inner_deg=35.0,
        sweep_outer_deg=18.0,
        root_airfoil="e474",
        mid_airfoil="e472",
        tip_airfoil="s5010",
        root_twist_deg=1.0,
        tip_twist_deg=-3.0,
        dihedral_deg=3.0,
    )


def test_openvsp_build_uses_exact_file_airfoils_and_reports_their_hashes(tmp_path):
    params_path = tmp_path / "params.json"
    params_path.write_text(_params().model_dump_json(), encoding="utf-8")
    builder = Path(__file__).parents[1] / "domain_packs" / "bwb" / "vsp_builder.py"
    run = subprocess.run(
        [str(VSP_PYTHON), str(builder), "--params", str(params_path),
         "--outdir", str(tmp_path / "geometry")],
        text=True,
        capture_output=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr

    vsp3 = tmp_path / "geometry" / "bwb.vsp3"
    probe = subprocess.run(
        [
            str(VSP_PYTHON),
            "-c",
            (
                "import json,openvsp as v; v.ClearVSPModel(); "
                f"v.ReadVSPFile({str(vsp3)!r}); w=v.FindGeoms()[0]; "
                "s=v.GetXSecSurf(w,0); "
                "print(json.dumps([v.GetXSecShape(v.GetXSec(s,i)) for i in range(3)]))"
            ),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(probe.stdout.strip().splitlines()[-1]) == [12, 12, 12]

    report = json.loads((tmp_path / "geometry" / "geometry_report.json").read_text())
    sections = report["airfoil_sections"]
    assert [entry["name"] for entry in sections] == ["e474", "e472", "s5010"]
    assert all(entry["sha256"] and entry["openvsp_type"] == "XS_FILE_AIRFOIL"
               for entry in sections)

