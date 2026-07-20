"""Derivation-graph tests: node values must equal the AeroEstimate exactly,
the graph must be closed, and every node must carry a source."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from domain_packs.bwb.aero_estimates import estimate
from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.defaults import derive
from domain_packs.bwb.derivation import ROOTS, derive_graph
from domain_packs.bwb.schema import MissionSpec

CTX = dict(atmosphere=atmosphere_at(100.0), v_ms=25.0, weight_n=5.0 * 9.81,
           launch="hand")


@pytest.fixture(scope="module")
def params():
    return derive(MissionSpec(payload_kg=1.5, cruise_speed_ms=25.0,
                              cruise_altitude_m=100.0, launch="hand")).params


def test_root_values_match_estimate(params):
    graph = derive_graph(params, **CTX)
    est = estimate(params, CTX["atmosphere"], CTX["v_ms"], CTX["weight_n"],
                   CTX["launch"])
    expected = {
        "ld_cruise": est.ld_cruise, "cd_cruise": est.cd_cruise,
        "cl_cruise": est.cl_cruise, "cd0": est.cd0, "oswald_e": est.oswald_e,
        "cl_star": est.cl_star, "ld_max": est.ld_max,
        "cl_max_3d": est.cl_max_3d, "v_stall_ms": est.v_stall_ms,
        "x_np_m": est.x_np_m, "reynolds_mac": est.reynolds_mac,
        "wing_loading_kgm2": est.wing_loading_kgm2,
    }
    for node_id, want in expected.items():
        assert graph.nodes[node_id].value == pytest.approx(want, rel=1e-12), node_id


def test_graph_is_closed(params):
    graph = derive_graph(params, **CTX)
    for node in graph.nodes.values():
        for child in node.children:
            assert child in graph.nodes, f"{node.id} -> missing child {child}"
    for root in graph.roots:
        assert root in graph.nodes
    assert set(graph.roots) == set(ROOTS)


def test_every_node_documented(params):
    graph = derive_graph(params, **CTX)
    for node in graph.nodes.values():
        assert node.source, f"{node.id} has no source"
        assert node.label
        if node.kind == "computed":
            assert node.formula, f"{node.id} computed without a formula"
            assert node.substitution, f"{node.id} computed without substitution"


def test_atmosphere_chain_is_explicit_and_consistent(params):
    graph = derive_graph(params, **CTX)

    for node_id in ("altitude_m", "temperature_k", "pressure_pa", "rho",
                    "mu_air", "nu_air"):
        assert node_id in graph.nodes
    assert graph.nodes["nu_air"].value == pytest.approx(
        graph.nodes["mu_air"].value / graph.nodes["rho"].value, rel=1e-13)
    assert graph.nodes["reynolds_mac"].children == ["v_ms", "mac_m", "nu_air"]


def test_launch_node_present_only_with_launch(params):
    with_launch = derive_graph(params, **CTX)
    assert "launch_limit" in with_launch.nodes
    without = derive_graph(params, CTX["atmosphere"], CTX["v_ms"], CTX["weight_n"])
    assert "launch_limit" not in without.nodes
