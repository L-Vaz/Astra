"""Pure-domain contract for deterministic coordinated airfoil recommendations."""
from __future__ import annotations

from copy import deepcopy

import pytest

from domain_packs.bwb.schema import BWBParams


def _selector():
    try:
        from domain_packs.bwb import airfoil_selection
    except ImportError as exc:  # Keep RED visible as a test failure, not collection error.
        pytest.fail(f"airfoil recommendation domain is missing: {exc}")
    return airfoil_selection


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


def _recommend(**overrides):
    selector = _selector()
    kwargs = dict(
        params=_params(),
        cruise_speed_ms=18.0,
        altitude_m=0.0,
        weight_n=34.335,
        launch="hand",
        brief="carry a camera for local inspection",
    )
    kwargs.update(overrides)
    return selector.recommend_airfoil_sets(**kwargs)


def test_recommendation_is_deterministic_distinct_and_records_search_provenance():
    first = _recommend()
    second = _recommend()

    assert first == second
    assert [s["strategy"] for s in first["strategies"]] == [
        "balanced", "efficiency", "low_speed_control"
    ]
    schedules = [tuple(s["schedule"][station] for station in ("root", "mid", "tip"))
                 for s in first["strategies"]]
    assert len(set(schedules)) == 3
    assert first["search"]["candidate_limit_per_station"] == 6
    assert all(len(first["search"]["station_candidates"][station]) == 6
               for station in ("root", "mid", "tip"))
    assert first["search"]["considered"]
    assert first["search"]["rejected"]
    assert all("reason" in item for item in first["search"]["rejected"])


def test_strategy_winners_are_members_of_the_pareto_front_and_close_sensitivity():
    recommendation = _recommend()
    front = {item["schedule_hash"] for item in recommendation["pareto_front"]}

    for strategy in recommendation["strategies"]:
        assert strategy["schedule_hash"] in front
        sensitivity = strategy["leave_one_metric_out"]
        assert set(sensitivity) == {"profile_drag", "stall_control", "trim", "evidence"}
        assert all("winner_hash" in result and "changed" in result
                   for result in sensitivity.values())


@pytest.mark.parametrize(
    ("brief", "launch", "expected"),
    [
        ("carry a camera for local inspection", "runway", "balanced"),
        ("maximize endurance and power efficiency", "runway", "efficiency"),
        ("hand launch with a strict low stall speed", "hand", "low_speed_control"),
    ],
)
def test_mission_badge_routing_is_explicit_and_deterministic(brief, launch, expected):
    recommendation = _recommend(brief=brief, launch=launch)
    assert recommendation["mission_badge"] == expected


def test_wider_and_longer_sensitivity_cannot_change_silently():
    production = _recommend()
    sensitivity = _recommend(candidate_limit=10, max_passes=8)
    comparison = sensitivity["bound_sensitivity"]

    assert comparison["production_candidate_limit"] == 6
    assert comparison["production_max_passes"] == 5
    if [s["schedule_hash"] for s in production["strategies"]] != [
        s["schedule_hash"] for s in sensitivity["strategies"]
    ]:
        assert comparison["warning"]


def test_pareto_filter_removes_only_dominated_schedules():
    pareto = _selector().pareto_filter
    records = [
        {"schedule_hash": "a", "objectives": {
            "profile_drag": 1.0, "stall_control": 1.0, "trim": 1.0, "evidence": 1.0}},
        {"schedule_hash": "b", "objectives": {
            "profile_drag": 2.0, "stall_control": 1.0, "trim": 1.0, "evidence": 1.0}},
        {"schedule_hash": "c", "objectives": {
            "profile_drag": 0.5, "stall_control": 2.0, "trim": 1.0, "evidence": 1.0}},
    ]
    untouched = deepcopy(records)

    assert [item["schedule_hash"] for item in pareto(records)] == ["a", "c"]
    assert records == untouched

