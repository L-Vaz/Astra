import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from domain_packs.bwb.defaults import derive
from domain_packs.bwb.schema import MissionSpec
from services.flow_conditions import (  # noqa: E402
    FlowConditionMismatch,
    build_flow_contract,
    enforce_flow_contract_path,
    enforce_openfoam_viscosity,
    load_flow_contract,
    run_contracted_solver,
    validate_openfoam_viscosity,
    write_flow_contract,
)
from nodes import input_writer_node as writer_node  # noqa: E402
from services import simulate as simulate_service  # noqa: E402


@pytest.fixture
def contract():
    derived = derive(MissionSpec(
        payload_kg=1.5,
        cruise_speed_ms=25.0,
        cruise_altitude_m=4000.0,
        launch="hand",
    ))
    return build_flow_contract(
        derived,
        reference_length_m=derived.params.mean_aero_chord_m(),
        reference_area_m2=derived.params.planform_area_m2(),
    )


def test_flow_contract_round_trip_and_reynolds(contract, tmp_path):
    path = tmp_path / "flow_conditions.json"

    write_flow_contract(str(path), contract)
    loaded = load_flow_contract(str(path))

    assert loaded == contract
    assert loaded.reference_reynolds == pytest.approx(
        loaded.velocity_ms
        * loaded.reference_length_m
        / loaded.atmosphere.kinematic_viscosity_m2s,
        rel=1e-13,
    )
    raw = json.loads(path.read_text())
    assert raw["atmosphere"]["dataset_sha256"] == contract.atmosphere.dataset_sha256


def test_flow_contract_rejects_inconsistent_reynolds(contract):
    corrupt = contract.model_dump()
    corrupt["reference_reynolds"] *= 2
    with pytest.raises(ValueError, match="reference Reynolds"):
        type(contract).model_validate(corrupt)


def test_flow_contract_rejects_non_finite_inputs(contract):
    corrupt = contract.model_dump()
    corrupt["velocity_ms"] = float("inf")
    corrupt["reference_reynolds"] = float("inf")
    with pytest.raises(ValueError, match="finite"):
        type(contract).model_validate(corrupt)


def test_enforcement_rewrites_and_validates_nu(contract, tmp_path):
    physical = tmp_path / "constant" / "physicalProperties"
    physical.parent.mkdir()
    physical.write_text(
        "FoamFile\n{\n    version 2.0;\n    object physicalProperties;\n}\n"
        "viscosityModel constant;\n"
        "nu [0 2 -1 0 0 0 0] 9e-5;\n"
    )

    written = enforce_openfoam_viscosity(str(tmp_path), contract)

    assert written == pytest.approx(contract.atmosphere.kinematic_viscosity_m2s)
    assert validate_openfoam_viscosity(str(tmp_path), contract) == pytest.approx(written)
    text = physical.read_text()
    assert "[0 2 -1 0 0 0 0]" in text
    assert "9e-5" not in text


def test_enforcement_creates_foundation_v10_dictionary(contract, tmp_path):
    value = enforce_openfoam_viscosity(str(tmp_path), contract)

    physical = tmp_path / "constant" / "physicalProperties"
    assert physical.exists()
    assert "object      physicalProperties;" in physical.read_text()
    assert validate_openfoam_viscosity(str(tmp_path), contract) == pytest.approx(value)


def test_validator_rejects_wrong_value_or_dimensions(contract, tmp_path):
    physical = tmp_path / "constant" / "physicalProperties"
    physical.parent.mkdir()
    physical.write_text("nu [0 1 -1 0 0 0 0] 9e-5;\n")

    with pytest.raises(FlowConditionMismatch, match="dimensions"):
        validate_openfoam_viscosity(str(tmp_path), contract)

    physical.write_text("nu [0 2 -1 0 0 0 0] 9e-5;\n")
    with pytest.raises(FlowConditionMismatch, match="does not match"):
        validate_openfoam_viscosity(str(tmp_path), contract)


def test_enforcement_ignores_commented_nu(contract, tmp_path):
    physical = tmp_path / "constant" / "physicalProperties"
    physical.parent.mkdir()
    physical.write_text(
        "/* old value:\nnu [0 2 -1 0 0 0 0] 9e-5;\n*/\n"
        "nu [0 2 -1 0 0 0 0] 8e-5;\n"
    )

    enforce_openfoam_viscosity(str(tmp_path), contract)

    text = physical.read_text()
    assert "old value:\nnu [0 2 -1 0 0 0 0] 9e-5;" in text
    assert validate_openfoam_viscosity(str(tmp_path), contract) == pytest.approx(
        contract.atmosphere.kinematic_viscosity_m2s
    )


def test_enforcement_rejects_dictionary_indirection(contract, tmp_path):
    physical = tmp_path / "constant" / "physicalProperties"
    physical.parent.mkdir()
    physical.write_text('#include "sharedViscosity"\n')

    with pytest.raises(FlowConditionMismatch, match="indirection"):
        enforce_openfoam_viscosity(str(tmp_path), contract)


@pytest.mark.parametrize(
    "content",
    [
        "viscosityModel constant; nu [0 2 -1 0 0 0 0] 9e-5;\n",
        "nu\n[0 2 -1 0 0 0 0]\n9e-5;\n",
    ],
)
def test_enforcement_rejects_unsupported_nu_layout(contract, tmp_path, content):
    physical = tmp_path / "constant" / "physicalProperties"
    physical.parent.mkdir()
    physical.write_text(content)

    with pytest.raises(FlowConditionMismatch, match="unsupported nu syntax"):
        enforce_openfoam_viscosity(str(tmp_path), contract)


def test_contracted_solver_enforces_immediately_before_launch(
    monkeypatch, contract, tmp_path
):
    contract_path = tmp_path / "flow_conditions.json"
    write_flow_contract(str(contract_path), contract)
    physical = tmp_path / "case" / "constant" / "physicalProperties"
    physical.parent.mkdir(parents=True)
    physical.write_text("nu [0 2 -1 0 0 0 0] 9e-5;\n")
    observed = {}

    def fake_run(*args, **kwargs):
        observed["nu"] = validate_openfoam_viscosity(str(tmp_path / "case"), contract)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("services.flow_conditions.subprocess.run", fake_run)

    result = run_contracted_solver(
        str(tmp_path / "case"), str(contract_path), timeout_s=30
    )

    assert result.returncode == 0
    assert observed["nu"] == pytest.approx(
        contract.atmosphere.kinematic_viscosity_m2s
    )


def test_contract_path_gate_enforces_serialized_contract(contract, tmp_path):
    contract_path = tmp_path / "flow_conditions.json"
    write_flow_contract(str(contract_path), contract)

    written = enforce_flow_contract_path(str(tmp_path / "case"), str(contract_path))

    assert written == pytest.approx(contract.atmosphere.kinematic_viscosity_m2s)


def test_input_writer_runs_flow_gate_after_generation(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(writer_node, "_premeshed_boundary_info", lambda state: "")
    monkeypatch.setattr(
        writer_node,
        "initial_write",
        lambda **kwargs: order.append("write") or {
            "dir_structure": {"constant": ["physicalProperties"]},
            "foamfiles": {},
        },
    )
    monkeypatch.setattr(
        writer_node,
        "build_allrun",
        lambda **kwargs: order.append("allrun") or {},
    )
    monkeypatch.setattr(
        writer_node,
        "enforce_flow_contract_path",
        lambda case_dir, path: order.append(("gate", case_dir, path)),
        raising=False,
    )
    state = {
        "case_dir": str(tmp_path / "case"),
        "flow_conditions_path": str(tmp_path / "flow_conditions.json"),
        "config": SimpleNamespace(
            input_writer_generation_mode="sequential_dependency",
            reuse_generated_dir="",
            database_path="db",
            searchdocs="docs",
        ),
        "subtasks": [],
        "user_requirement": "test",
        "tutorial_reference": None,
        "case_stats": {"case_solver": ["simpleFoam"]},
        "case_info": "",
        "allrun_reference": "",
        "mesh_type": "custom_mesh",
        "mesh_commands": [],
    }

    writer_node._initial_write_mode(state)

    assert order == [
        "write",
        "allrun",
        ("gate", state["case_dir"], state["flow_conditions_path"]),
    ]


def test_simulation_subprocess_receives_flow_contract(monkeypatch, contract, tmp_path):
    mesh_path = tmp_path / "approved.msh"
    mesh_path.write_text("mesh")
    contract_path = tmp_path / "flow_conditions.json"
    write_flow_contract(str(contract_path), contract)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(simulate_service.subprocess, "run", fake_run)

    simulate_service.run_simulation(
        "requirement",
        str(mesh_path),
        str(tmp_path / "simulation"),
        flow_conditions_path=str(contract_path),
    )

    shell_command = captured["command"][2]
    assert "--flow_conditions_path" in shell_command
    assert str(contract_path) in shell_command


def test_requirement_records_exact_atmosphere(contract):
    requirement = simulate_service.compose_requirement(
        "mission",
        cruise_speed_ms=contract.velocity_ms,
        alpha_deg=2.0,
        flow_conditions=contract,
    )

    assert f"{contract.atmosphere.kinematic_viscosity_m2s:.17g}" in requirement
    assert contract.atmosphere.source in requirement
    assert contract.atmosphere.dataset_sha256 in requirement
