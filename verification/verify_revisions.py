"""V2 — Meaningful revisions: verify the versioning/rollback machinery.

Phase-1 scope (geometry stage): build a geometry, revise it with a meaningful
parameter delta (span +10%), and assert: a new version is created, the old
version is kept intact, downstream stages are invalidated, and the new STL
actually reflects the change. Mesh/simulation/visualization revisions extend
this script in Phase 2.

No LLM calls — deterministic MissionSpec.
"""
import json
import os
import sys

from _common import REPO_ROOT, RUNS_DIR, Table


def main() -> int:
    t = Table("V2 REVISIONS")
    os.environ.setdefault("ASTRA_SESSIONS_DIR",
                          os.path.join(RUNS_DIR, "verify_revisions_sessions"))

    from domain_packs.bwb.schema import MissionSpec
    from domain_packs.bwb.defaults import derive
    from services import design_session as ds
    from services.geometry import apply_overrides, build_geometry

    derived = derive(MissionSpec(payload_kg=2.0, cruise_speed_ms=25.0,
                                 cruise_altitude_m=100.0))
    sid = ds.create_session(label="verify_revisions")
    t.add("session created", True, sid)

    # v1 geometry
    v1_dir = ds.new_version_dir(sid, "geometry")
    r1 = build_geometry(derived.params, v1_dir)
    ds.record_version(sid, "geometry", {"overrides": {}})
    t.add("geometry v1 built", r1.all_gates_pass,
          f"span={r1.params.span_m:.3f} m, S={r1.s_ref_m2:.3f} m2")

    # pretend mesh happened so we can verify downstream invalidation
    ds.new_version_dir(sid, "mesh")
    ds.record_version(sid, "mesh", {"note": "placeholder mesh for invalidation test"})
    t.add("placeholder mesh recorded", ds.latest_dir(sid, "mesh") is not None)

    # meaningful revision: span +10%
    new_span = round(r1.params.span_m * 1.10, 4)
    p2 = apply_overrides(r1.params, {"span_m": new_span})
    v2_dir = ds.new_version_dir(sid, "geometry")
    r2 = build_geometry(p2, v2_dir)
    ds.record_version(sid, "geometry", {"overrides": {"span_m": new_span}})
    t.add("geometry v2 built with span +10%", r2.all_gates_pass,
          f"span {r1.params.span_m:.3f} -> {r2.params.span_m:.3f} m")

    # assertions on the machinery
    state = ds.get_state(sid)
    t.add("v2 is latest", state["stages"]["geometry"]["latest"] == 2)
    t.add("v1 kept on disk",
          os.path.exists(os.path.join(v1_dir, "bwb.stl"))
          and os.path.exists(os.path.join(v1_dir, "geometry_report.json")))
    t.add("downstream mesh invalidated",
          state["stages"]["mesh"]["valid"] is False and ds.latest_dir(sid, "mesh") is None)

    # the change is physically real in the exported STL
    with open(os.path.join(v2_dir, "geometry_report.json")) as f:
        rep2 = json.load(f)
    stl_span = rep2["bbox_m"][4] - rep2["bbox_m"][1]
    t.add("STL reflects new span", abs(stl_span - new_span) / new_span < 0.05,
          f"STL span {stl_span:.3f} vs requested {new_span:.3f}")

    # sanity: S_ref grew with span (same chords)
    t.add("S_ref increased", rep2["s_ref_m2"] > r1.s_ref_m2)

    # ---- mesh revision: coarse -> medium refinement is a real change ----
    from services.bwb_mesh import generate_mesh
    m1_dir = ds.new_version_dir(sid, "mesh")
    m1 = generate_mesh(stl_path=os.path.join(v2_dir, "bwb.stl"),
                       mac_m=r2.mean_aero_chord_m, span_m=r2.params.span_m,
                       outdir=m1_dir, level="coarse")
    ds.record_version(sid, "mesh", {"level": "coarse"})
    t.add("mesh v1 (coarse) built", m1["passed"], f"{m1['stats']['cells']} cells")

    m2_dir = ds.new_version_dir(sid, "mesh")
    m2 = generate_mesh(stl_path=os.path.join(v2_dir, "bwb.stl"),
                       mac_m=r2.mean_aero_chord_m, span_m=r2.params.span_m,
                       outdir=m2_dir, level="medium")
    ds.record_version(sid, "mesh", {"level": "medium"})
    t.add("mesh v2 (medium) has more cells",
          m2["passed"] and m2["stats"]["cells"] > 1.5 * m1["stats"]["cells"],
          f"{m1['stats']['cells']} -> {m2['stats']['cells']} cells, checkMesh OK both")
    t.add("mesh v1 kept on disk", os.path.exists(os.path.join(m1_dir, "bwb.msh")))

    # ---- simulation revision: documented restart with extended endTime ----
    # Requires a completed golden case (run verify_golden.py first).
    golden_case = os.path.join(RUNS_DIR, "verify_golden", "simulation", "case")
    if os.path.isdir(golden_case):
        import re
        import shutil
        import subprocess
        rev_case = os.path.join(RUNS_DIR, "verify_revisions_sessions",
                                sid, "sim_revision_case")
        shutil.copytree(golden_case, rev_case,
                        ignore=shutil.ignore_patterns("VTK", "*.foam"))
        foam = os.environ.get("FOAM_BASHRC", "/opt/openfoam10/etc/bashrc")

        def foam_run(cmd):
            return subprocess.run(["bash", "-lc",
                                   f"source {foam} >/dev/null 2>&1; {cmd}"],
                                  cwd=rev_case, capture_output=True, text=True,
                                  timeout=3600)

        end0 = foam_run("foamDictionary system/controlDict -entry endTime -value").stdout.strip()
        new_end = int(float(end0)) + 100
        foam_run(f"foamDictionary system/controlDict -entry endTime -set {new_end}")
        foam_run("foamDictionary system/controlDict -entry startFrom -set latestTime")
        # ensure the solver writes fields at the new endTime
        foam_run("foamDictionary system/controlDict -entry writeInterval -set 100")
        res = foam_run("simpleFoam > log.simpleFoam.continue 2>&1; echo rc=$?")
        cont_log = os.path.join(rev_case, "log.simpleFoam.continue")
        ok = os.path.exists(cont_log) and "rc=0" in res.stdout
        t.add("simulation revision: restart to extended endTime",
              ok and os.path.isdir(os.path.join(rev_case, str(new_end))),
              f"endTime {end0} -> {new_end} (documented latestTime restart)")

        # ---- visualization revision: extra slice plane in the report ----
        from services.evaluate import evaluate_case
        ev_dir = os.path.join(RUNS_DIR, "verify_revisions_sessions",
                              sid, "eval_revision")
        ev = evaluate_case(case_dir=rev_case, outdir=ev_dir, extra_slices=["z"])
        t.add("visualization revision: extra z-slice rendered",
              "render_U_slice_z" in ev["artifacts"],
              ", ".join(os.path.basename(v) for v in ev["artifacts"].values()))
    else:
        t.add("simulation revision", False,
              "no golden case found — run verify_golden.py (with LLM) first")
        t.add("visualization revision", False, "needs the simulation revision case")

    return t.finish()


if __name__ == "__main__":
    sys.exit(main())
