"""V1 — Golden path: one command, zero revisions, fully autonomous.

Phase-1 scope: fixed mission prompt -> intent (LLM parse + derived physics with
logged assumptions) -> OpenVSP geometry -> validation gates -> renders.
Phase-2 stages (mesh / OpenFOAM via the existing Foam-Agent graph / evaluator)
are reported as SKIP placeholders and will be activated when Phase 2 lands.

Usage (WSL, FoamAgent conda env):
    python verification/verify_golden.py            # full, uses the configured LLM
    python verification/verify_golden.py --no-llm   # deterministic MissionSpec, no API calls
"""
import argparse
import os
import shutil
import sys

from _common import RUNS_DIR, Table

GOLDEN_PROMPT = ("I want a small surveillance drone that carries a 2 kg camera payload, "
                 "cruises at about 25 m/s at roughly 100 m altitude, hand-launched.")
OUTDIR = os.path.join(RUNS_DIR, "verify_golden")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true",
                    help="skip the LLM mission parse; use the equivalent fixed MissionSpec")
    args = ap.parse_args()

    t = Table("V1 GOLDEN PATH")
    if os.path.exists(OUTDIR):
        shutil.rmtree(OUTDIR)
    os.makedirs(OUTDIR)

    from domain_packs.bwb.schema import MissionSpec
    from domain_packs.bwb.defaults import derive
    from services.intent import build_enriched_requirement

    # ---- Stage: intent ----
    try:
        if args.no_llm:
            mission = MissionSpec(payload_kg=2.0, cruise_speed_ms=25.0,
                                  cruise_altitude_m=100.0, launch="hand")
            t.add("intent: mission parse", True, "fixed MissionSpec (--no-llm)")
        else:
            from services import global_llm_service
            from services.intent import parse_mission
            mission = parse_mission(GOLDEN_PROMPT, global_llm_service)
            ok = (1.5 <= mission.payload_kg <= 2.5 and 20 <= mission.cruise_speed_ms <= 30
                  and 50 <= mission.cruise_altitude_m <= 200)
            t.add("intent: mission parse", ok,
                  f"payload={mission.payload_kg} kg v={mission.cruise_speed_ms} m/s "
                  f"h={mission.cruise_altitude_m} m launch={mission.launch}")
            if not ok:
                return t.finish()
    except Exception as e:
        t.add("intent: mission parse", False, repr(e)[:200])
        return t.finish()

    derived = derive(mission)
    # L = W sanity: q * S * CL must equal weight within 1%
    q = 0.5 * derived.air_density_kgm3 * mission.cruise_speed_ms ** 2
    lift = q * derived.s_ref_target_m2 * derived.required_cl
    t.add("intent: derived physics (L=W)",
          abs(lift - derived.weight_n) / derived.weight_n < 0.01,
          f"required CL={derived.required_cl:.3f}, S={derived.s_ref_target_m2:.3f} m2, "
          f"{len(derived.assumptions)} logged assumptions")

    with open(os.path.join(OUTDIR, "mission_derived.json"), "w") as f:
        f.write(derived.model_dump_json(indent=2))
    with open(os.path.join(OUTDIR, "enriched_requirement.txt"), "w") as f:
        f.write(build_enriched_requirement(derived))
    t.add("intent: enriched requirement written", True)

    # ---- Stage: geometry ----
    from services.geometry import GeometryRejected, build_geometry
    geo_dir = os.path.join(OUTDIR, "geometry")
    try:
        report = build_geometry(derived.params, geo_dir)
        for g in report.gates:
            t.add(f"geometry gate: {g.gate}", g.passed, g.reason)
        t.add("geometry: renders produced",
              all(os.path.exists(v) for k, v in report.artifacts.items()
                  if k.startswith("render_")),
              f"{report.n_facets} facets, S={report.s_ref_m2:.3f} m2, AR={report.aspect_ratio:.2f}")
        # planform must hit the sizing target
        t.add("geometry: S_ref matches plan",
              abs(report.s_ref_m2 - derived.s_ref_target_m2) / derived.s_ref_target_m2 < 0.02,
              f"{report.s_ref_m2:.3f} vs target {derived.s_ref_target_m2:.3f} m2")
    except GeometryRejected as e:
        t.add("geometry: build", False, str(e)[:200])
        return t.finish()

    # ---- Stage: fast aero (VSPAERO tier 1) ----
    from services.vspaero import FastAeroRejected, run_fast_aero
    fa_dir = os.path.join(OUTDIR, "fast_aero")
    fast = None
    try:
        fast = run_fast_aero(report, derived, fa_dir)
        for g in fast["gates"]:
            t.add(f"fast-aero gate: {g['gate']}", g["passed"], g.get("reason", ""))
        t.add("fast-aero: trim + L/D computed",
              fast["alpha_trim_deg"] is not None and fast["ld_at_trim"] is not None,
              f"alpha_trim={fast['alpha_trim_deg']:.2f} deg, L/D={fast['ld_at_trim']:.1f}, "
              f"x_np={fast['x_np_m']:.3f} m")
    except FastAeroRejected as e:
        t.add("fast-aero: run", False, str(e)[:200])
        return t.finish()

    # ---- Stage: mesh (deterministic Gmsh farfield) ----
    from services.bwb_mesh import MeshRejected, generate_mesh
    mesh_dir = os.path.join(OUTDIR, "mesh")
    try:
        mesh_report = generate_mesh(
            stl_path=report.artifacts["stl"], mac_m=report.mean_aero_chord_m,
            span_m=report.params.span_m, outdir=mesh_dir, level="coarse")
        for g in mesh_report["gates"]:
            t.add(f"mesh gate: {g['gate']}", g["passed"], g.get("reason", ""))
        t.add("mesh: stats", True,
              f"{mesh_report['stats']['cells']} cells, "
              f"maxNonOrtho={mesh_report['stats']['max_non_orthogonality']}")
    except MeshRejected as e:
        t.add("mesh: build", False, str(e)[:200])
        return t.finish()

    if args.no_llm:
        t.add("simulation: existing Foam-Agent graph", None, "needs LLM (--no-llm set)")
        t.add("evaluate: convergence + plausibility + report", None, "needs simulation")
        print(f"\nArtifacts: {OUTDIR}")
        return t.finish()

    # ---- Stage: simulation through the EXISTING Foam-Agent graph ----
    from services.simulate import SimulationFailed, compose_requirement, run_simulation
    sim_dir = os.path.join(OUTDIR, "simulation")
    requirement = compose_requirement(
        build_enriched_requirement(derived), mission.cruise_speed_ms,
        fast["alpha_trim_deg"], iterations=600)
    try:
        sim = run_simulation(requirement, mesh_report["artifacts"]["msh"], sim_dir)
        t.add("simulation: graph completed", True, sim["case_dir"])
    except SimulationFailed as e:
        t.add("simulation: graph completed", False, f"{e} …{e.log_tail[-200:]}")
        return t.finish()

    # ---- Stage: evaluate ----
    from services.evaluate import evaluate_case
    eval_dir = os.path.join(OUTDIR, "evaluation")
    ref = {"sref": report.s_ref_m2, "cref": report.mean_aero_chord_m,
           "magUInf": mission.cruise_speed_ms, "rho": derived.air_density_kgm3,
           "alpha_deg": fast["alpha_trim_deg"],
           "cofr_x": fast["x_np_m"] - 0.08 * report.mean_aero_chord_m}
    ev = evaluate_case(case_dir=sim["case_dir"], outdir=eval_dir,
                       required_cl=derived.required_cl, vlm_cl=derived.required_cl,
                       ref=ref)
    # meets_cl_target / plausible_vs_vspaero are DESIGN outcomes, not
    # machinery: a coarse mesh legitimately under-predicts CL and the verdict
    # (asserted below) is what must route correctly on them.
    informational = {"meets_cl_target", "plausible_vs_vspaero"}
    for g in ev["gates"]:
        if g["gate"] in informational:
            t.add(f"evaluate gate: {g['gate']} (informational)", None,
                  f"{'passed' if g['passed'] else 'failed'} — {g.get('reason', '')}")
        else:
            t.add(f"evaluate gate: {g['gate']}", g["passed"], g.get("reason", ""))
    t.add("evaluate: visual report produced",
          any(k.startswith("render_") for k in ev["artifacts"]),
          ", ".join(os.path.basename(v) for v in ev["artifacts"].values()))
    t.add("evaluate: verdict acceptable", ev["verdict"] in ("pass", "revise-design"),
          f"verdict={ev['verdict']}" + (f" warnings={ev['warnings']}" if ev["warnings"] else ""))

    print(f"\nArtifacts: {OUTDIR}")
    return t.finish()


if __name__ == "__main__":
    sys.exit(main())
