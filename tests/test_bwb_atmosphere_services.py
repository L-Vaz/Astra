import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from domain_packs.bwb.defaults import derive
from domain_packs.bwb.schema import GateResult, GeometryReport, MissionSpec

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from services.intent import build_enriched_requirement  # noqa: E402
from services import vspaero  # noqa: E402


@pytest.fixture
def derived():
    return derive(MissionSpec(
        payload_kg=1.5,
        cruise_speed_ms=25.0,
        cruise_altitude_m=1275.0,
        launch="hand",
    ))


def test_enriched_requirement_contains_resolved_atmosphere(derived):
    text = build_enriched_requirement(derived)
    atmosphere = derived.atmosphere

    assert f"temperature {atmosphere.temperature_k:.3f} K" in text
    assert f"pressure {atmosphere.pressure_pa:.2f} Pa" in text
    assert f"air density {atmosphere.density_kgm3:.6f} kg/m^3" in text
    assert f"dynamic viscosity {atmosphere.dynamic_viscosity_kgms:.9g} kg/(m*s)" in text
    assert f"kinematic viscosity {atmosphere.kinematic_viscosity_m2s:.9g} m^2/s" in text
    assert atmosphere.source in text
    assert atmosphere.dataset_sha256 in text
    assert "1.48e-5" not in text


def test_vspaero_uses_geometry_reynolds_and_records_atmosphere(
    tmp_path, monkeypatch, derived
):
    vsp3 = tmp_path / "model.vsp3"
    vsp3.write_text("fixture")
    geometry = GeometryReport(
        params=derived.params,
        s_ref_m2=derived.params.planform_area_m2(),
        mean_aero_chord_m=derived.params.mean_aero_chord_m() * 1.05,
        aspect_ratio=derived.params.aspect_ratio(),
        bbox_m=[0, 0, 0, 1, 1, 1],
        n_facets=100,
        watertight=True,
        open_edges=0,
        gates=[GateResult(gate="fixture", passed=True)],
        artifacts={"vsp3": str(vsp3)},
    )
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        outdir = cmd[cmd.index("--outdir") + 1]
        with open(tmp_path / "fast_aero.json", "w") as stream:
            json.dump({"gates": [{"gate": "fixture", "passed": True}]}, stream)
        assert outdir == str(tmp_path)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vspaero.subprocess, "run", fake_run)

    result = vspaero.run_fast_aero(geometry, derived, str(tmp_path))

    expected_re = (
        derived.mission.cruise_speed_ms
        * geometry.mean_aero_chord_m
        / derived.atmosphere.kinematic_viscosity_m2s
    )
    cmd_re = float(captured["cmd"][captured["cmd"].index("--recref") + 1])
    assert cmd_re == pytest.approx(expected_re, rel=5e-7)
    assert result["reference_reynolds"] == pytest.approx(expected_re)
    assert result["reference_length_m"] == geometry.mean_aero_chord_m
    assert result["velocity_ms"] == derived.mission.cruise_speed_ms
    assert result["atmosphere"] == derived.atmosphere.model_dump()
    assert json.loads((tmp_path / "fast_aero.json").read_text()) == result
