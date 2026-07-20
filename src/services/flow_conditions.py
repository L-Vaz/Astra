"""Deterministic atmosphere contract and Foundation OpenFOAM v10 nu gate."""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domain_packs.bwb.atmosphere import AtmosphereState
from domain_packs.bwb.schema import MissionDerived


KINEMATIC_VISCOSITY_DIMENSIONS = "0 2 -1 0 0 0 0"
FOAM_BASHRC = os.environ.get("FOAM_BASHRC", "/opt/openfoam10/etc/bashrc")
_FLOAT_PATTERN = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_NU_RE = re.compile(
    rf"(?m)^(?P<indent>[ \t]*)nu[ \t]+"
    rf"(?:\[(?P<dimensions>[^\]]+)\][ \t]+)?"
    rf"(?P<value>{_FLOAT_PATTERN})[ \t]*;"
)
_NU_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])nu(?![A-Za-z0-9_])")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//.*?$", re.MULTILINE)
_INDIRECTION_RE = re.compile(r"(?m)^[ \t]*#|\$\{?[A-Za-z_]", re.MULTILINE)


class FlowConditionMismatch(RuntimeError):
    """The generated case does not implement its contracted flow state."""


class FlowConditions(BaseModel):
    model_config = ConfigDict(frozen=True)

    atmosphere: AtmosphereState
    velocity_ms: float = Field(gt=0.0)
    reference_length_m: float = Field(gt=0.0)
    reference_area_m2: float = Field(gt=0.0)
    reference_reynolds: float = Field(gt=0.0)

    @model_validator(mode="after")
    def validate_reynolds_identity(self):
        values = (
            self.velocity_ms,
            self.reference_length_m,
            self.reference_area_m2,
            self.reference_reynolds,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("flow-condition values must be finite")
        expected = (
            self.velocity_ms * self.reference_length_m
            / self.atmosphere.kinematic_viscosity_m2s
        )
        if not math.isclose(
            self.reference_reynolds, expected, rel_tol=1e-12, abs_tol=0.0
        ):
            raise ValueError("reference Reynolds is inconsistent with flow conditions")
        return self


def build_flow_contract(
    derived: MissionDerived,
    reference_length_m: float,
    reference_area_m2: float,
) -> FlowConditions:
    velocity = derived.mission.cruise_speed_ms
    reynolds = (
        velocity
        * reference_length_m
        / derived.atmosphere.kinematic_viscosity_m2s
    )
    return FlowConditions(
        atmosphere=derived.atmosphere,
        velocity_ms=velocity,
        reference_length_m=reference_length_m,
        reference_area_m2=reference_area_m2,
        reference_reynolds=reynolds,
    )


def write_flow_contract(path: str, contract: FlowConditions) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = contract.model_dump_json(indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, delete=False
    ) as stream:
        stream.write(content)
        temporary = stream.name
    os.replace(temporary, target)


def load_flow_contract(path: str) -> FlowConditions:
    return FlowConditions.model_validate_json(Path(path).read_text(encoding="utf-8"))


def enforce_flow_contract_path(
    case_dir: str, flow_conditions_path: str | None
) -> float | None:
    """Apply a serialized flow contract when the workflow was given one."""
    if not flow_conditions_path:
        return None
    return enforce_openfoam_viscosity(
        case_dir, load_flow_contract(flow_conditions_path)
    )


def _physical_properties_path(case_dir: str) -> Path:
    return Path(case_dir) / "constant" / "physicalProperties"


def _canonical_physical_properties(nu: float) -> str:
    return f"""FoamFile
{{
    format      ascii;
    class       dictionary;
    object      physicalProperties;
}}

viscosityModel constant;

nu              [{KINEMATIC_VISCOSITY_DIMENSIONS}] {nu:.17g};
"""


def _mask_comments(content: str) -> str:
    """Replace comments with whitespace while preserving match offsets."""
    def mask(match: re.Match) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group())

    return _LINE_COMMENT_RE.sub(mask, _BLOCK_COMMENT_RE.sub(mask, content))


def _active_dictionary(content: str, target: Path) -> str:
    active = _mask_comments(content)
    if _INDIRECTION_RE.search(active):
        raise FlowConditionMismatch(
            f"{target} uses unsupported dictionary indirection; write one literal nu"
        )
    return active


def _literal_nu_matches(active: str, target: Path) -> list[re.Match]:
    matches = list(_NU_RE.finditer(active))
    tokens = list(_NU_TOKEN_RE.finditer(active))
    if len(tokens) != len(matches):
        raise FlowConditionMismatch(
            f"{target} contains unsupported nu syntax; use one literal, "
            "single-line nu entry"
        )
    return matches


def enforce_openfoam_viscosity(
    case_dir: str, contract: FlowConditions
) -> float:
    """Write the exact contracted nu, then parse it back before returning."""
    target = _physical_properties_path(case_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    nu = contract.atmosphere.kinematic_viscosity_m2s
    replacement = f"nu [{KINEMATIC_VISCOSITY_DIMENSIONS}] {nu:.17g};"
    if target.exists():
        content = target.read_text(encoding="utf-8")
        matches = _literal_nu_matches(
            _active_dictionary(content, target), target
        )
        if len(matches) > 1:
            raise FlowConditionMismatch(
                f"{target} contains {len(matches)} active nu entries"
            )
        if matches:
            match = matches[0]
            replacement = match.group("indent") + replacement
            content = content[:match.start()] + replacement + content[match.end():]
        else:
            content = content.rstrip() + "\n\n" + replacement + "\n"
    else:
        content = _canonical_physical_properties(nu)
    target.write_text(content, encoding="utf-8", newline="\n")
    return validate_openfoam_viscosity(case_dir, contract)


def validate_openfoam_viscosity(
    case_dir: str, contract: FlowConditions
) -> float:
    target = _physical_properties_path(case_dir)
    if not target.exists():
        raise FlowConditionMismatch(f"missing Foundation v10 dictionary: {target}")
    content = target.read_text(encoding="utf-8")
    active = _active_dictionary(content, target)
    matches = _literal_nu_matches(active, target)
    if len(matches) != 1:
        raise FlowConditionMismatch(
            f"{target} must contain exactly one active nu entry; found {len(matches)}"
        )
    match = matches[0]
    dimensions = " ".join((match.group("dimensions") or "").split())
    if dimensions != KINEMATIC_VISCOSITY_DIMENSIONS:
        raise FlowConditionMismatch(
            f"OpenFOAM nu dimensions [{dimensions}] do not match "
            f"[{KINEMATIC_VISCOSITY_DIMENSIONS}]"
        )
    actual = float(match.group("value"))
    expected = contract.atmosphere.kinematic_viscosity_m2s
    if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=0.0):
        raise FlowConditionMismatch(
            f"OpenFOAM nu {actual:.17g} does not match contracted nu "
            f"{expected:.17g}"
        )
    return actual


def run_contracted_solver(
    case_dir: str,
    flow_conditions_path: str,
    *,
    solver_command: str = "simpleFoam",
    log_filename: str = "log.simpleFoam",
    timeout_s: int = 3600,
):
    """Enforce/read back nu immediately before a direct OpenFOAM solve."""
    enforce_flow_contract_path(case_dir, flow_conditions_path)
    command = (
        f"source {shlex.quote(FOAM_BASHRC)} >/dev/null 2>&1; "
        f"{solver_command} > {shlex.quote(log_filename)} 2>&1"
    )
    return subprocess.run(
        ["bash", "-lc", command],
        cwd=case_dir,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
