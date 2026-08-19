"""MCP exposure and public named-section override contract."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from domain_packs.bwb.defaults import derive
from domain_packs.bwb.schema import MissionSpec
from services.geometry import apply_overrides


pytestmark = pytest.mark.wsl


def test_public_geometry_override_accepts_named_string_sections():
    params = derive(MissionSpec(
        payload_kg=1.0, mtow_kg=3.5, cruise_speed_ms=18.0,
        cruise_altitude_m=0.0, launch="hand")).params

    changed = apply_overrides(params, {
        "root_airfoil": "e475",
        "mid_airfoil": "e474",
        "tip_airfoil": "s5010",
    })

    assert changed.root_airfoil == "e475"
    assert changed.mid_airfoil == "e474"
    assert changed.root_thickness_pct != params.root_thickness_pct


def test_mcp_registers_airfoil_recommend_and_select_tools_without_importing_credentials():
    """The inherited MCP module loads credential-backed CFD services at import time.

    Parse its registration AST so this contract remains environment-independent; the
    tool bodies delegate to the service whose behavior is covered by store tests.
    """
    path = Path(__file__).parents[1] / "src" / "mcp" / "fastmcp_server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    classes = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.add(node.name)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "tool"):
                for keyword in decorator.keywords:
                    if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                        names.add(keyword.value.value)

    assert {"recommend_airfoil_sets", "select_airfoil_set"} <= names
    assert {"RecommendAirfoilSetsRequest", "SelectAirfoilSetRequest"} <= classes


def test_mcp_build_geometry_override_annotation_is_not_float_only():
    path = Path(__file__).parents[1] / "src" / "mcp" / "fastmcp_server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    request = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef)
                   and node.name == "BuildGeometryRequest")
    override = next(node for node in request.body
                    if isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == "overrides")
    rendered = ast.unparse(override.annotation)
    assert "Any" in rendered, rendered
