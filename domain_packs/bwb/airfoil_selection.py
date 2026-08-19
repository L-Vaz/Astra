"""Deterministic coordinated root/mid/tip airfoil recommendations.

This module ranks schedules, not isolated sections.  Local polar screening narrows the
search, then the existing whole-wing estimate supplies profile drag, stall, trim and
stability consequences.  No language model participates in correctness.

Production bounds of six candidates/station and five coupled passes are approved
engineering judgements [D].  Callers can request the predeclared 10/8 sensitivity;
the result records any changed winner instead of hiding it.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import itertools
import json
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from . import airfoil_coordinates, airfoils
from .aero_estimates import LAUNCH_VSTALL_MAX, estimate
from .atmosphere import atmosphere_at
from .inverse import ShapeFractions, solve_planform
from .schema import BWBParams
from .spanwise import stations


PRODUCTION_CANDIDATE_LIMIT = 6  # [D], sensitivity at 10.
PRODUCTION_MAX_PASSES = 5       # [D], sensitivity at 8.
SENSITIVITY_CANDIDATE_LIMIT = 10
SENSITIVITY_MAX_PASSES = 8
STRATEGIES = ("balanced", "efficiency", "low_speed_control")
OBJECTIVES = ("profile_drag", "stall_control", "trim", "evidence")

_EFFICIENCY_INTENT = re.compile(
    r"\b(range|endurance|loiter|flight[ -]?time|power[ -]?efficien(?:cy|t))\b",
    re.IGNORECASE,
)


def _hash(value: Any) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True,
                     ensure_ascii=True).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _replace_params(params: BWBParams, **updates: Any) -> BWBParams:
    raw = params.model_dump()
    for computed in ("root_thickness_pct", "mid_thickness_pct", "tip_thickness_pct"):
        raw.pop(computed, None)
    raw.update(updates)
    return BWBParams.model_validate(raw)


def _station_state(params: BWBParams, cruise_speed_ms: float, altitude_m: float,
                   weight_n: float, launch: str) -> Dict[str, Dict[str, Any]]:
    atmosphere = atmosphere_at(altitude_m)
    base = estimate(params, atmosphere, cruise_speed_ms, weight_n, launch)
    requested_low = min(
        cruise_speed_ms,
        max(base.v_stall_ms, LAUNCH_VSTALL_MAX[launch]),
    )
    # The exact table envelope, not a guessed margin, sets the lowest evaluable
    # speed. Re=rho*V*c/mu = V*c/nu, hence V=Re_min*nu/c for the smallest chord.
    re_floor = min(float(value) for value in airfoils.provenance()["re_grid"])
    min_chord = min(params.chords())
    supported_low = re_floor * atmosphere.kinematic_viscosity_m2s / min_chord
    evaluated_low = min(cruise_speed_ms, max(requested_low, supported_low))

    def samples(speed: float):
        re_mac = speed * params.mean_aero_chord_m() / atmosphere.kinematic_viscosity_m2s
        wing_cl = weight_n / (
            0.5 * atmosphere.density_kgm3 * speed ** 2 * params.planform_area_m2()
        )
        all_stations = stations(params, re_mac)
        chosen = {
            "root": min(all_stations, key=lambda item: abs(item.eta - 0.0)),
            "mid": min(all_stations, key=lambda item: abs(
                item.eta - params.centre_body_fraction)),
            "tip": min(all_stations, key=lambda item: abs(item.eta - 1.0)),
        }
        return wing_cl, chosen

    cruise_cl, cruise = samples(cruise_speed_ms)
    low_cl, low = samples(evaluated_low)
    baseline_sections = {
        "root": params.root_airfoil,
        "mid": params.mid_airfoil,
        "tip": params.tip_airfoil,
    }
    table_provenance = airfoils.provenance()
    reynolds_grid = tuple(float(value) for value in table_provenance["re_grid"])
    cl_ladder = tuple(float(value) for value in airfoils.cl_ladder())
    output: Dict[str, Dict[str, Any]] = {}
    for name in ("root", "mid", "tip"):
        section_name = baseline_sections[name]
        cruise_aero = airfoils.section_at(
            section_name, float(cruise[name].reynolds)
        )
        low_aero = airfoils.section_at(section_name, float(low[name].reynolds))
        _cruise_cd, cruise_lift_flags = airfoils.section_cd(
            section_name,
            float(cruise_cl * cruise[name].cl_add + cruise[name].cl_basic),
            float(cruise[name].reynolds),
        )
        _low_cd, low_lift_flags = airfoils.section_cd(
            section_name,
            float(low_cl * low[name].cl_add + low[name].cl_basic),
            float(low[name].reynolds),
        )
        output[name] = {
            "eta": float(cruise[name].eta),
            "chord_m": float(cruise[name].chord),
            "twist_deg": float(cruise[name].twist_deg),
            "reynolds_cruise": float(cruise[name].reynolds),
            "reynolds_low_speed": float(low[name].reynolds),
            "cl_demand_cruise": float(
                cruise_cl * cruise[name].cl_add + cruise[name].cl_basic),
            "cl_demand_low_speed": float(
                low_cl * low[name].cl_add + low[name].cl_basic),
            "requested_low_speed_ms": float(requested_low),
            "evaluated_low_speed_ms": float(evaluated_low),
            "polar_floor_bound": bool(evaluated_low > requested_low + 1e-12),
            "baseline_section": section_name,
            "thickness_ratio": float(cruise_aero.tc),
            "cm0_cruise": float(cruise_aero.cm0),
            "cm0_low_speed": float(low_aero.cm0),
            "polar_bounds": {
                "reynolds_min": min(reynolds_grid),
                "reynolds_max": max(reynolds_grid),
                "cl_min": min(cl_ladder),
                "cl_max": max(cl_ladder),
            },
            "validity": {
                "cruise_reynolds": not cruise_aero.out_of_range,
                "low_speed_reynolds": not low_aero.out_of_range,
                "cruise_lift": not cruise_lift_flags,
                "low_speed_lift": (
                    not low_lift_flags
                    and float(low_cl * low[name].cl_add + low[name].cl_basic)
                    <= low_aero.clmax
                ),
            },
            "evidence": {
                "grade": "C",
                "source": "committed NeuralFoil polar screen and spanwise.stations",
                "caveat": "model-derived operating state; not tunnel validation",
            },
        }
    return output


def _candidate_pool(station: str, state: Mapping[str, float], baseline_tc: float,
                    limit: int) -> List[Dict[str, Any]]:
    # The window is centred on the already sized station thickness. Its width is not
    # an acceptance threshold: final schedules still face monotonic progression and
    # geometry validation. It is merely a deterministic search-order device [D], and
    # the 10-candidate fixture challenges the retained-set decision.
    window = (max(0.06, baseline_tc - 0.03), min(0.20, baseline_tc + 0.03))
    screened = airfoils.screen(float(state["reynolds_cruise"]), tc_range=window)
    if not screened:
        return []

    # Seed a multi-objective pool: neutral/reflex trim, CLmax, confidence, then names
    # already selected on the sized plan. This prevents a single section-level score
    # from erasing the coordinated whole-wing alternatives before they are evaluated.
    seeds = []
    orders = (
        screened,
        sorted(screened, key=lambda item: (-item.aero.clmax, item.name)),
        sorted(screened, key=lambda item: (-item.aero.cm0, item.name)),
        sorted(screened, key=lambda item: (-item.aero.confidence, item.name)),
    )
    for rank in range(max(12, limit * 2)):
        for ordered in orders:
            if rank < len(ordered) and ordered[rank].name not in seeds:
                seeds.append(ordered[rank].name)

    evaluated: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for name in seeds:
        cruise = airfoils.section_at(name, float(state["reynolds_cruise"]))
        low = airfoils.section_at(name, float(state["reynolds_low_speed"]))
        reasons = []
        if cruise.out_of_range or low.out_of_range:
            reasons.append("polar Reynolds outside committed envelope")
        if name not in airfoil_coordinates.list_sections():
            reasons.append("exact coordinates missing")
        cd, drag_flags = airfoils.section_cd(
            name, float(state["cl_demand_cruise"]), float(state["reynolds_cruise"])
        )
        if float(state["cl_demand_low_speed"]) > low.clmax:
            reasons.append("low-speed section lift demand exceeds CLmax")
        if reasons:
            rejected.append({"name": name, "reason": "; ".join(reasons)})
            continue
        ref = airfoil_coordinates.reference(name)
        evaluated.append({
            "name": name,
            "station": station,
            "tc": cruise.tc,
            "cm0": cruise.cm0,
            "clmax_cruise": cruise.clmax,
            "clmax_low_speed": low.clmax,
            "cd_cruise": None if drag_flags else cd,
            "confidence": min(cruise.confidence, low.confidence),
            "coordinate_sha256": airfoil_coordinates.coordinate_sha256(name),
            "reference": ref,
            "evidence_weakness": (
                1.0 - min(cruise.confidence, low.confidence)
                + (1.0 if ref.get("experimental") is None else 0.0)
            ),
        })

    if len(evaluated) < limit:
        raise ValueError(
            f"{station}: only {len(evaluated)} eligible sections for a {limit}-candidate search"
        )

    ranks = (
        sorted(evaluated, key=lambda item: (
            math.inf if item["cd_cruise"] is None else item["cd_cruise"],
            item["name"],
        )),
        sorted(evaluated, key=lambda item: (-item["clmax_low_speed"], item["name"])),
        sorted(evaluated, key=lambda item: (-item["cm0"], item["name"])),
        sorted(evaluated, key=lambda item: (item["evidence_weakness"], item["name"])),
    )
    retained: List[Dict[str, Any]] = []
    index = 0
    while len(retained) < limit:
        for ranked in ranks:
            candidate = ranked[index]
            if all(item["name"] != candidate["name"] for item in retained):
                retained.append(candidate)
                if len(retained) == limit:
                    break
        index += 1
    return retained


def _coupled_evaluate(schedule: Mapping[str, str], params: BWBParams,
                      cruise_speed_ms: float, altitude_m: float, weight_n: float,
                      launch: str, max_passes: int) -> tuple[BWBParams, Any, Dict[str, Any]]:
    atmosphere = atmosphere_at(altitude_m)
    current = _replace_params(
        params,
        root_airfoil=schedule["root"],
        mid_airfoil=schedule["mid"],
        tip_airfoil=schedule["tip"],
    )
    schedule_hash = _hash(schedule)
    history = []
    trim_burden = math.inf
    converged = False
    for pass_number in range(1, max_passes + 1):
        panel = estimate(current, atmosphere, cruise_speed_ms, weight_n, launch)
        trim_burden = abs(panel.cm_trim_residual)
        if not panel.trim_twist_reachable or panel.tip_twist_for_trim_deg is None:
            raise ValueError("reachable washout-only trim is unavailable")
        current = _replace_params(current, tip_twist_deg=panel.tip_twist_for_trim_deg)
        panel = estimate(current, atmosphere, cruise_speed_ms, weight_n, launch)

        launch_limit = LAUNCH_VSTALL_MAX[launch]
        if panel.v_stall_ms > launch_limit:
            required_area = (
                2.0 * weight_n
                / (atmosphere.density_kgm3 * launch_limit ** 2 * panel.cl_max_3d)
            )
            if required_area > current.planform_area_m2() * (1.0 + 1e-9):
                current = solve_planform(
                    required_area,
                    current.aspect_ratio(),
                    ShapeFractions.from_params(current),
                )
                panel = estimate(current, atmosphere, cruise_speed_ms, weight_n, launch)

        trim_burden = abs(panel.cm_trim_residual)
        coupled_station_state = _station_state(
            current, cruise_speed_ms, altitude_m, weight_n, launch
        )
        history.append({
            "pass": pass_number,
            "schedule_hash": schedule_hash,
            "plan_hash": _hash(current.model_dump()),
            "station_state_hash": _hash(coupled_station_state),
            "trim_residual": trim_burden,
            "trims_as_built": panel.trims_as_built,
        })
        if (len(history) >= 2
                and history[-1]["schedule_hash"] == history[-2]["schedule_hash"]
                and panel.trims_as_built):
            converged = True
            break

    if not converged and max_passes > 1:
        raise ValueError(
            f"schedule/trim state did not stabilize within {max_passes} passes"
        )
    if not panel.cruise_feasible:
        raise ValueError("cruise condition is below whole-wing stall")
    if panel.cd_sections_span_clamped > 0.0:
        raise ValueError("trimmed whole-wing profile drag uses clamped polar data")
    if not panel.trim_twist_reachable:
        raise ValueError("trim twist left the reachable schema range after resizing")
    if panel.static_margin_at_trim is None or panel.static_margin_at_trim <= 0.0:
        raise ValueError("trimmed CG is not statically stable")
    if panel.launch_ok is False:
        raise ValueError("resized schedule does not meet the launch stall gate")
    return current, panel, {
        "converged": converged,
        "passes": history,
        "trim_burden": trim_burden,
    }


def pareto_filter(records: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Return a stable defensive copy of the non-dominated objective records."""
    result = []
    for index, record in enumerate(records):
        a = record["objectives"]
        dominated = False
        for other_index, other in enumerate(records):
            if index == other_index:
                continue
            b = other["objectives"]
            no_worse = all(b[key] <= a[key] for key in OBJECTIVES)
            strictly_better = any(b[key] < a[key] for key in OBJECTIVES)
            if no_worse and strictly_better:
                dominated = True
                break
        if not dominated:
            result.append(deepcopy(dict(record)))
    return result


def _ordinal_regret(records: Sequence[Mapping[str, Any]], metrics: Iterable[str]) -> Dict[str, Dict[str, float]]:
    """Normalize heterogeneous objectives by stable ordinal regret, not fake units."""
    metrics = tuple(metrics)
    regrets = {record["schedule_hash"]: {} for record in records}
    denominator = max(1, len(records) - 1)
    for metric in metrics:
        ordered = sorted(records, key=lambda item: (
            item["objectives"][metric], item["schedule_hash"]
        ))
        for rank, item in enumerate(ordered):
            regrets[item["schedule_hash"]][metric] = rank / denominator
    return regrets


def _rank(records: Sequence[Mapping[str, Any]], strategy: str,
          metrics: Iterable[str] = OBJECTIVES) -> List[Dict[str, Any]]:
    metrics = tuple(metrics)
    if strategy == "balanced":
        regret = _ordinal_regret(records, metrics)
        key = lambda item: (
            max(regret[item["schedule_hash"]].values(), default=0.0),
            sum(regret[item["schedule_hash"]].values()),
            item["schedule_hash"],
        )
    elif strategy == "efficiency":
        order = tuple(metric for metric in (
            "profile_drag", "stall_control", "trim", "evidence") if metric in metrics)
        key = lambda item: tuple(item["objectives"][metric] for metric in order) + (
            item["schedule_hash"],)
    elif strategy == "low_speed_control":
        order = tuple(metric for metric in (
            "stall_control", "trim", "profile_drag", "evidence") if metric in metrics)
        key = lambda item: tuple(item["objectives"][metric] for metric in order) + (
            item["schedule_hash"],)
    else:
        raise ValueError(f"unknown airfoil strategy {strategy!r}")
    return [deepcopy(dict(item)) for item in sorted(records, key=key)]


def _leave_one_out(front: Sequence[Mapping[str, Any]], strategy: str,
                   winner_hash: str) -> Dict[str, Dict[str, Any]]:
    output = {}
    for omitted in OBJECTIVES:
        winner = _rank(front, strategy, (m for m in OBJECTIVES if m != omitted))[0]
        output[omitted] = {
            "winner_hash": winner["schedule_hash"],
            "changed": winner["schedule_hash"] != winner_hash,
            "schedule": deepcopy(winner["schedule"]),
        }
    return output


def _mission_badge(brief: str, launch_bind: bool) -> str:
    if launch_bind or re.search(r"\b(stall|hand[ -]?launch|low[ -]?speed)\b", brief,
                                re.IGNORECASE):
        return "low_speed_control"
    if _EFFICIENCY_INTENT.search(brief):
        return "efficiency"
    return "balanced"


@lru_cache(maxsize=32)
def _recommend_cached(params_json: str, cruise_speed_ms: float, altitude_m: float,
                      weight_n: float, launch: str, brief: str,
                      candidate_limit: int, max_passes: int) -> Dict[str, Any]:
    params = BWBParams.model_validate_json(params_json)
    if launch not in LAUNCH_VSTALL_MAX:
        raise ValueError(f"unknown launch method {launch!r}")
    if candidate_limit < 3:
        raise ValueError("at least three station candidates are required")
    if max_passes < 2:
        raise ValueError("at least two coupled passes are required to prove stability")

    state = _station_state(params, cruise_speed_ms, altitude_m, weight_n, launch)
    baseline_tc = {
        "root": params.root_thickness_pct / 100.0,
        "mid": params.mid_thickness_pct / 100.0,
        "tip": params.tip_thickness_pct / 100.0,
    }
    pools = {
        station: _candidate_pool(station, state[station], baseline_tc[station],
                                 candidate_limit)
        for station in ("root", "mid", "tip")
    }
    by_station = {
        station: {item["name"]: item for item in values}
        for station, values in pools.items()
    }

    considered: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for root, mid, tip in itertools.product(
            pools["root"], pools["mid"], pools["tip"]):
        schedule = {"root": root["name"], "mid": mid["name"], "tip": tip["name"]}
        schedule_hash = _hash(schedule)
        if not (root["tc"] >= mid["tc"] >= tip["tc"]):
            rejected.append({
                "schedule": schedule,
                "schedule_hash": schedule_hash,
                "reason": "thickness must progress monotonically root >= mid >= tip",
            })
            continue
        try:
            stable, panel, convergence = _coupled_evaluate(
                schedule, params, cruise_speed_ms, altitude_m, weight_n, launch,
                max_passes,
            )
        except (ValueError, ArithmeticError) as exc:
            rejected.append({
                "schedule": schedule,
                "schedule_hash": schedule_hash,
                "reason": str(exc),
            })
            continue
        evidence = sum(item["evidence_weakness"] for item in (root, mid, tip))
        considered.append({
            "schedule": schedule,
            "schedule_hash": schedule_hash,
            "coordinate_hashes": {
                station: by_station[station][name]["coordinate_sha256"]
                for station, name in schedule.items()
            },
            "params": stable.model_dump(),
            "objectives": {
                "profile_drag": panel.cd_profile_sections,
                "stall_control": [int(panel.stall_is_outboard), panel.v_stall_ms],
                "trim": convergence["trim_burden"],
                "evidence": evidence,
            },
            "metrics": {
                "cl_cruise": panel.cl_cruise,
                "cd_profile_sections": panel.cd_profile_sections,
                "v_stall_ms": panel.v_stall_ms,
                "stall_station_frac": panel.stall_station_frac,
                "stall_is_outboard": panel.stall_is_outboard,
                "cm_trim_residual": panel.cm_trim_residual,
                "tip_twist_for_trim_deg": panel.tip_twist_for_trim_deg,
                "static_margin_at_trim": panel.static_margin_at_trim,
            },
            "convergence": convergence,
        })

    front = pareto_filter(considered)
    if len(front) < len(STRATEGIES):
        raise ValueError(
            f"only {len(front)} distinct Pareto schedules survived; three are required"
        )

    chosen = []
    used = set()
    for strategy in STRATEGIES:
        ranked = _rank(front, strategy)
        winner = next((item for item in ranked if item["schedule_hash"] not in used), None)
        if winner is None:
            raise ValueError("three distinct strategy schedules are unavailable")
        used.add(winner["schedule_hash"])
        detail = deepcopy(winner)
        detail["strategy"] = strategy
        detail["ranking"] = {
            "method": (
                "minimax ordinal regret" if strategy == "balanced"
                else "drag-first lexicographic" if strategy == "efficiency"
                else "stall/control-first lexicographic"
            ),
            "ranked_hashes": [item["schedule_hash"] for item in ranked],
        }
        detail["stations"] = {
            station: deepcopy(by_station[station][name])
            for station, name in winner["schedule"].items()
        }
        detail["leave_one_metric_out"] = _leave_one_out(
            front, strategy, winner["schedule_hash"]
        )
        chosen.append(detail)

    base_panel = estimate(
        params, atmosphere_at(altitude_m), cruise_speed_ms, weight_n, launch
    )
    return {
        "schema_version": 1,
        "mission_badge": _mission_badge(brief, base_panel.launch_ok is False),
        "station_state": state,
        "strategies": chosen,
        "pareto_front": front,
        "search": {
            "candidate_limit_per_station": candidate_limit,
            "max_coupled_passes": max_passes,
            "station_candidates": {
                station: [item["name"] for item in values]
                for station, values in pools.items()
            },
            "strategy_candidates": {
                strategy: {
                    station: [item["name"] for item in values]
                    for station, values in pools.items()
                }
                for strategy in STRATEGIES
            },
            "considered": considered,
            "rejected": rejected,
        },
        "provenance": {
            "algorithm": "coordinated-airfoil-pareto-v1",
            "polar_table_sha256": airfoils.table_sha256(),
            "coordinate_archive_sha256": airfoil_coordinates.archive_sha256(),
            "aerosandbox": airfoil_coordinates.provenance()["aerosandbox"],
            "candidate_limit_grade": "D",
            "max_passes_grade": "D",
        },
        "bound_sensitivity": {
            "production_candidate_limit": PRODUCTION_CANDIDATE_LIMIT,
            "production_max_passes": PRODUCTION_MAX_PASSES,
            "tested_candidate_limit": candidate_limit,
            "tested_max_passes": max_passes,
            "changed": False,
            "warning": None,
        },
    }


def recommend_airfoil_sets(*, params: BWBParams, cruise_speed_ms: float,
                           altitude_m: float, weight_n: float, launch: str,
                           brief: str = "",
                           candidate_limit: int = PRODUCTION_CANDIDATE_LIMIT,
                           max_passes: int = PRODUCTION_MAX_PASSES) -> Dict[str, Any]:
    """Return a JSON-safe deterministic recommendation payload."""
    raw = deepcopy(_recommend_cached(
        params.model_dump_json(), float(cruise_speed_ms), float(altitude_m),
        float(weight_n), launch, brief, int(candidate_limit), int(max_passes)
    ))
    if (candidate_limit, max_passes) != (
            PRODUCTION_CANDIDATE_LIMIT, PRODUCTION_MAX_PASSES):
        production = deepcopy(_recommend_cached(
            params.model_dump_json(), float(cruise_speed_ms), float(altitude_m),
            float(weight_n), launch, brief,
            PRODUCTION_CANDIDATE_LIMIT, PRODUCTION_MAX_PASSES,
        ))
        production_hashes = [item["schedule_hash"] for item in production["strategies"]]
        tested_hashes = [item["schedule_hash"] for item in raw["strategies"]]
        changed = production_hashes != tested_hashes
        raw["bound_sensitivity"].update({
            "changed": changed,
            "production_schedule_hashes": production_hashes,
            "tested_schedule_hashes": tested_hashes,
            "warning": (
                "Wider/longer sensitivity changed at least one strategy schedule"
                if changed else None
            ),
        })
    return raw
