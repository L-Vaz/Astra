import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from domain_packs.bwb.defaults import derive  # noqa: E402
from domain_packs.bwb.schema import MissionSpec  # noqa: E402

# See tests/test_flow_conditions.py for why this guard exists: importing `services`
# needs credentials/optional deps a domain_packs-only run may lack, and an unguarded
# failure aborts collection for the ENTIRE suite rather than skipping this module.
try:
    from services import evaluate  # noqa: E402
    from services.flow_conditions import build_flow_contract  # noqa: E402
except (FileNotFoundError, ImportError) as exc:
    pytest.skip(f"services layer unavailable ({type(exc).__name__}: {exc})",
                allow_module_level=True)


def test_evaluation_records_atmosphere_and_yplus_viscosity(monkeypatch, tmp_path):
    derived = derive(MissionSpec(
        payload_kg=1.5,
        cruise_speed_ms=25.0,
        cruise_altitude_m=1275.0,
        launch="hand",
    ))
    contract = build_flow_contract(
        derived,
        reference_length_m=derived.params.mean_aero_chord_m(),
        reference_area_m2=derived.params.planform_area_m2(),
    )
    monkeypatch.setattr(evaluate, "find_force_coeffs", lambda case_dir: None)
    monkeypatch.setattr(evaluate, "parse_residuals", lambda case_dir: {})
    monkeypatch.setattr(
        evaluate,
        "parse_yplus",
        lambda case_dir: {"min": 10.0, "max": 90.0, "avg": 45.0},
    )
    monkeypatch.setattr(evaluate, "_plots", lambda *args: {})
    monkeypatch.setattr(evaluate, "_field_renders", lambda *args: {})

    report = evaluate.evaluate_case(
        case_dir=str(tmp_path / "case"),
        outdir=str(tmp_path / "results"),
        flow_conditions=contract,
    )

    assert report["flow_conditions"] == contract.model_dump(mode="json")
    assert report["metrics"]["yplus"]["kinematic_viscosity_m2s"] == (
        contract.atmosphere.kinematic_viscosity_m2s
    )
    saved = json.loads((tmp_path / "results" / "evaluation.json").read_text())
    assert saved["flow_conditions"]["atmosphere"]["source"] == (
        contract.atmosphere.source
    )
    assert saved["flow_conditions"]["atmosphere"]["dataset_sha256"] == (
        contract.atmosphere.dataset_sha256
    )
