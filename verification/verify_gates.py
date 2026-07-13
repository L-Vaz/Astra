"""V3 — Gate adversarial: bad input dies at the right gate, good input proceeds.

Phase-1 scope (geometry gates). Pattern per user spec: alternate bad and good
inputs; the pipeline must STOP on bad (correct gate, reason code recorded,
nothing built downstream) and PROCEED on good. Mesh-settings and numerics
gates extend this script in Phase 2.

No LLM calls.
"""
import os
import sys

from _common import REPO_ROOT, RUNS_DIR, Table


def expect_reject(t, label, fn, must_mention: str):
    """Bad input: fn must raise, and the reason must name the real cause."""
    try:
        fn()
        t.add(label, False, "was accepted — should have been rejected")
        return None
    except Exception as e:
        msg = str(e)
        ok = must_mention.lower() in msg.lower()
        t.add(label, ok, (msg[:140] + "…") if len(msg) > 140 else msg)
        return e


def main() -> int:
    t = Table("V3 GATES")
    outbase = os.path.join(RUNS_DIR, "verify_gates")

    from domain_packs.bwb.schema import MissionSpec
    from domain_packs.bwb.defaults import derive
    from services.geometry import GeometryRejected, apply_overrides, build_geometry

    good = derive(MissionSpec(payload_kg=2.0, cruise_speed_ms=25.0,
                              cruise_altitude_m=100.0)).params

    # --- bad: negative span (schema range) ---
    expect_reject(t, "reject: negative span",
                  lambda: apply_overrides(good, {"span_m": -1.5}), "span_m")

    # --- good: nominal build proceeds ---
    r = build_geometry(good, os.path.join(outbase, "good_1"))
    t.add("proceed: nominal geometry", r.all_gates_pass,
          f"S={r.s_ref_m2:.3f} m2, watertight={r.watertight}")

    # --- bad: impossible taper -> unmanufacturable tip chord (cross-check) ---
    expect_reject(t, "reject: tip chord below manufacturable minimum",
                  lambda: apply_overrides(good, {"root_chord_m": 0.25,
                                                 "mid_chord_frac": 0.35,
                                                 "taper_outer": 0.15}), "tip chord")

    # --- bad: wash-in twist (destabilizing for tailless BWB) ---
    expect_reject(t, "reject: wash-in twist (tip > root)",
                  lambda: apply_overrides(good, {"root_twist_deg": 0.0,
                                                 "tip_twist_deg": 2.0}), "twist")

    # --- bad: unknown parameter name ---
    expect_reject(t, "reject: unknown parameter",
                  lambda: apply_overrides(good, {"wing_area": 2.0}), "unknown")

    # --- bad: parameters that break aspect-ratio sanity ---
    expect_reject(t, "reject: absurd aspect ratio",
                  lambda: apply_overrides(good, {"span_m": 3.0, "root_chord_m": 0.25,
                                                 "mid_chord_frac": 0.5}), "aspect ratio")

    # --- bad params straight to the builder (gate must also hold at builder level) ---
    try:
        bad_raw = good.model_dump()
        bad_raw["span_m"] = 99.0  # out of schema range
        import json
        os.makedirs(os.path.join(outbase, "bad_builder"), exist_ok=True)
        with open(os.path.join(outbase, "bad_builder", "params.json"), "w") as f:
            json.dump(bad_raw, f)
        import subprocess
        from services.geometry import VSP_PYTHON, _BUILDER
        proc = subprocess.run([VSP_PYTHON, _BUILDER,
                               "--params", os.path.join(outbase, "bad_builder", "params.json"),
                               "--outdir", os.path.join(outbase, "bad_builder")],
                              capture_output=True, text=True, timeout=300)
        rejected = proc.returncode != 0 and "param_ranges" in (proc.stdout + proc.stderr)
        t.add("reject: builder-level param gate", rejected,
              proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "no output")
    except Exception as e:
        t.add("reject: builder-level param gate", False, repr(e)[:140])

    # --- good again: loop moves forward after rejects ---
    r2 = build_geometry(apply_overrides(good, {"sweep_outer_deg": 25.0}),
                        os.path.join(outbase, "good_2"))
    t.add("proceed: good input after rejects", r2.all_gates_pass,
          f"sweep_outer=25 deg, S={r2.s_ref_m2:.3f} m2")

    # ================= Phase 2: mesh-settings gates =================
    from services.bwb_mesh import MeshRejected, generate_mesh
    stl = r.artifacts["stl"]

    # bad: absurdly small cell budget -> must stop BEFORE any solve
    try:
        generate_mesh(stl_path=stl, mac_m=r.mean_aero_chord_m,
                      span_m=r.params.span_m,
                      outdir=os.path.join(outbase, "bad_mesh_budget"),
                      level="coarse", max_cells=5000)
        t.add("reject: mesh over cell budget", False, "was accepted")
    except MeshRejected as e:
        t.add("reject: mesh over cell budget", "budget" in str(e).lower()
              or "count" in str(e).lower(), str(e)[:140])

    # bad: nonexistent refinement level -> generator gate
    try:
        generate_mesh(stl_path=stl, mac_m=r.mean_aero_chord_m,
                      span_m=r.params.span_m,
                      outdir=os.path.join(outbase, "bad_mesh_level"),
                      level="ultra")
        t.add("reject: invalid mesh level", False, "was accepted")
    except (MeshRejected, Exception) as e:
        t.add("reject: invalid mesh level", True, str(e)[:140])

    # good: coarse mesh proceeds
    m = generate_mesh(stl_path=stl, mac_m=r.mean_aero_chord_m,
                      span_m=r.params.span_m,
                      outdir=os.path.join(outbase, "good_mesh"), level="coarse")
    t.add("proceed: good mesh after rejects", m["passed"],
          f"{m['stats']['cells']} cells, checkMesh OK")

    # ================= Phase 2: numerics gates (evaluator) =================
    # Requires a completed golden case (run verify_golden.py with LLM first).
    golden_case = os.path.join(RUNS_DIR, "verify_golden", "simulation", "case")
    if os.path.isdir(golden_case):
        import shutil
        import subprocess
        from services.evaluate import evaluate_case
        foam = os.environ.get("FOAM_BASHRC", "/opt/openfoam10/etc/bashrc")

        # bad: sabotage fvSchemes with an invalid divergence scheme, fresh run
        bad_case = os.path.join(outbase, "bad_numerics_case")
        if os.path.exists(bad_case):
            shutil.rmtree(bad_case)
        shutil.copytree(golden_case, bad_case,
                        ignore=shutil.ignore_patterns("VTK", "postProcessing", "*.foam"))
        # remove result time dirs so the evaluator can't see old good data
        for d in os.listdir(bad_case):
            if d.replace(".", "").isdigit() and d != "0":
                shutil.rmtree(os.path.join(bad_case, d))
        subprocess.run(["bash", "-lc",
                        f"source {foam} >/dev/null 2>&1; "
                        "foamDictionary system/fvSchemes -entry divSchemes/'div(phi,U)' "
                        "-set 'Gauss bogusScheme'"],
                       cwd=bad_case, capture_output=True, text=True)
        subprocess.run(["bash", "-lc",
                        f"source {foam} >/dev/null 2>&1; "
                        "simpleFoam > log.simpleFoam 2>&1 || true"],
                       cwd=bad_case, capture_output=True, text=True, timeout=1200)
        ev_bad = evaluate_case(case_dir=bad_case,
                               outdir=os.path.join(outbase, "bad_numerics_eval"),
                               required_cl=0.181, vlm_cl=0.181)
        t.add("reject: bad numerics (bogus div scheme) caught by evaluator",
              ev_bad["verdict"] in ("reject", "revise-numerics"),
              f"verdict={ev_bad['verdict']}")

        # good: the golden case itself must NOT be rejected
        ev_good = evaluate_case(case_dir=golden_case,
                                outdir=os.path.join(outbase, "good_numerics_eval"),
                                required_cl=0.181, vlm_cl=0.181)
        t.add("proceed: good numerics accepted by evaluator",
              ev_good["verdict"] in ("pass", "revise-design"),
              f"verdict={ev_good['verdict']}")
    else:
        t.add("numerics gates", False,
              "no golden case found — run verify_golden.py (with LLM) first")

    return t.finish()


if __name__ == "__main__":
    sys.exit(main())
