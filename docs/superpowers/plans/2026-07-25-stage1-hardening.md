# Stage-1 Hardening and Test Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan stage-by-stage.
> Steps use checkbox (`- [ ]`) syntax for tracking. Companion ledger:
> [`specs/2026-07-25-stage1-limitations-and-hardening-design.md`](../specs/2026-07-25-stage1-limitations-and-hardening-design.md)
> — that document owns the model-fidelity ledger, the disclosure strings, and the
> golden-update policy. This document owns the ordered work and its tests.

**Goal:** Make Stage 1 (freeform brief → tiered requirements → feasibility → five
concepts → promote) trustworthy: no request can brick or corrupt a session, no
concept violates a cap the user typed, and nothing the screen says about a number
is false.

**Architecture:** Fix in dependency order — store integrity first, then crashes,
then the honesty layer (cheap, text/flags only), then the constraint solver, then
the candidate lifecycle, then the physics core. Every fix is locked by a test, and
every known-broken behaviour is encoded *now* as `xfail(strict=True)` so the suite
is the live backlog and each fix flips exactly one marker.

**Tech stack:** Python 3.12 (FoamAgent conda env), pydantic 2, pytest 8.3.5,
`hypothesis==6.161.2`, `pytest-cov==7.1.0`, `coverage==7.15.2`, `fastapi.testclient`,
Node v24.13.1 for the ES-module parity layer.

**Provenance of the findings:** every item below is probe-confirmed by executed
code unless explicitly labelled `INFERRED`. Section J (solver fuzz) is **COMPLETE**:
648,000 legal `DesignRequirements` combinations, seed `20260725`, 0 exceptions, all
structural invariants clean, one violated invariant (the stall cap, 33.1% of the
envelope) plus a launch-cap reporting hole — see S6-J1…J4.

---

## Global constraints

- **CLAUDE.md rules bind.** No LLM step becomes load-bearing for correctness; all
  domain knowledge stays in `domain_packs/bwb/`; every physics constant is either
  cited to an official source or explicitly labelled engineering judgment. Several
  items below exist *only* to satisfy the third rule.
- **`domain_packs/*` must stay dual-env importable** (py3.10 OpenVSP venv and py3.12
  FoamAgent): pydantic/numpy only, no OpenVSP, no FAISS, no `src/` imports.
- **Do not touch the inherited LangGraph CFD spine.** All work here is pre-CFD.
- **Test environment split.**
  - `domain_packs/*` tests run on Windows python (pure pydantic) *and* in WSL.
  - Anything importing `src/services` or `src/cockpit/server.py` is **WSL only** —
    it eagerly loads FAISS/sentence-transformers (~30 s). Batch such work.
  - Full suite: `wsl.exe bash -lc "cd /mnt/e/AI_projects/Astra/Foam-Agent && /opt/conda/envs/FoamAgent/bin/python -m pytest tests/ -q"`
- **Baseline to beat:** 21 test files, ~136 test functions, 119 pass, 2 files fail to
  collect. Coverage 46% overall; `domain_packs/bwb` mostly 89–99%.
- **Coverage is not the target.** The high-coverage modules are *executed but
  unasserted*: lines run on every request while almost nothing asserts the values or
  invariants. Assertion density on values and invariants is the metric, not line
  coverage. Do not chase the 0% modules (`vsp_builder`, `vsp_aero`,
  `gmsh_farfield`) — they need OpenVSP and are out of Stage-1 scope.

## Three kinds of work — never blur them

Every item is tagged with exactly one kind. The tag drives cost, ordering, and the
shape of its test.

| Kind | Meaning | Test shape |
| --- | --- | --- |
| **defect** | Behaviour is wrong. Output changes after the fix. | assert the corrected value/invariant; `xfail(strict=True)` until fixed |
| **honesty** | Behaviour is arguably fine, but the UI text, provenance string, field description, or docstring says something false about it. | assert on the *string/flag*, plus a consistency test that the claim matches the computation |
| **capability** | The behaviour a user reasonably expects does not exist yet. | a skipped/backlogged test plus an explicit "not modelled" disclosure test in the meantime |

**Honesty fixes come before most defect fixes on purpose.** They are one-line
conditionals and string edits, they need no physics decision, they cannot regress a
number, and they buy back most of the user's trust. A concept card that admits it
breached the stall cap is far better than one that silently breaches it, and it ships
in an hour instead of a week.

## Dependency graph

```
Stage 0  make the suite honest
   │            (everything downstream is measured against this)
   ▼
Stage 1  store integrity + request boundary  ── atomic writes MUST precede
   │                                            any item that raises write volume
   ▼
Stage 2  kill the session-bricking 500s + honest input parsing
   │
   ├────────────► Stage 3  honesty pass (text/flags only, no math)
   │
   ▼
Stage 4  constraint adherence  ── the stall-floor fix MUST land before any
   │                              cap-adherence test can go green
   ▼
Stage 5  candidate lifecycle state machine
   │
   ▼
Stage 6  physics core + lens math + solver robustness
   │
   ▼
Stage 7  parity, deduplication, capability backlog
```

Hard edges:

- **Stage 1 before Stage 2/3/4.** Stage 2 adds validation responses, Stage 3 adds
  `warnings` payloads, Stage 4 adds violation flags — all of them increase what each
  request writes. `_save_intake` (`src/cockpit/server.py:1027-1036`) is an unlocked
  read-modify-write today; raising write volume on top of it multiplies B2.
- **Stage 4 before the cap tests can pass.** `tests/test_bwb_candidate_caps.py` gets
  its strengthened matrix in Stage 0 as `xfail(strict=True)`; those markers only flip
  when S4-C1 lands.
- **Stage 0 golden panel before Stage 6.** Any physics edit without the pinned
  22-field panel is an invisible change to every L/D the user has ever seen.
- **Stage 3 does not depend on Stage 4.** Disclosing a cap breach and fixing it are
  independent; ship the disclosure first.

---

## Stage 0 — Make the suite honest

**Nothing else can be measured until this lands.** Two files fail to collect and
silently hide ~14 flow-contract tests; two passing tests certify features that do
nothing.

**Files:**
- Create: `tests/conftest.py`, `tests/test_suite_integrity.py`, `tests/test_js_parity.py`
- Create: `tests/data/aero_golden.json`, `tests/test_bwb_aero_golden.py`,
  `tests/test_bwb_aero_invariants.py`, `tests/strategies.py`
- Create: `verification/verify_aero_goldens.py`
- Modify: `pyproject.toml`, `tests/test_flow_conditions.py`,
  `tests/test_lid_driven_cavity_services.py`, `tests/test_bwb_intake.py`,
  `tests/test_bwb_candidate_caps.py`

- [x] **S0-1 — Pytest has no configuration at all** · infrastructure · med · cost S
  - Anchors: no `pytest.ini`, no `setup.cfg`, no `[tool.pytest.ini_options]` in
    `pyproject.toml`, no `conftest.py` anywhere. Every test file re-does its own
    `sys.path.insert` (e.g. `tests/test_requirements_api.py:12-13`).
  - Fix: add `[tool.pytest.ini_options]` with `testpaths = ["tests"]`,
    `xfail_strict = true`, `markers = ["wsl", "openvsp", "credentials", "slow", "js"]`,
    and `filterwarnings`. Add `tests/conftest.py` that does the two `sys.path`
    inserts once and exports the shared fixtures (see S0-11).
  - Locks: `tests/test_suite_integrity.py::test_pytest_config_declares_xfail_strict`.
  - Done when: `pytest tests/ -q` collects with zero `sys.path` lines left in
    individual test files, and an `xfail` that unexpectedly passes fails the run.

- [x] **S0-2 — Two files fail to collect without Codex/ChatGPT OAuth credentials** · defect · HIGH · cost S
  - Anchors: `tests/test_flow_conditions.py:12-25` and
    `tests/test_lid_driven_cavity_services.py:17-23` import `services.*` at module
    scope, which pulls `src/config.py` and its credential requirement. ~14 flow-contract
    tests vanish from every credential-less run with no signal.
  - **Decision: skipif-guard them (see Open questions, Q3).** Wrap the credential-bound
    imports in a module-level guard:
    `pytest.importorskip("services.flow_conditions", reason=...)` for the import failure
    plus `pytestmark = pytest.mark.credentials` and a
    `skipif(not _have_credentials(), reason="needs Codex/ChatGPT OAuth")`.
  - Locks: `tests/test_suite_integrity.py::test_no_collection_errors` (below).
  - Done when: a credential-less `pytest tests/ -q` reports **0 errors** and a
    non-zero, named skip count instead of two silent collection failures.

- [x] **S0-3 — A silently shrinking suite is indistinguishable from a passing one** · infrastructure · HIGH · cost S
  - Fix: `tests/test_suite_integrity.py` asserts (a) collection produces zero errors
    (`pytest.main(["--collect-only", "-q"])` in a subprocess, assert rc==0), (b) the
    collected test count is `>=` a committed floor constant, (c) every skipped module
    carries one of the declared markers so no skip is anonymous.
  - Done when: deleting a test file or breaking an import fails the suite loudly.

- [x] **S0-4 — `tests/js/atmosphere_parity.mjs` is orphaned; nothing runs it** · infrastructure · HIGH · cost S
  - Anchors: `tests/js/atmosphere_parity.mjs` imports
    `src/cockpit/static/atmosphere.js` and `aero_mirror.js` and checks them against
    `domain_packs/bwb/data/us_standard_atmosphere_1976_0_4000m.csv`; it is referenced
    by nothing in `pyproject.toml`, pytest, `verification/`, or any doc.
  - **Decision: pytest shells out to node (see Open questions, Q1).** Add
    `tests/test_js_parity.py` that globs `tests/js/*.mjs`, parametrizes over the glob,
    runs `subprocess.run([node, path], cwd=repo_root)`, asserts rc==0, and surfaces
    stdout/stderr on failure. Guard with
    `@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")`
    and `@pytest.mark.js`.
  - Locks: itself, plus `test_suite_integrity.py::test_every_js_file_is_collected`
    which asserts the parametrized id set equals the `tests/js/*.mjs` file set — this
    is what prevents the *next* orphan.
  - Done when: `pytest tests/ -q` runs the atmosphere parity file, and adding a new
    `.mjs` file requires no wiring.

- [x] **S0-5 — FALSE-CONFIDENCE TEST #1: `tests/test_bwb_intake.py::test_lock_toggle_roundtrip`** · honesty · HIGH · cost S
  - Anchors: `tests/test_bwb_intake.py:108-113` asserts only that the `status` flag
    round-trips. The feature it appears to certify is inert: setting every row to
    `fixed` vs every row to `variable` produces byte-identical derived values;
    `domain_packs/bwb/intake.py:238` is the only write of `status` and nothing outside
    display code reads it.
  - Fix: **rename to `test_lock_toggle_flag_persists_only`** with a docstring stating
    that the flag is persisted display state and does not constrain the solver. Add
    `test_lock_status_does_not_change_derived_values` which asserts the *current*
    inert behaviour explicitly (all-fixed derived == all-variable derived), so the day
    locks become load-bearing this test fails and forces a decision. Add
    `test_lock_toggle_constrains_the_solver` marked `xfail(strict=True,
    reason="S7-CAP-3: locks are display-only")` as the capability placeholder.
  - Done when: no test name in the suite implies locks work; the inert behaviour is
    asserted, not assumed.

- [x] **S0-6 — FALSE-CONFIDENCE TEST #2: `tests/test_bwb_candidate_caps.py::test_tighter_stall_cap_grows_candidate_wings`** · defect exposure · CRITICAL · cost M
  - Anchors: `tests/test_bwb_candidate_caps.py:50-67`. It passes only because its
    fixture (`mtow 2.0 kg, v 18 m/s, alt 100 m, hand, stall 8.0`) is a benign
    combination. A 13-scenario probe found **11 violated**: (mtow 20 kg, span cap
    0.9 m, hand) → stall 29.3–30.2 vs cap 10.5 in 5/5 candidates; (mtow 25 kg, stall
    cap 5.0, hand) → 13.4–14.4 vs 5.0 in 5/5; (mtow 2 kg, span cap 0.8) → 1/5.
  - Fix: **replace** the single fixture with a `@pytest.mark.parametrize` matrix over
    the 13 probed scenarios asserting
    `c.panel.v_stall_ms <= min(user_cap, LAUNCH_VSTALL_MAX[launch]) + tol` for **every**
    candidate including `baseline`. Mark the 11 known-violating scenarios
    `xfail(strict=True, reason="S4-C1 stall floor undone by AR/root-chord repairs")`.
    Keep the benign case as a non-xfail row so a regression there is still caught.
  - Done when: the matrix exists, `xfail_strict` is on, and Stage 4 flips 11 markers
    to pass with zero code changes to the test.

- [x] **S0-7 — Pin the measured 22-field golden panel** · infrastructure · CRITICAL · cost M
  - Anchors: `domain_packs/bwb/aero_estimates.py:184-243` (`estimate`).
  - Reference case (probe-measured, exact): `BWBParams(span_m=1.6, root_chord_m=0.55,
    centre_body_fraction=0.28, mid_chord_frac=0.6, taper_outer=0.4,
    sweep_inner_deg=35, sweep_outer_deg=18, root_thickness_pct=14,
    tip_thickness_pct=10, root_twist_deg=1.0, tip_twist_deg=-3.0, dihedral_deg=3.0)`
    at `atmosphere_at(0.0)`, `v_ms=18.0`, `weight_n=34.335`, `launch="hand"` →
    `ld_cruise 17.003749573839468`, `cd0 0.01215973103075618`,
    `oswald_e 0.8194042063729294`, `cl_max_3d 1.1208492861603039`,
    `v_stall_ms 10.390650635946146`, `x_np_m 0.24976166981040424`,
    `s_ref_m2 0.4632320000000001`, `aspect_ratio 5.5263885051119095`,
    `mac_m 0.33196149949341447`, `reynolds_mac 409065.98276939226`.
  - Fix: commit `tests/data/aero_golden.json` holding all 22 fields (not just the ten
    above) at full repr precision. `tests/test_bwb_aero_golden.py` compares field by
    field at `rel=1e-12`. Add `verification/verify_aero_goldens.py` in the house style
    (`from _common import Table`, `Table("GOLDEN AERO PANEL")`, one row per field,
    `t.finish()` exit code) with a `--update` flag that rewrites the JSON.
  - Golden-update policy (spec §Golden policy): `--update` may only be run together
    with a ledger entry naming the physical reason and the source. A golden diff in a
    PR without that entry is a rejected PR.
  - Done when: `pytest tests/test_bwb_aero_golden.py -q` passes on Windows python and
    in WSL, and `python verification/verify_aero_goldens.py` prints a 22-row PASS table.

- [x] **S0-8 — Component goldens so a physics edit localises itself** · infrastructure · HIGH · cost M
  - Anchors: `aero_estimates.py:85-94` (`mean_tc`), `:97-100`
    (`effective_le_sweep_deg`), `:103-110` (`effective_c4_sweep_deg`), `:113-118`
    (`skin_friction_coeff`), `:121-123` (`form_factor`), `:126-138`
    (`oswald_efficiency`), `:151-155` (`cl_max_3d`), `:158-177` (`neutral_point_m`).
  - Fix: golden tables per component, in the same JSON: `skin_friction_coeff` over a
    Re ladder (1e3, 9999, 1e4, 2e5, 5e5, 1e6, 2e6 — the ladder that exposes the
    `max(re, 1e4)` floor at `:115`), `oswald_efficiency` over an (AR × LE-sweep) grid
    including the clamp plateaus and the AR=4.800 crossing, both effective sweeps on
    the F4 worked case (`span 1.8, cr 0.25, cbf 0.45, mcf 0.85, taper 0.7, sweeps
    60/40` → LE 49.000, c/4 50.493), and `neutral_point_m` on the reference geometry.
  - Done when: changing `LAMINAR_FRACTION` fails exactly the Cf/CD0/L-D goldens, and
    changing an Oswald coefficient fails exactly the Oswald grid.

- [x] **S0-9 — Lock the five invariants that currently hold with zero violations** · infrastructure · CRITICAL · cost M
  - Anchors: measured over 117,600 flight cases with **zero** violations and **zero**
    NaN: `ld_cruise <= ld_max`; `cl_star <= cl_max_3d`;
    `endurance_fom == cl_cruise**1.5 / cd_cruise`;
    `x_cg_for_target_sm_m == x_np_m - 0.08 * mac_m`; `x_np_m > 0`.
  - Fix: `tests/test_bwb_aero_invariants.py` asserts all five over a parametrized
    envelope sweep (and, from S0-10, under hypothesis). These are **not** bugs to fix
    — they are the contract that every Stage 6 physics edit must preserve.
  - Done when: the five invariants are asserted in one named test each, and each test
    names the invariant in its docstring so a Stage-6 failure is self-explaining.

- [x] **S0-10 — Hypothesis strategies for the three schemas** · infrastructure · HIGH · cost M
  - Fix: `tests/strategies.py` exports `bwb_params()` (a `@st.composite` that samples
    the 12 fields inside their `Field` ranges and *filters on the model validator* so
    only legal geometries are produced — AR in [2.5, 10], tip chord ≥ 42 mm, no
    wash-in), `design_requirements()` (mtow 0–25 kg, cruise 8–60 m/s, altitude
    0–4000 m, stall 5–20 m/s, span 0.8–3.0 m), `flight_conditions()`, and
    `intake_edits()` (including the hostile values from D1/D3).
  - Locks: a meta-test that draws 200 examples from `bwb_params()` and asserts every
    one constructs without `ValidationError`.
  - Done when: `hypothesis` is importable in the WSL env (already pinned,
    `environment.yml:372`) and the strategies are reused by Stage 4/6 property tests.

- [x] **S0-11 — One shared TestClient fixture for the API layer** · infrastructure · HIGH · cost S
  - Anchors: the proven pattern is `tests/test_requirements_api.py:17-27` —
    `monkeypatch.setenv("ASTRA_SESSIONS_DIR", tmp_path/"sessions")` then
    `TestClient(cockpit.server.app)`. No WSL-external services, no OpenVSP.
  - Fix: move that fixture into `tests/conftest.py` as `client` plus a
    `stage1_session` fixture that parses a brief and returns `(client, sid)`. Mark the
    module `@pytest.mark.wsl` (FAISS import cost ~30 s — one import per session, so
    keep all API tests in as few modules as practical).
  - Done when: Stage 1/2/4/5 API tests need no boilerplate and the whole API layer
    pays the FAISS import once.

- [x] **S0-12 — Seed the xfail backlog** · infrastructure · HIGH · cost M
  - Fix: write the *desired-behaviour* tests for every item marked `xfail-encode` in
    the test-strategy section below, each with
    `@pytest.mark.xfail(strict=True, reason="<item ID>: <one line>")`. The reason
    string **must** start with the work-item ID.
  - Locks: `test_suite_integrity.py::test_every_xfail_names_a_plan_item` — parse the
    reason strings, assert each ID matches `S\d-[A-Z]+\d+` and appears in this
    document. The suite is then the live backlog and it cannot drift from the plan.
  - Done when: `pytest tests/ -q -rx` prints a backlog listing whose IDs map 1:1 onto
    the items below.

**Stage 0 exit:** zero collection errors; a named, non-zero skip count; the golden
panel, component goldens, and five invariants pinned; the JS parity file executed by
`pytest`; both false-confidence tests renamed/strengthened; an xfail backlog whose
reasons resolve to plan IDs.

---

## Stage 1 — Store integrity and the request boundary

**Depends on:** Stage 0. **Blocks:** everything that increases per-request write
volume (Stages 2, 3, 4).

The cockpit is an unauthenticated localhost FastAPI app with no CSRF protection: any
web page the user visits can POST to it. Combined with an unlocked
read-modify-write store, that is data loss plus filesystem writes outside the
session root.

**Files:**
- Modify: `src/cockpit/server.py`, `src/services/design_session.py`
- Create: `tests/test_stage1_api_security.py`, `tests/test_stage1_store_integrity.py`

- [x] **S1-A1 — `session_id` is an unvalidated filesystem path segment** · defect · CRITICAL · cost S
  - Anchors: `src/cockpit/server.py:74` (`_session_path` joins the client string
    directly), `src/services/design_session.py:30-37` (`sessions_root` /
    `_session_file` likewise).
  - Now (probe-confirmed): `session_id="/tmp/astra_probe_outside"`,
    `session_id="../escaped"`, and `session_id="aa/bb"` all return **200** and the
    server **creates** `intake/ plan/ candidates/ requirements/ cockpit/`
    directories outside the sessions root. Exploitation currently needs a
    `session.json` at the target, which bounds but does not remove it.
  - Fix: validate in `_ensure_session` (`server.py:109-124`) with
    `re.fullmatch(r"[0-9a-f]{8}", sid)` — that is exactly what `create_session`
    produces (`design_session.py:56`, `uuid4().hex[:8]`) — returning `HTTPException(400)`
    on mismatch. Add a defence-in-depth containment assertion in
    `design_session._session_file`: `os.path.realpath(path)` must start with
    `os.path.realpath(sessions_root()) + os.sep`, else `ValueError`.
  - Locks: `test_stage1_api_security.py::test_traversal_session_ids_are_rejected`,
    parametrized over `"../escaped"`, `"/tmp/x"`, `"aa/bb"`, `"..\\win"`, a 300-char
    id, a unicode id, and `"AABBCCDD"` (uppercase → reject; ids are lowercase hex);
    plus `::test_no_directory_is_created_outside_sessions_root` which snapshots
    `tmp_path.parent` before/after.
  - Done when: all four Stage-1 routes return 400 for every hostile id and the
    filesystem snapshot is unchanged.

- [x] **S1-A2 — Omitted/empty `session_id` on a MUTATING POST hijacks the live session** · defect · CRITICAL · cost S
  - Anchors: `server.py:109-124` — `_ensure_session(None)` falls back to
    `_list_sessions()[0]`, i.e. the most recently modified session in the whole store
    (`server.py:78-91` sorts by mtime descending).
  - Now: `POST /api/stage1/intake` with no `session_id`, and with `""`, silently
    mutated the newest session and returned 200.
  - Fix: split the resolver. `_ensure_session(sid)` keeps the convenience fallback for
    **GET** only; add `_require_session(sid)` used by `POST /api/stage1/parse`,
    `POST /api/stage1/intake`, `POST /api/stage1/generate` (and the candidate
    mutators at `server.py:749-804`) which raises `HTTPException(400, "session_id is
    required")` when falsy. A GET default is defensible; a mutating-POST default is not.
  - Locks: `::test_mutating_posts_require_an_explicit_session` parametrized over the
    three Stage-1 POSTs × `{omitted, "", null}`, asserting 400 **and** that a
    pre-existing session's intake version is unchanged.
  - Done when: no POST can write to a session the caller did not name.

- [x] **S1-A3 — No input size limits; every call writes a new immutable version** · defect · HIGH · cost S
  - Anchors: `server.py:1075-1086` (`IntakeEditReq.edits` unbounded),
    `server.py:1054-1057` (`brief` unbounded), `server.py:1027-1036` (every save is a
    new version directory).
  - Now: a single 2 MB edit value is persisted verbatim; 500 edits in one call yields
    a 519-row intake; a 50,000-char brief is stored. Unbounded disk growth per request.
  - Fix: pydantic constraints on the request models —
    `brief: str = Field(max_length=4000)`,
    `edits: List[Dict[str, Any]] = Field(max_length=64)`, and a per-edit value cap
    (`max_length=200` after `str()`), returning 422 with the offending field. Cap the
    row count in `intake.apply_edits` (`domain_packs/bwb/intake.py:307-334`) at, say,
    64 rows total and reject further custom rows with a named error.
  - Locks: `::test_oversize_brief_rejected`, `::test_too_many_edits_rejected`,
    `::test_oversize_edit_value_rejected`, each asserting 422 and that **no** new
    version directory was created.
  - Done when: the store cannot be grown without bound by a single request.

- [x] **S1-B2 — Concurrency corrupts the store (unlocked read-modify-write, in-place writes)** · defect · CRITICAL · cost M
  - Anchors: `server.py:1027-1036` (`_save_intake` writes `intake.json` then
    `index.json` in place, no lock, no tmp+rename), `server.py:1008-1013`
    (`_intake_latest_version` reads `index.json`), `server.py:1016-1024`
    (`_load_intake` reads `intake.json`), `src/services/design_session.py:48-52`
    (`_save` writes `session.json` in place), `:150` (`record_version` reads it).
  - Now: 12 parallel `POST /api/stage1/intake` → **5× HTTP 500**
    (`json.JSONDecodeError` from half-written files at `server.py:1024` and `:1013`)
    and only v1+v2 on disk with `index.latest=2` — **10 versions silently lost**.
    Parallel generates fail the same way: `server.py:1138` →
    `design_session.py:150` `JSONDecodeError` on a truncated `session.json`, leaving
    plan v1–v3 orphaned with no candidate batch and returned `plan_version` values
    that match no listable state.
  - Fix: (a) an atomic-write helper — write to `<path>.tmp.<pid>` then
    `os.replace(tmp, path)` — used by `_save_intake`, `design_session._save`,
    `design_session.save_requirements` (`:202-215`), `candidates._save_candidate`
    (`src/services/candidates.py:77-81`) and the index writes at
    `candidates.py:129-133` and `:353-357`. (b) a per-session advisory lock around the
    whole read-modify-write: a `filelock`-style `<session>/.lock` acquired by a
    context manager in `design_session`, used by every mutating path. (c) allocate the
    version number *inside* the lock, not before it.
  - Locks: `tests/test_stage1_store_integrity.py::test_parallel_intake_saves_lose_nothing`
    — `ThreadPoolExecutor(12)` against `TestClient`, assert 12× 200, assert
    `index.latest == 12`, assert `v1..v12` all exist and all parse as `Intake`;
    `::test_parallel_generates_leave_a_consistent_store` — every returned
    `plan_version` is listable and has a matching candidate batch.
  - Done when: no interleaving produces a 500, a lost version, or a partially written
    JSON file. **This item gates Stages 2–4.**

- [x] **S1-B3 — A corrupt `intake.json` → 500 on all three intake endpoints** · defect · HIGH · cost S
  - Anchors: `server.py:1024` (`intake_pack.Intake.model_validate(json.load(f))`,
    unguarded). Note S1-B2 is exactly how such a file is created in normal use.
  - Fix: catch `(json.JSONDecodeError, ValidationError, OSError)` in `_load_intake` and
    degrade deterministically: fall back to the most recent version that *does* parse,
    and if none does, return the derived default intake with an explicit
    `warnings: ["intake vN unreadable — recovered from vM"]` entry (the same
    `warnings` channel S3-D2 introduces). Never repair the corrupt file in place;
    never silently discard the user's data.
  - Locks: `::test_corrupt_intake_recovers_to_previous_version` and
    `::test_all_versions_corrupt_returns_default_with_warning` — write garbage bytes
    into `intake/vN/intake.json`, assert 200 + a warning naming the version, on all
    three intake endpoints.
  - Done when: a corrupt store degrades with a named warning instead of 500-ing.

- [x] **S1-B4 — `index.json` pointing at a missing version → `FileNotFoundError` → 500** · defect · MED · cost S
  - Anchors: `server.py:1022-1024`.
  - Fix: same recovery path as S1-B3 — treat "declared latest is absent" as
    "corrupt", scan the directory for the highest existing parseable version, warn.
  - Locks: `::test_index_pointing_at_missing_version_recovers`.
  - Done when: deleting `intake/v3/` while `index.latest==3` yields 200 + warning.

- [x] **S1-D8 — Version inflation: identical writes each create a new version** · defect · MED · cost S
  - Anchors: `server.py:1080-1086` (`edits: []` still saves), `:1059-1072` (repeated
    identical parses save), `:1093-1163` (repeated generates write a plan version, a
    requirements version, and a candidate batch). Confirmed: `edits: []` × 3 → versions
    1, 2, 3.
  - Fix: content-addressed dedupe. Before writing, compare the canonical
    `model_dump_json(indent=2)` of the new artifact against the current latest; if
    byte-identical, return the existing version number unchanged. Apply to intake,
    requirements (`design_session.save_requirements`), and the plan write
    (`server.py:94-106`). Do **not** dedupe candidate batches — they legitimately
    depend on requirements that live outside the plan (see S4-C3).
  - Locks: `::test_noop_edit_does_not_create_a_version`,
    `::test_identical_parse_is_idempotent`,
    `::test_identical_generate_reuses_the_plan_version`.
  - Done when: three identical requests leave exactly one version on disk and all
    three return the same version number.

**Stage 1 exit:** no request can write outside the sessions root or into a session it
did not name; 12-way parallel writes lose nothing; corrupt/missing store files degrade
with a named warning; identical requests are idempotent.

---

## Stage 2 — Kill the session-bricking 500s and the silent-wrongness parser

**Depends on:** Stage 1 (atomic writes; these fixes add validation responses and
`warnings` payloads).

This stage covers the two worst user-visible classes: a plain-English brief that
permanently disables the Generate button, and a parser that returns the wrong number
with `origin: "you_said"`.

**Files:**
- Modify: `src/cockpit/server.py`, `domain_packs/bwb/intake.py`
- Create: `tests/test_stage1_api_robustness.py`, `tests/test_bwb_intake_parsing.py`

- [x] **S2-B1 — `endurance_min > 600` bricks Generate forever** · defect · CRITICAL · cost S
  - Anchors: `server.py:1111-1118` clamps payload/speed/altitude/MTOW but passes
    `endurance` through raw into `MissionSpec(...)` at `server.py:1113`;
    `domain_packs/bwb/schema.py:21` caps `endurance_min` at 600.
  - Now: "loiter for 11 hours" → 660 and "12 hour endurance" → 720 both parse **200**,
    then every subsequent `POST /api/stage1/generate` returns **500**, forever. `GET`
    still returns 200, so the UI shows a healthy list with a dead button and no
    indication which row is at fault.
  - Fix: two-part. (a) At the boundary, clamp endurance the same way its neighbours are
    clamped — `min(max(endurance, 1.0), 600.0)` — and record the clamp in the
    `warnings` channel (S3-D2), so the request succeeds and the user is told. (b) At
    ingest, `intake.parse_brief` (`intake.py:272-276`) should clamp to the schema
    range at parse time and set the row's `source` to
    `"your brief (capped at 600 min — endurance is recorded only)"`. Do **not** raise:
    endurance drives no computation (see S3-E2), so a 500 for it is indefensible.
  - Locks: `test_stage1_api_robustness.py::test_long_endurance_brief_still_generates`
    parametrized over `"loiter for 11 hours"`, `"12 hour endurance"`, `"endurance
    900 min"`; assert parse 200 **and** generate 200 **and** a warning naming the
    endurance row.
  - Done when: no brief expressible in English can make Generate return 500.

- [x] **S2-B5 — A negative `number` in a stored intake → 500 at generate** · defect · HIGH · cost S
  - Anchors: `server.py:1103-1118` — `row_num` returns the stored number verbatim and
    generate trusts the file with zero validation.
  - Fix: validate every row number at the generate boundary before constructing
    `MissionSpec`: finite, non-negative, inside the schema range for that row's target
    field; otherwise fall back to the documented default **and** emit a warning naming
    the row. Same treatment in `intake.to_requirements` (`intake.py:345-371`).
  - Locks: `::test_negative_row_number_does_not_500` (write a negative `number`
    directly into `intake/vN/intake.json`, then generate), parametrized over
    `payload`, `altitude`, `mtow_cap`, `span_max`, `v_stall_max`.
  - Done when: no stored intake, however malformed, can 500 the generate route.

- [x] **S2-D1 — The number parser drops signs, ignores units, and is unanchored** · defect · CRITICAL · cost M
  - Anchors: `domain_packs/bwb/intake.py:229-231` —
    `_NUM = r"(\d+(?:\.\d+)?)"` with `text.replace(",", ".")`, no sign, no units, no
    anchoring; used by `apply_edits` (`:320`, `:329`).
  - Now, **all HTTP 200 and invisible**: `-3 kg` → `+3.0` (sign dropped);
    `-500 m` → `+500`; `1,500` → `1.5` (**1000× error**); `6 ft` → 6 m;
    `1800 mm` → 1800 m; `20in` → 20 m; `1e400` → 1.0; a 400-digit number → `inf`,
    serialized as `null`, so the row **silently loses its value**;
    `abc`/`""`/2 MB junk → `None` and the row *displays the junk as the requirement*
    while the solver uses the schema default. Note `cm`/`mm` **are** converted in the
    brief path (`intake.py:254-257`) but **not** in the edit path — the same string
    means different things depending on how it arrives (this is also **E5**).
  - Fix: replace `_num` with one shared, unit-aware, sign-aware parser used by *both*
    paths. Requirements: (a) optional leading sign; (b) explicit unit table per row id
    (`m|cm|mm|in|ft` for lengths, `kg|g|lb` for masses, `m/s|kt|km/h|mph` for speeds,
    `min|h` for time) with conversion to SI; (c) thousands separators handled by
    locale-free rules — a comma followed by exactly three digits is a separator, a
    comma followed by 1–2 digits is a decimal point; (d) reject non-finite and
    out-of-`float64` magnitudes with a named error rather than producing `inf`;
    (e) return a discriminated result `(value, unit_seen, ok, reason)` so the caller
    can warn; (f) when parsing fails, the row keeps the user's text **and** gains an
    explicit `unparsed: true` flag so the UI can mark it rather than presenting junk
    as a requirement.
  - Locks: `tests/test_bwb_intake_parsing.py` — a parametrized table of the 12 probed
    cases above with their **desired** outputs, all `xfail(strict=True,
    reason="S2-D1: ...")` at Stage 0 and flipping here. Plus a hypothesis property:
    for any string drawn from `intake_edits()`, `_num` either returns a finite float
    or `ok=False`, never `inf`/`NaN`, never raises.
  - Done when: the 12 cases pass, the brief and edit paths agree on every unit string
    (a dedicated `test_brief_and_edit_paths_agree` parametrized over the unit table),
    and no row can display a value the solver does not use without an `unparsed` flag.

- [x] **S2-D3 — `apply_edits` accepts any JSON type and `str()`s it** · defect · MED · cost S
  - Anchors: `intake.py:318-323`. Confirmed: `value: null` stores the literal string
    `"None"`; `value: [1,2]` stores `"[1, 2]"` with `number = 1.0`.
  - Fix: type-gate the payload. `value` must be `str | int | float`; anything else is
    422 with the row id. `null` means "clear this row" and must be handled explicitly
    (restore the assumed default, set `origin` back to `"assumed"`), not stringified.
  - Locks: `::test_non_scalar_edit_value_rejected` parametrized over `None`, `[1,2]`,
    `{"a":1}`, `True`; `::test_null_value_restores_the_assumed_default`.
  - Done when: no Python `repr` can reach a user-visible requirement value.

- [x] **S2-D4 — Brief misparse: "span" reaches past itself and steals the altitude** · defect · CRITICAL · cost M
  - Anchors: `intake.py:254` —
    `span[^.\d]{0,20}(\d+(?:\.\d+)?)\s*(m|cm|mm)\b` — the `{0,20}` gap skips over the
    span's own number when it is followed by other text, reaching the *next*
    number+unit; then the altitude branch at `intake.py:259-264` discards its own
    correct match because `abs(v - rows["span_max"].number) <= 1e-9`.
  - Now: `"Surveillance BWB, 250 g camera, 2.2 m span, fly at 300 m, 45 min
    endurance, hand launch"` → `span_max = 300 m` **and**
    `altitude = 100 m AMSL (assumed)`. Two requirements wrong, **both** stamped
    `origin: "you_said"`, `status: "fixed"` — presented back to the user as their own
    words. Also `"span 2 m but also span 5 m"` → 2 with no ambiguity reported, and a
    brief containing every launch keyword resolves by dict order
    (`intake.py:283-293`).
  - Fix: (a) anchor each pattern to its own number: allow the value **before** the
    keyword (`2.2 m span`) or immediately after it (`span 2.2 m`), with a gap that
    cannot contain a digit — replace `[^.\d]{0,20}` with `[^\d]{0,12}` and add the
    pre-keyword alternative. (b) Remove the "equals the span number" suppression at
    `:263` — it is a symptom-patch for (a) and it *deletes a correct altitude match*.
    (c) Detect multiple distinct matches for one row and record an ambiguity: keep the
    first, set `origin: "you_said"` but add
    `warnings: ["brief mentions span twice (2 m, 5 m) — used 2 m"]`. (d) Make the
    launch resolution order explicit and documented (most specific keyword wins:
    `runway`/`wheel` > `catapult`/`bungee` > `hand`/`throw`) instead of dict order,
    and report an ambiguity when more than one family matches.
  - Locks: `tests/test_bwb_intake_parsing.py::test_probe_brief_parses_span_and_altitude_correctly`
    (the exact probed brief → `span_max = 2.2`, `altitude = 300`, both `you_said`);
    `::test_span_before_and_after_keyword` parametrized over 10 phrasings;
    `::test_ambiguous_span_is_reported`;
    `::test_all_launch_keywords_resolve_deterministically`. All `xfail(strict=True)`
    at Stage 0.
  - Done when: no field stamped `you_said` holds a number the user did not write for
    that field, and the probed brief round-trips.

- [x] **S2-D5 — `POST /api/stage1/parse` destroys user state** · defect · HIGH · cost S
  - Anchors: `server.py:1059-1072` calls `intake_pack.parse_brief`, which starts from
    `default_intake()` (`intake.py:245`) and returns a fresh list.
  - Now: parsing after edits reverted an edited `mtow_cap` to the assumed default and
    **deleted a user-added custom row**. 200, no warning.
  - Fix: parse into a *candidate* intake, then merge onto the stored one with an
    explicit precedence rule: `you_said` (from this brief) > `edited` (user's prior
    edit) > `assumed`; custom rows are always preserved. Where the brief contradicts a
    prior edit, keep the brief's value **and** emit a warning naming both values so the
    override is visible. Add `?replace=true` for the deliberate start-over case.
  - Locks: `::test_parse_preserves_user_edits_and_custom_rows`,
    `::test_brief_overriding_an_edit_is_reported`,
    `::test_replace_true_starts_from_defaults`.
  - Done when: no parse silently loses a value the user typed.

- [x] **S2-D6 — Cross-endpoint data loss: generate overwrites the requirements screen** · defect · HIGH · cost S
  - Anchors: `server.py:1143-1152` — generate reads `_current_requirements(sid)`, then
    overwrites `span_max_m` / `v_stall_max_ms` / mtow / speed / altitude / launch from
    the intake and calls `ds.save_requirements`.
  - Now: after `POST /api/requirements {ld_min: 14.0, span_max_m: 2.4, persist: true}`,
    one Stage-1 generate silently reset `span_max_m` 2.4 → 3.0 and
    `v_stall_max_ms` → 10.5. Stage 1 destroys requirements set on the older M3 screen.
  - Fix: make the sync **non-destructive**. Only write a field from the intake when the
    corresponding intake row's `origin` is `you_said` or `edited`; a row still at
    `assumed` must not overwrite an explicitly persisted requirement. Where the intake
    genuinely does override, record the previous value in the warning channel and in
    the requirements version payload (`design_session.save_requirements`) so the
    overwrite is auditable.
  - Locks: `::test_generate_does_not_clobber_explicit_requirements`,
    `::test_generate_override_is_recorded_in_the_requirements_version`.
  - Done when: a persisted `span_max_m=2.4` survives a Stage-1 generate whose span row
    is still `assumed`.

- [x] **S2-D7 — Internally inconsistent plan artifacts (a 1 kg aircraft carrying 6 kg)** · defect · HIGH · cost S
  - Anchors: `server.py:1113-1137` builds `MissionSpec` and `MissionDerived` from
    independently clamped values. Confirmed: payload 6 kg + MTOW cap 1 kg produced
    `mission_derived.json` with `mission.mtow_kg = 1.0`, `mission.payload_kg = 6.0`,
    `mtow_kg = 6.0`.
  - Fix: add a cross-check `model_validator` to `MissionSpec`
    (`domain_packs/bwb/schema.py:14-26`): `mtow_kg >= payload_kg` when both are given
    — this is the same class of cross-check `BWBParams` already carries
    (`schema.py:55-58`). At the boundary, resolve the conflict *before* constructing
    the model: raise the MTOW cap to the payload and warn, or reject with 422 naming
    both rows. Also assert `MissionDerived.mtow_kg == mission.mtow_kg` when the latter
    is set.
  - Locks: `::test_payload_above_mtow_cap_is_reconciled_not_persisted`,
    `tests/test_bwb_derivation.py::test_mission_spec_rejects_payload_above_mtow`.
  - Done when: no `mission_derived.json` can be written whose payload exceeds its MTOW.

**Stage 2 exit:** no plain-English brief can 500 any route; the number parser is
sign-, unit-, and magnitude-correct and identical across the brief and edit paths; no
requirement is stamped `you_said` with a value the user did not write; no endpoint
destroys another's state.

---

## Stage 3 — Honesty pass (text, flags, and provenance only)

**Depends on:** Stage 1 (the `warnings` channel is a new payload field). **Deliberately
does NOT depend on Stage 4 or 6** — disclosing a breach and fixing it are independent,
and disclosure ships first.

**No computed number changes in this stage.** Every item is a string, a field
description, a flag, or a conditional. This is the cheapest stage and the one that
recovers the most trust: the app currently tells the user things that are false.

The canonical wording for every disclosure string lives in the companion ledger, so
the strings are reviewed once and reused.

**Files:**
- Modify: `domain_packs/bwb/intake.py`, `domain_packs/bwb/aero_estimates.py`,
  `domain_packs/bwb/variants.py`, `src/cockpit/server.py`,
  `src/cockpit/static/stage1.{html,js}`
- Create: `tests/test_bwb_disclosure.py`, `tests/test_bwb_lens_intent.py`

- [x] **S3-D2 — Silent clamping with no disclosure (the `warnings` channel)** · honesty · CRITICAL · cost M
  - Anchors: `intake.py:353-371` (`to_requirements` clamps mtow, stall, span,
    altitude) and `server.py:1114-1118` (clamps payload, speed, altitude, mtow) — both
    report nothing.
  - Now, display-vs-used divergence, all with verdict still "feasible": span 100 m →
    **3.0** used; span 0.1 → 0.8; MTOW 500 → 25; MTOW 0 → ignored; altitude 99999 →
    4000; altitude −500 → **+500** (sign flip); stall 0.5 → 5.0; stall 99 → 20
    (effective 10.5); payload 0 → 1.0 for sizing but 0.01 in the plan; payload 99 → 10.
    The row keeps the typed value, `origin` becomes `"edited"`, and no `warnings` or
    `clamped` field exists in the response.
  - Fix: (a) add `warnings: List[IntakeWarning]` to the intake response model, where
    `IntakeWarning` carries `row_id`, `kind` (`clamped|unparsed|ambiguous|recovered|
    not_modelled`), `typed`, `used`, `limit`, and a plain-language `message`. (b) Every
    clamp site appends one. (c) Add `used_value` to `IntakeRow` so the row can render
    "you typed 100 m · using 3.0 m (schema maximum)". (d) The feasibility verdict must
    not read "feasible" while a hard cap was silently substituted — introduce a
    `verdict: "feasible_with_substitutions"` state or, at minimum, a
    `substitutions: int` count the UI must surface. (e) `stage1.js` renders the badge.
  - Locks: `tests/test_bwb_disclosure.py::test_every_clamp_emits_a_warning`
    parametrized over the ten probed divergences, asserting `typed`, `used`, and that
    `used_value != value` implies a warning exists;
    `::test_no_silent_substitution_reports_plain_feasible`.
  - Done when: for every one of the ten probed inputs the response states both the
    typed and the used value, and the sign-flip case (−500 → +500) is reported as a
    rejection, not a clamp.

- [x] **S3-E1 — The lock toggles change nothing** · honesty · HIGH · cost S
  - Anchors: `intake.py:238` is the only write of `status`; nothing outside display
    code reads it. All-fixed and all-variable derive byte-identical values.
  - Fix: relabel in the UI. The column becomes "Lock (display only)" with an ⓘ that
    says the flag records intent for Stage 2 and does not currently constrain the
    solver. Add the same sentence to `IntakeRow.status`'s field description so the API
    is honest too. The capability itself is **S7-CAP-3**.
  - Locks: `test_bwb_disclosure.py::test_lock_status_is_documented_as_display_only`
    (asserts the field description contains the disclaimer) plus the S0-5 inertness
    test.
  - Done when: nothing in the UI or schema implies locks constrain anything.

- [x] **S3-E2 / E3 — Six of twelve editable rows and all custom rows drive zero computation** · honesty · HIGH · cost S
  - Anchors: verified inert — `mission_type`, `endurance`, `regulatory`, `structure`,
    `airfoil` (`intake.py:203-205`), `wind` (`intake.py:206-208`): editing each leaves
    all seven derived values bit-identical. Rows that **do** drive math: `payload`,
    `altitude`, `launch`, `span_max`, `v_stall_max`, `mtow_cap`. Custom rows
    (`intake.py:326-333`) are recorded only. The `endurance` row already half-admits it
    (`intake.py:276`, `"recorded only (aero scope)"`).
  - Fix: add an explicit `drives_computation: bool` to `IntakeRow` (default `False`,
    `True` only for the six that do), and render "recorded only — not evaluated at
    this tier" on the rest, matching the existing wording style at `intake.py:276` and
    `schema.py:22-25`. Custom rows get the same treatment; their current info string
    (`intake.py:332-333`) claims they are "passed to Stage 2 as a constraint when
    recognised" — either narrow that to the recognised set or drop the promise.
  - Locks: `::test_only_the_six_computing_rows_are_flagged` (asserts the flag set
    equals the measured set) and `::test_inert_rows_do_not_change_derived_values`
    (the executable proof behind the label — if a row ever *starts* mattering, this
    fails and forces the flag to be updated).
  - Done when: every row states whether it changes a number, and the claim is tested.

- [x] **S3-E4 — Some ⓘ "driver chips" are fiction** · honesty · MED · cost S
  - Anchors: `static_margin`'s listed drivers are `wind` and `mission_type`, but static
    margin is the hardcoded constant `aero_estimates.py:32`
    (`STATIC_MARGIN_TARGET = 0.08`). `clmax`'s listed driver is `airfoil`, but
    `cl_max_3d` = `1.30 * 0.90 * cos(sweep)` (`aero_estimates.py:151-155`) and responds
    **only** to sweep.
  - Fix: derive the driver chips from the actual dependency set rather than a hand-kept
    list. Minimum viable version: a single `_DERIVED_DRIVERS` table in `intake.py`
    whose contents are asserted by a test that perturbs each editable row and records
    which derived values move — the table must equal the measured dependency set.
    `static_margin` gets no drivers and the label "fixed target (8% MAC,
    engineering judgment)"; `clmax` lists sweep only.
  - Locks: `::test_driver_chips_match_the_measured_dependency_set` — the perturbation
    sweep is the oracle, so the chips can never drift again.
  - Done when: every chip names a row that measurably moves that number.

- [x] **S3-F7 — `root_bending_index_nm` is labelled "elliptic" but computes uniform load** · honesty · HIGH · cost S
  - Anchors: `aero_estimates.py:74` (field description
    `"~W*b/8, elliptic half-span load"`), `:220` (user-facing assumption
    `"Root bending proxy W*b/8 (elliptic half-span loading)"`), `:239` (the
    computation, `weight_n * params.span_m / 8.0`). `W*b/8` is the **uniform**-load
    result; the elliptic result is `W*b/(3*pi)`, so the code is **17.8% high** relative
    to the loading it claims.
  - Fix: **change the label, not the formula.** `W*b/8` is a defensible conservative
    comparative index and it is the number every existing candidate, golden, and
    tradeoff sentence is built on; changing it would move every candidate comparison
    for no gain. Relabel both strings to
    `"W*b/8 — uniform half-span load, a conservative comparative index (not elliptic;
    the elliptic value is W*b/(3*pi), ~18% lower)"`, and rename the field description
    accordingly. Record in the ledger.
  - Locks: `test_bwb_disclosure.py::test_bending_index_label_matches_the_formula` —
    assert the assumption string contains "uniform" and does **not** contain
    "elliptic", and assert `root_bending_index_nm == weight_n * span_m / 8` so label and
    formula are pinned together.
  - Done when: no user-visible string says "elliptic".

- [x] **S3-F8 — `LAUNCH_VSTALL_MAX`'s documented calibration is false** · honesty · MED · cost S
  - Anchors: `aero_estimates.py:29-31` claims the values are "calibrated so the pack's
    own wing-loading table (`defaults.WING_LOADING`) sits at the boundary". Inverting
    each pair gives implied CLmax **1.0169** (hand) / **0.9477** (catapult) /
    **0.8133** (runway) — spread 0.2036, and none equals the code's own cap 1.1700.
    They are three independent engineering guesses.
  - Fix: replace the comment with the truth and a source or an explicit judgment label,
    per CLAUDE.md: `"Engineering judgment (Astra): highest stall speed each launch
    method tolerates. NOT derived from defaults.WING_LOADING — inverting these against
    that table implies CLmax 1.02/0.95/0.81, which is inconsistent with the pack's own
    CL_MAX_3D cap of 1.17. Revisit against Sadraey §5 / Raymer §5 before treating as
    calibrated."` Add the same to the assumptions string at `:222-224` (which already
    says "engineering default" — keep that and make the constant's comment agree).
  - Locks: `::test_launch_limits_are_labelled_engineering_judgment` (string assertion)
    and `::test_launch_limits_are_not_claimed_to_be_derived` (assert the comment does
    not contain "calibrated"). Add a documenting test that computes the three implied
    CLmax values and asserts they differ from `CL_MAX_2D * CL_MAX_3D_FACTOR`, so the
    inconsistency stays measured rather than remembered.
  - Done when: the code no longer claims a derivation it does not have.

- [x] **S3-F12 — Unsourced global levers presented as physics** · honesty · HIGH · cost M
  - Anchors: `aero_estimates.py:25` `LAMINAR_FRACTION = 0.30` (one line of
    justification; 0.30 → 0.00 moves CD0 **+22%** and `ld_max` **−11%**; → 0.60 moves
    `ld_max` to 19.410), `:26` `INTERFERENCE_MARGIN = 1.10` (the **only** allowance for
    prop, nacelle, gaps, camera, antennas, servo horns — the pack's maximum CD0 is
    0.0233 while measured small-UAV flying wings sit at 0.020–0.035, so the whole band
    is at the optimistic edge), `:27` `CL_MAX_2D = 1.30`, `:28`
    `CL_MAX_3D_FACTOR = 0.90`, and `:93` `mean_tc`'s invented
    `t_mid = 0.5*(t_root + t_tip)`. Also `STATIC_MARGIN_TARGET` is declared **twice**
    — `aero_estimates.py:32` and `MissionDerived.static_margin_target`
    (`schema.py:130`) — equal today, independently declared, free to drift.
  - Fix: (a) each constant gets a citation or an explicit
    `# ENGINEERING JUDGMENT (Astra):` prefix plus its measured sensitivity, mirroring
    the ledger. (b) The `assumptions` list (`:213-224`) gains the sensitivity numbers,
    so the user sees "CD0 assumes a 30% laminar run; at 0% laminar, L/D would be ~11%
    lower". (c) Deduplicate `STATIC_MARGIN_TARGET`: `schema.py:130` imports the
    constant from `aero_estimates` (or both import it from a new
    `domain_packs/bwb/constants.py` if the import direction is awkward — check for a
    cycle; `aero_estimates` already imports `schema`, so **`schema` must not import
    `aero_estimates`** → put it in `constants.py`). *(INFERRED: the cycle direction —
    verify before implementing.)*
  - Locks: `::test_every_declared_constant_has_a_source_or_judgment_label` — reflect
    over the module's uppercase floats and assert each has a comment matching
    `(Raymer|Hoerner|Blasius|Schlichting|Sadraey|Anderson|ENGINEERING JUDGMENT)`;
    `::test_static_margin_target_is_declared_once` (assert
    `schema.MissionDerived().static_margin_target is aero_estimates.STATIC_MARGIN_TARGET`
    by value **and** that only one module literal exists, via a source grep test).
  - Done when: no float in the physics core lacks either a citation or a judgment label,
    and the two static-margin declarations cannot drift.

- [x] **S3-G1h — `easy_launch`'s provenance line is a literal falsehood** · honesty · CRITICAL · cost S
  - Anchors: `variants.py:132-134` prints
    `"Wing loading set to {wl} kg/m^2 (below baseline {baseline})"` **unconditionally**.
    Verified live output: `"Wing loading set to 5.5 kg/m^2 (below baseline 2.6)"` —
    false in **56.0%** of conditions, because the `max(5.5, ...)` floor at
    `variants.py:130` raises loading whenever baseline loading is below 6.875 kg/m².
    The card's own tradeoff sentence then contradicts its title:
    `"stall +2.0 m/s (8.2 m/s — harder launch)"`.
  - Fix: make the sentence conditional on the comparison it asserts — "below baseline"
    only when `wl < derived.wing_loading_kgm2`, otherwise
    `"Wing loading raised to the 5.5 kg/m^2 minimum practical value (baseline
    {b} kg/m^2 is lower)"` with an explicit note that this lens cannot lower the stall
    speed here. The floor itself is a defect and is fixed separately in **S6-G1**.
  - Locks: `tests/test_bwb_lens_intent.py::test_easy_launch_provenance_matches_the_numbers`
    — over a 2880-condition grid (or a hypothesis-sampled subset), assert that a
    provenance line containing "below baseline" implies
    `panel.wing_loading_kgm2 < base.wing_loading_kgm2`. This is the general pattern:
    **every asserted comparison in a provenance string must be tested against the
    numbers it claims.**
  - Done when: no card asserts a comparison that its own panel contradicts.

- [x] **S3-G2h — `max_range`'s objective claim is unqualified and wrong 84% of the time** · honesty · HIGH · cost S
  - Anchors: the objective string at `variants.py:64-66`
    ("Cruise at the best-L/D lift coefficient") and the provenance assertion at
    `variants.py:167-169` ("Wing sized so cruise CL = CL*: S = W/(q*CL*)"), stated as
    fact. Measured: the lens reaches CL* within 2% in only **16.2%** of conditions;
    median `|CL−CL*|/CL*` is 0.612, statistically identical to the baseline's 0.613. An
    area floor binds in 2516/2880 conditions; in the 364 unconstrained conditions the
    fixed point is exact to 2e−5, so the *method* is right and the *claim* is
    unconditional. A separate `constraint_notes` entry (`variants.py:188-191`) retracts
    it, but **nothing tests that the retraction is present when the assertion is false**.
  - Fix: (a) make the provenance line state the achieved state, not the intent:
    `"Target wing sized for CL = CL* ({cl_star:.3f}); achieved CL {cl:.3f}"` plus the
    binding note. (b) Qualify the objective string: "…where the stall and cruise-CL
    limits allow". (c) The retraction becomes mandatory: assert it, do not hope for it.
  - Locks: `::test_max_range_retraction_is_present_whenever_cl_star_is_missed` — over
    the grid, `abs(cl - cl_star)/cl_star > 0.02` implies a `constraint_notes` entry
    naming the binder. `::test_max_range_provenance_reports_achieved_cl`.
  - Done when: no condition exists where the card claims CL = CL* without a retraction.

- [x] **S3-G3h / G4h — `span_efficiency` and `control_margin` overstate their promises** · honesty · MED · cost S
  - Anchors: `variants.py:136-138` prints the **pre-repair** wing loading and area as
    fact, while `_feasible_planform` (`inverse.py:133-192`) subsequently moves the
    area — breaking "at the same wing loading" in **14.8%** overall and **50.0%** at
    mtow 10 kg. `variants.py:70-76`'s "pay for it in span" is false in **33.8%** (span
    *smaller* than baseline), and `easy_launch` has lower induced drag than
    `span_efficiency` in **57.3%**. `control_margin` (`variants.py:84-90`) has the
    highest `v_stall` of all five in **65.9%** of conditions but concedes only "the L/D
    cost".
  - Fix: print the **post-repair** panel values in every provenance line (compute the
    line after `_solve_planform` returns, not before), qualify "pay for it in span" to
    the cases where span actually grew, and add stall to `control_margin`'s conceded
    costs.
  - Locks: `::test_provenance_numbers_match_the_final_panel` (a general assertion over
    all five cards: every number rendered in a provenance string appears in that card's
    own `panel`/`params` to the printed precision), `::test_span_efficiency_span_claim`,
    `::test_control_margin_concedes_stall`.
  - Done when: no provenance string quotes a pre-repair number.

- [x] **S3-G5 — Degeneracy: the five cards collapse to fewer real designs, undetected** · honesty · HIGH · cost M
  - Anchors: clustering by sizing (span within 1e−4 m, area within 1%): **1 distinct
    sizing in 288/2880 conditions (10.0%)** — all at `span_cap = 1.2`; 2 in 2.7%; 3 in
    5.7%. At `span_cap = 1.2` specifically, 40.0% of conditions have all five at
    identical aspect ratio, 56.1% at identical span, 27.1% with all five cruise L/D
    within 2%. Concrete: mtow 0.8, alt 4000, v 10, span_cap 1.2 →
    baseline/max_range/span_efficiency/control_margin all at S=0.5754, b=1.200, AR=2.50,
    L/D 11.75–11.99 — **four cards, one design**. Worst pair: `baseline|max_range` share
    sizing in 47.6%. Nothing reports this.
  - Fix: compute a sizing-cluster signature in `generate_candidates`
    (`variants.py:259-297`) and attach `distinct_sizings: int` plus a per-candidate
    `duplicate_of: Optional[str]` to the batch. The gallery renders "3 of these 5
    concepts are the same wing at this span cap — the cap is doing the designing".
    Making them genuinely distinct is **S6-G5** (capability/defect).
  - Locks: `::test_degeneracy_is_reported_when_sizings_collapse` — the four probed
    collapse conditions must yield `distinct_sizings < 5` and a matching
    `duplicate_of` chain. **Cluster on sizing, not on the 12-parameter vector**: the
    full-parameter distance never drops below 0.15 (sweep/thickness/twist always
    differ), so that metric cannot detect the collapse — assert that too, as a
    documenting test.
  - Done when: the gallery cannot present four identical wings as four choices.

- [x] **S3-G6 — `tradeoff_sentences` rendering and threshold defects** · honesty + defect · MED · cost M
  - Anchors: `variants.py:203-239`. Direction words are **clean** (a 41-point sweep
    found zero wrong-direction words) — do not touch them. The defects are:
    - (a) `:215` renders the hand-launch limit as "the 10 m/s launch limit" because
      `{10.5:.0f}` round-half-even gives 10 — observed live next to a
      "stall 10.50 m/s" sentence on the **same card**. Fix: `:.1f`.
      Catapult/runway render fine, so a test using only those would miss it — **the
      test must parametrize over all three launch methods.**
    - (b) Exact-threshold behaviour is float-dependent and asymmetric: a −10 mm
      neutral-point shift (`:222`) and a −0.10 m span change (`:234`) are silently
      dropped while their positive twins are reported. Fix: compare with a tolerance
      (`>= thr - 1e-9`) so ± are symmetric.
    - (c) `:230` the tip-Reynolds sentence is **absolute, not comparative** (fires on
      `tip_re < 1.5e5` with no reference to `base`), so it fires on nearly every
      small-UAV candidate, fires even when the baseline is equally bad, and makes the
      `"within a few percent of the baseline on every tracked metric"` fallback at
      `:238` **unreachable** for any low-tip-Re design. Fix: make it comparative
      (fire only when the candidate crosses the threshold and the baseline does not),
      and keep an absolute variant in `constraint_notes` where an absolute warning
      belongs.
    - (d) `:231` prints "tip Reynolds 1.5e+05 below 1.5e+05" because `:.2g` rounds
      1.49e5 up to the threshold it claims to be below. Fix: `:.3g` or print the
      threshold comparison, not the rounded pair.
    - (e) `:217-220` and `:233-236` are perfectly redundant: since
      `root_bending_index = weight * span / 8` and weight is identical across
      candidates, bending %-change **is** span %-change exactly; they differ only
      because one threshold is relative (5%) and one absolute (0.1 m). Fix: emit one
      sentence, mentioning both consequences, on one threshold.
    - (f) `tradeoff_sentences` is **never applied to the baseline** —
      `variants.py:282` hardcodes `["reference design — all comparisons are against
      this"]` — so a baseline that exceeds its own launch limit is never flagged.
      Fix: keep the placeholder, but always run the *absolute* checks (launch limit,
      tip Re, cap breach) on the baseline and put them in the baseline's
      `constraint_notes`.
  - Locks: `::test_launch_limit_renders_one_decimal` parametrized over hand/catapult/
    runway; `::test_threshold_symmetry` (±10 mm np, ±0.10 m span both reported);
    `::test_tip_re_sentence_is_comparative`;
    `::test_no_baseline_only_fallback_when_a_metric_moved`;
    `::test_baseline_launch_breach_is_flagged`.
  - Done when: no sentence prints a number that contradicts another sentence on the
    same card, and the fallback string is reachable.

**Stage 3 exit:** every clamp, substitution, inert row, unsourced constant, and
degenerate concept is disclosed; no provenance string asserts a comparison its own
numbers contradict; the disclosure claims are themselves tested against the
computation, not hand-maintained.

---

## Stage 4 — Constraint adherence

**Depends on:** Stage 1 (batch writes), Stage 0 (the xfail cap matrix). **Blocks:**
nothing downstream, but this is the item that makes the 11 xfails from S0-6 go green.

Commit 87b3b94 fixed the **span** cap. The **stall** cap is still broken.

**Files:**
- Modify: `domain_packs/bwb/variants.py`, `domain_packs/bwb/inverse.py`,
  `src/services/candidates.py`
- Modify: `tests/test_bwb_candidate_caps.py`; Create: `tests/test_candidate_staleness.py`

- [x] **S4-C1 — The stall-speed area floor is undone by the AR-gate and root-chord repairs** · defect · CRITICAL · cost L
  - Anchors: `variants.py:144-150` applies the stall floor
    (`s_floor = max(2W/(rho*v_cap^2*clmax), W/(q*MAX_CRUISE_CL))`) and then calls
    `_solve_planform` → `inverse._feasible_planform` (`inverse.py:133-192`), whose
    AR-gate branch (`:159-165`) and root-chord branch (`:166-174`) **shrink the area
    back below the floor**. Same shape at `variants.py:176-192` for the CL*-sized lenses.
  - Now: the candidates narrate the contradiction themselves in adjacent
    `constraint_notes` — `"Wing grown to 0.419 m^2 to honour stall <= 10.5 m/s"`
    followed by `"Wing area capped at 0.400 m^2 by the AR gate (2.5) at the 1.00 m span
    cap"`. Confirmed violations (independently reproduced twice): (mtow 20 kg, span cap
    0.9 m, hand) → stalls 29.3–30.2 vs cap 10.5 in **5/5**; (mtow 25 kg, stall cap 5.0,
    hand) → 13.4–14.4 vs 5.0 in **5/5**; (mtow 2 kg, span cap 0.8) → 1/5. **11 of 13**
    probed cap scenarios violated.
  - Fix approach: make the stall floor a **constraint on the repair loop**, not a value
    applied before it.
    1. Extend `_feasible_planform` with an optional `s_floor: Optional[float]` and
       treat "area below `s_floor`" as an infeasibility the loop must report rather than
       silently accept: when the AR gate or root-chord limit would push `s` below
       `s_floor`, stop, keep `s = s_floor` if any legal geometry exists there, and set a
       machine-readable outcome.
    2. When **no** legal geometry satisfies both the floor and the gates, the honest
       answer is not a quietly violating design: return the closest feasible planform
       (preserving `inverse.py`'s documented never-raises contract) **plus** a
       structured `violations: List[CapViolation]` naming the cap, the required value,
       the achieved value, and the gate that blocked it. That structure feeds S4-C4.
    3. Fix the ordering so the *user's* cap dominates the *schema's* convenience
       repairs: the AR gate and root-chord range are schema limits; a user's hard stall
       requirement outranks a cosmetic AR preference. Where they truly conflict, prefer
       reporting the conflict over silently choosing the schema.
  - Locks: the S0-6 matrix (11 `xfail(strict=True)` rows flip to pass); plus
    `::test_no_candidate_ever_exceeds_the_effective_stall_cap` as a **hypothesis
    property** over `design_requirements()` — for every drawn requirement, every
    candidate either satisfies
    `v_stall_ms <= min(user_cap, LAUNCH_VSTALL_MAX[launch]) + tol` or carries a
    `violations` entry naming stall. No third outcome.
  - Done when: 0 of 13 probed scenarios violate silently; the property test passes on
    at least 500 hypothesis examples; the contradictory adjacent-notes pair is
    impossible (add `::test_notes_do_not_contradict_each_other`: no candidate may hold
    both a "grown to honour stall" and an "area capped" note).

- [x] **S4-C1b — `solve_planform` raises span back to 0.8 m unconditionally** · defect · MED · cost S
  - Anchors: `inverse.py:105-107` — `if span < 0.8: span = 0.8; ar = span**2/s_ref`,
    with no reference to `span_cap`, overriding a tighter requirement and leaving no
    note (contrast `:99-104`, which does note the cap).
  - Fix: clamp to `min(0.8, span_cap)` is wrong (0.8 is the schema minimum); instead,
    detect `span_cap < 0.8` as an unsatisfiable requirement and report it through the
    same `violations` channel as S4-C1, and add a note whenever the minimum-span floor
    moves the span, matching the wording style at `:104`.
  - Locks: `tests/test_bwb_inverse.py::test_span_floor_is_noted`,
    `::test_span_cap_below_schema_minimum_is_reported`.
  - Done when: no span change is silent.

- [x] **S4-C2 — The `baseline` candidate is never re-checked against the user's caps** · defect · HIGH · cost S
  - Anchors: `variants.py:280` passes `derived.params` through verbatim; the caps at
    `variants.py:271-273` are applied only to the four lenses (`:286-296`).
  - Now: plan solved at `span_max 3.0` (baseline span 1.2153 m), requirements later
    saved as `span_max 1.2`, regenerate → all four lenses at 1.200 m, **baseline still
    1.2153 m**.
  - Fix: run the same cap check on the baseline. Since the baseline is by definition
    "the deterministic sizing default, not an optimum" (`variants.py:278-279`), the
    right behaviour is: re-solve it under the caps if it violates them, and if the
    re-solve changes it, say so in its `constraint_notes` (it currently has none) —
    or, if the baseline must stay untouched for comparability, mark it explicitly as
    "outside your current constraints" and exclude it from selection. **Recommend
    re-solving**: a card the user can pick must obey the caps the user set.
  - Locks: `::test_baseline_respects_the_span_cap`,
    `::test_baseline_respects_the_stall_cap`, both parametrized over the S0-6 matrix.
  - Done when: the baseline is subject to the same caps as every other card.

- [x] **S4-C3 — Requirements changes do not invalidate the candidate batch** · defect · CRITICAL · cost M
  - Anchors: requirements are deliberately not a stage
    (`src/services/design_session.py:182-187`), so `_plan_version`
    (`candidates.py:52-53`) does not move; `candidates.generate` short-circuits on
    `os.path.exists(idx)` (`candidates.py:96`); the batch index records only
    `plan_version`, `generated`, `keys` (`candidates.py:129-133`) — **no
    `requirements_version`**, so nothing downstream can detect staleness.
  - Now: saving `span_max` 3.0 → 1.2 then calling `generate()` returned the **old**
    batch with 4 of 5 candidates violating the live cap.
  - Fix: record `requirements_version` in the batch index (both writers:
    `candidates.py:129-133` and `:353-357`, using
    `design_session._latest_requirements_version`), and compare it at read time in
    `list_candidates` (`:137-145`) and `generate` (`:92-97`). A mismatch marks the
    batch `stale: true` with the two version numbers; `generate` regenerates instead of
    short-circuiting. Keep the requirements store non-staging (that design is correct
    — do not turn requirements into a stage); staleness detection belongs at read time,
    exactly as `design_session.py:182-187` already describes for geometry.
  - Locks: `tests/test_candidate_staleness.py::test_requirements_change_marks_the_batch_stale`,
    `::test_generate_after_a_requirements_change_regenerates`,
    `::test_batch_index_records_the_requirements_version`.
  - Done when: no batch generated under different requirements can be returned as
    current without a `stale` flag.

- [x] **S4-C4 — Candidate records carry no violation flag** · honesty + defect · CRITICAL · cost S
  - Anchors: the record written at `candidates.py:112-126` has
    `evidence: {"equation": {"passed": True}}` unconditionally, and
    `DesignCandidate` (`variants.py:28-40`) has no violation field. The gallery
    therefore cannot tell the user its concepts break the user's own hard cap.
  - Fix: add `violations: List[CapViolation]` to `DesignCandidate` and mirror it into
    the record; `evidence.equation.passed` becomes `not violations`. `stage1.js`
    renders a red badge and disables promotion of a violating candidate (or requires an
    explicit override with a recorded reason). **Ship this even before S4-C1 lands** —
    it is cheap, and a disclosed violation is vastly better than a hidden one.
  - Locks: `::test_violating_candidate_is_flagged_and_not_silently_promotable`;
    `test_stage1_api_robustness.py::test_gallery_response_surfaces_violations`.
  - Done when: no candidate that breaches a user cap reports `passed: True`.

**Stage 4 exit:** every candidate, baseline included, either satisfies the effective
span and stall caps or carries a machine-readable violation; a requirements change can
never yield a stale batch silently; the 11 `xfail` cap scenarios pass.

---

## Stage 5 — Candidate lifecycle state machine

**Depends on:** Stage 1 (atomic writes — the version-leak fix depends on them), Stage 4
(violating candidates must not be promotable).

`STATUSES` (`src/services/candidates.py:33`) is **dead code**: declared, with zero
references anywhere in `src/`, `domain_packs/`, or the cockpit JS. There is no status
validation, no legal-transition table, and no guard at the HTTP layer
(`src/cockpit/server.py:749-804`). Only `build` (`:163`) and `screen` (`:253`) have
ratchets; `promote` (`:276`) and `discard` (`:285`) write unconditionally.

**Files:**
- Modify: `src/services/candidates.py`, `src/cockpit/server.py`,
  `src/services/geometry.py` (exception surface only)
- Create: `tests/test_candidate_lifecycle.py`

- [x] **S5-H1 — No status machine: `STATUSES` is dead code** · defect · CRITICAL · cost M
  - Anchors: `candidates.py:33`; no reader anywhere.
  - Fix: promote `STATUSES` to a real machine: a `TRANSITIONS: Dict[str, Set[str]]`
    table, a `_transition(record, new_status)` helper that validates and raises
    `CandidateError` on an illegal move, and a single write path so no function sets
    `record["status"]` directly. Map `CandidateError` → 409/422 at the HTTP layer.
  - Locks: `test_candidate_lifecycle.py::test_transition_table_is_total` (every status
    × every action has a defined outcome) and a parametrized
    `::test_illegal_transitions_are_refused` over the full cross product.
  - Done when: `status` can only change through `_transition`, proven by a source test
    asserting no other assignment to `record["status"]` exists.

- [x] **S5-H2 — `promote` has no preconditions** · defect · CRITICAL · cost S
  - Anchors: `candidates.py:259-280` writes unconditionally. Confirmed unguarded
    promotions: `candidate` → promote with **no geometry evidence** (succeeds, writes
    geometry v2); `failed` → promote (a geometry-**rejected** candidate goes straight
    onto the main line); fast-aero-rejected (`built`) → promote.
  - Fix: require, before any promote, that (a) `evidence.geometry.passed is True`,
    (b) `status` is in `{"built", "screened"}`, (c) no `violations` (S4-C4), and
    (d) fast-aero evidence, if present, passed — or an explicit
    `override_reason: str` is supplied and recorded in the geometry manifest.
  - Locks: `::test_promote_requires_passing_geometry_evidence`,
    `::test_promote_refuses_failed_and_violating_candidates`,
    `::test_promote_override_is_recorded`.
  - Done when: nothing without passing evidence reaches the main geometry line.

- [x] **S5-H3 — Terminal states are not terminal; promotion is not idempotent** · defect · HIGH · cost S
  - Anchors: `candidates.py:276-278`, `:283-289`. Confirmed broken:
    `discarded` → promote becomes `promoted` **while retaining `discard_reason`** (the
    record self-contradicts); `promoted` → discard becomes `discarded` while the
    session geometry stage stays **valid and sourced from it** (the live manifest reads
    `candidate_key: easy_launch, source: candidate` while that candidate reads
    `discarded`); `promoted` → promote again **overwrites**
    `promoted_geometry_version` (v2 → v3) and loses the first reference;
    `discarded` → build does the work but the status **stays** `discarded`.
  - Fix: (a) `discard` on a promoted candidate must either be refused or must
    invalidate the geometry stage it sourced — refuse by default, with an explicit
    `unpromote` action if the workflow needs it. (b) Un-discard is an explicit action
    that clears `discard_reason`; promote from `discarded` is refused. (c)
    `promoted_geometry_version` becomes `promoted_geometry_versions: List[int]`
    (append-only) so no reference is lost. (d) `build`/`screen` on a `discarded`
    candidate is refused rather than performed-and-ignored.
  - Locks: `::test_discarded_cannot_be_promoted`,
    `::test_promoted_cannot_be_discarded_while_geometry_sources_it`,
    `::test_repromote_appends_a_version`, `::test_build_on_discarded_is_refused`.
  - Done when: no record can hold two contradictory facts about itself.

- [x] **S5-H4 — A `failed` candidate can never recover; rejection is asymmetric** · defect · HIGH · cost S
  - Anchors: the ratchet at `candidates.py:163`
    (`"built" if record["status"] == "candidate" else record["status"]`) and the same
    shape at `:253`. Confirmed: `failed` → build (now passing) leaves
    `status="failed"` while `evidence.geometry.passed == True`. And
    `GeometryRejected` sets `status="failed"` (`:156-161`) while `FastAeroRejected`
    writes failing evidence and **leaves the status untouched** (`:211-216`) — verified
    status stayed `built` with `evidence.fast_aero.passed == False`.
  - Fix: derive status from evidence through the S5-H1 transition table rather than
    ratcheting: a passing rebuild moves `failed` → `built`. Make the two rejection
    paths symmetric — `FastAeroRejected` sets a distinct `screen_failed` status (or
    `failed` with a `failed_stage` field) so "which tier rejected me" is machine
    readable.
  - Locks: `::test_failed_candidate_recovers_after_a_passing_rebuild`,
    `::test_geometry_and_fast_aero_rejection_are_symmetric`.
  - Done when: status and evidence can never disagree about pass/fail.

- [x] **S5-H5 — Error paths escape as HTTP 500 instead of the intended 4xx** · defect · HIGH · cost M
  - Anchors:
    - `candidates.py:203-205` — `screen()` skips the rebuild whenever
      `"geometry" in evidence`, but a **failed** build also populates that key without
      writing `geometry_report.json` → bare `FileNotFoundError` at `:204` → HTTP 500
      instead of the intended 422 (`server.py:757-761`).
    - `candidates.py:155`, `:265` catch only `GeometryRejected`. `build_geometry` also
      raises `subprocess.TimeoutExpired` (600 s), `FileNotFoundError` when `VSP_PYTHON`
      is missing (`src/services/geometry.py:20` defaults to
      `/opt/vsp-venv/bin/python`), and `pydantic.ValidationError` when the builder
      exits 0 without a report (`geometry.py:67`). All escape raw → HTTP 500 with **no
      evidence recorded**, leaving the record at `status="candidate"` with no trace of
      the attempt.
    - `candidates.py:52` — `_plan_version` → `ds.get_state` raises `FileNotFoundError`
      for a bogus sid, so `list_candidates`, `stl_path`, and `generate` raise
      `FileNotFoundError` not `CandidateError`; `server.py:738-740` and `:798` have no
      handler → **500 instead of 404**.
  - Fix: (a) gate the rebuild on `evidence.geometry.passed`, not key presence, and on
    `os.path.exists(geometry_report.json)`. (b) Wrap the builder call in a single
    `except Exception` that records failing evidence with the exception class and
    message before re-raising as `CandidateError` — an environment failure must leave a
    trace. (c) Convert `FileNotFoundError` from `_plan_version` into
    `CandidateError("unknown session")` and map it to 404 at the HTTP layer.
  - Locks: `::test_screen_after_a_failed_build_returns_422`,
    `::test_builder_environment_failure_records_evidence` (monkeypatch `VSP_PYTHON` to
    a nonexistent path), `::test_bogus_session_returns_404_not_500` over
    `/api/candidates/{sid}`, `/api/candidates/stl/...`, and the mutators.
  - Done when: no Stage-1/candidate route returns 500 for a condition the caller caused.

- [x] **S5-H6 — Evidence can record a pass it did not earn** · defect · HIGH · cost S
  - Anchors:
    - `candidates.py:239` — `all(g["passed"] for g in fast.get("gates", []))`;
      `all([])` is **True**, so an empty gates list records `passed: True` with zero
      gates evaluated.
    - `candidates.py:227-237` — `if cd_ind:` is falsy for `0.0`, so `hybrid` stays `{}`
      and the deltas at `:246-251` become `None` with no explanation; a **negative**
      `cd_ind` is truthy and writes `ld_hybrid = 110.97` and
      `oswald_e_measured = -0.517` into evidence unguarded.
    - `candidates.py:269-275` — `record_version(... "passed": report.all_gates_pass)`
      marks the geometry stage **valid even when `all_gates_pass` is False**;
      `build_geometry` only raises on a non-zero exit code (`geometry.py:61`), so a
      builder reporting a failed gate with exit 0 is promoted onto the main line.
  - Fix: require a non-empty gate list (`passed = bool(gates) and all(...)`, with a
    `reason: "no gates evaluated"` when empty); replace `if cd_ind:` with
    `if cd_ind is not None and cd_ind > 0:` and record an explicit
    `hybrid_unavailable_reason` for the zero/negative branches; refuse to
    `record_version` a geometry whose `all_gates_pass` is False (raise
    `CandidateError` before the version is recorded).
  - Locks: `::test_empty_gate_list_is_not_a_pass`,
    `::test_zero_and_negative_induced_drag_are_explained`,
    `::test_geometry_with_failing_gates_is_not_recorded_valid`.
  - Done when: `passed: True` always corresponds to at least one evaluated gate.

- [x] **S5-H7 — Promote leaks a geometry version and cross-contaminates the next manifest** · defect · CRITICAL · cost M
  - Anchors: `candidates.py:262-268` — `ds.new_version_dir` creates `geometry/v{n}` on
    disk (`design_session.py:79-86`); on `GeometryRejected`, `record_version` is never
    called so `latest` stays 0; the **next** promote gets the **same** `v1` path,
    already containing the failed candidate's artifacts, and
    `record_version`'s `_artifact_inventory` (`design_session.py:105-130`) hashes them
    into the new manifest as evidence for a **different** candidate. Verified: a
    manifest with `candidate_key: span_efficiency` whose inventory lists files written
    while promoting `max_range`.
  - Fix: build into a temporary directory and move into the version directory only on
    success; or allocate the version directory, and on failure delete it (and any
    partial artifacts) before re-raising. Additionally have `record_version` refuse a
    directory that contains files not written by the current call — pass an expected
    file list, or stamp a per-attempt token file and assert it. **This is an evidence-
    integrity bug: a manifest currently attests to the wrong candidate's artifacts.**
  - Locks: `::test_failed_promote_leaves_no_version_directory`,
    `::test_manifest_inventory_belongs_to_its_own_candidate` (fail a promote, succeed
    the next, assert the inventory hashes match only the second candidate's files).
  - Done when: no manifest can list an artifact it did not produce.

- [x] **S5-H8 — One missing directory makes the whole batch unreadable** · defect · MED · cost S
  - Anchors: `candidates.py:145` — a comprehension over `meta["keys"]`, so one missing
    candidate directory raises for the entire listing; `:144` —
    `meta["plan_version"]`/`meta["generated"]` `KeyError` on a legacy or truncated
    `index.json` (contrast the defensive `.get("keys", [])` at `:318`); `:292` —
    `stl_path` returns a path with no existence check (the cockpit checks at
    `server.py:801`, the MCP path does not).
  - Fix: degrade per candidate (skip with a recorded `unreadable: [keys]` list rather
    than raising), use `.get` with defaults for the index metadata, and make `stl_path`
    raise `CandidateError` when the file is absent so both callers behave alike.
  - Locks: `::test_partially_deleted_batch_is_listable`,
    `::test_legacy_index_without_plan_version_is_readable`,
    `::test_stl_path_raises_for_a_missing_file`.
  - Done when: a damaged batch degrades instead of disappearing.

- [x] **S5-H9 — `_candidate_derived` bypasses `MissionDerived`'s atmosphere validator** · defect · MED · cost S
  - Anchors: `candidates.py:179-189` uses `model_copy(update=...)`, which in pydantic
    v2 **skips validators** — so `MissionDerived.validate_atmosphere_consistency` never
    runs and the reynolds/density invariant is bypassed for **every screened candidate**.
  - Fix: reconstruct through the model (`MissionDerived.model_validate({**base.model_dump(),
    **update})`) so the cross-check runs, or call the validator explicitly and raise
    `CandidateError` on inconsistency.
  - Locks: `::test_candidate_derived_revalidates_the_atmosphere_invariant` (construct
    an inconsistent update and assert it is rejected).
  - Done when: no `MissionDerived` reaches VSPAERO without its cross-check.

- [x] **S5-H10 — `add_mdo_candidates` silently destroys records** · defect · MED · cost S
  - Anchors: `candidates.py:325-327` and `:350-351` — the key is `f"mdo_{rank}"` and
    `rank` is unvalidated, so three winners at rank 1 produce **one** record on disk
    with `"mdo_1"` appearing three times in the index and `list_candidates` returning it
    three times. `:328`, `:341` — a missing score raises
    `TypeError: unsupported format string passed to NoneType.__format__`.
    `generate(regenerate=True)` drops all `mdo_*` keys from the index while **orphaning
    their directories** (`:129-133`), asymmetric with `add_mdo_candidates`, which
    carefully preserves non-mdo keys (`:318`). MDO winners can be added to a session
    with no lens batch at all, producing a baseline-less batch with nothing to compare
    against. (Lens/mdo key collision is impossible — prefix-separated — so that worry
    is unfounded; do not guard it.)
  - Fix: validate `rank` (positive int, unique within the call), reject duplicates with
    a named error, default a missing score to `float("nan")` rendered as `"n/a"` rather
    than crashing, delete orphaned `mdo_*` directories on `regenerate=True`, and require
    a lens batch (or synthesize the baseline) before writing MDO winners.
  - Locks: `::test_duplicate_ranks_are_rejected`, `::test_missing_score_does_not_crash`,
    `::test_regenerate_removes_orphaned_mdo_directories`,
    `::test_mdo_without_a_lens_batch_is_refused_or_seeds_the_baseline`.
  - Done when: no MDO write can lose a winner.

- [x] **S5-H11 — Plan rollback resurrects a stale batch; the module docstring claims otherwise** · defect + honesty · MED · cost S
  - Anchors: `candidates.py:14-16` states "Promotion writes a normal geometry version
    on the main line, so rollback/versions keep working unchanged" — **false**. A plan
    bump forward is accidentally safe (clean `CandidateError` from `_plan_derived`) but
    **orphans the old batch forever**; a plan **rollback** resurrects a stale batch with
    no guard, because `index.json` stores `plan_version` but nothing ever compares it to
    the current one at read time. No rollback endpoint exists today, so this is latent.
  - Fix: the same read-time comparison as S4-C3, extended to `plan_version`; correct
    the docstring to state exactly what rollback does and does not preserve.
  - Locks: `::test_plan_rollback_marks_the_resurrected_batch_stale`,
    `test_bwb_disclosure.py::test_candidates_docstring_matches_rollback_behaviour`.
  - Done when: no plan version change, in either direction, yields an unflagged batch.

**Stage 5 exit:** a validated transition table with no direct status writes; promotion
requires passing evidence and no violations; every error path returns a 4xx with
recorded evidence; no manifest attests to another candidate's artifacts.

---

## Stage 6 — Physics core, lens math, and solver robustness

**Depends on:** Stage 0 (goldens and invariants — **do not start this stage without
them**), Stage 3 (the honesty layer already told users what these models do and do not
capture, so a fix here is an improvement, not a surprise).

Every item in this stage moves numbers. Each one therefore needs a golden diff
reviewed against the ledger, and each needs its effect on candidate **ranking**
assessed (see Risks).

**Files:**
- Modify: `domain_packs/bwb/aero_estimates.py`, `domain_packs/bwb/schema.py`,
  `domain_packs/bwb/variants.py`, `domain_packs/bwb/inverse.py`
- Create: `tests/test_bwb_aero_properties.py`, `tests/test_bwb_inverse_properties.py`

- [x] **S6-F1 — 22.31% of the flight envelope reports a cruise point BEYOND STALL, unflagged** · defect · CRITICAL · cost M
  - Anchors: `aero_estimates.py:200` computes `cl = weight_n/(q*s)` with no comparison
    to `clmax` at `:206`; `AeroEstimate` (`:35-78`) has **no** `cruise_feasible` or
    `stall_margin` field; `defaults.MAX_CRUISE_CL = 0.75` is applied only in
    `defaults.derive()`, never inside `estimate()`. Downstream, `variants.py:214` checks
    only `launch_ok is False` and `variants.py:205` ranks on `ld_cruise`, so infeasible
    points compete against feasible ones as equals.
  - Now: `cl_cruise > cl_max_3d` in **26,238/117,600 cases (22.31%)**, identically
    `v_ms < v_stall_ms`. `cl_cruise` reaches **136.43** (p99 20.74). Worst case has
    `launch_ok=True` while the aircraft cannot sustain the requested cruise.
  - Fix: add `cruise_feasible: bool` and `stall_margin: float`
    (`(v_ms - v_stall_ms)/v_stall_ms`) to `AeroEstimate`, computed in `estimate()`.
    Then: `variants.tradeoff_sentences` emits a sentence when infeasible;
    `generate_candidates` marks the candidate as violating (reuse the S4-C4
    `violations` channel); and ranking must not place an infeasible candidate above a
    feasible one.
  - Locks: `tests/test_bwb_aero_properties.py::test_cruise_feasible_is_exactly_v_above_stall`
    (hypothesis: `cruise_feasible == (v_ms >= v_stall_ms)` for every drawn case, and
    `cl_cruise > cl_max_3d` implies `not cruise_feasible`);
    `test_bwb_lens_intent.py::test_infeasible_candidates_never_outrank_feasible_ones`.
  - Done when: no panel can report `launch_ok=True` with `cruise_feasible=False`
    without both flags visible in the response.

- [x] **S6-F9 / F10 — `estimate()`'s flight condition and launch string are unvalidated** · defect · HIGH · cost S
  - Anchors: `aero_estimates.py:184-186` (signature), `:193-207` (the arithmetic),
    `:208` (`LAUNCH_VSTALL_MAX.get(launch) if launch else None`), `:236`
    (`launch_ok=(v_stall <= launch_limit) if launch_limit else None`).
  - Now: `v_ms=0` raises a bare `ZeroDivisionError`; `v_ms=-5` **silently returns a
    plausible-looking panel** with `reynolds_mac=-34348` (the `max(re, 1e4)` floor at
    `:115` laundered it); `v_ms=NaN` returns all-NaN silently; `weight_n=inf` returns
    `cl=inf` silently; `weight_n=-10` raises an accidental `math domain error` from
    `sqrt` **after** cl/cd were computed. An unknown launch string **silently disables
    the launch gate**: `estimate(..., launch="glider")` returns `limit=None, ok=None`
    with `v_stall=24.71` — a value that would have failed all three real limits.
    `MissionSpec.launch` is a `Literal` (`schema.py:26`) but `estimate()`'s parameter is
    a plain `Optional[str]`. `:236` also tests truthiness, so a limit of `0.0` would be
    skipped.
  - Fix: validate at the top of `estimate()` — `v_ms` finite and `> 0`, `weight_n`
    finite and `> 0`, raising `ValueError` with the offending value (matching
    `AtmosphereState`'s style, `atmosphere.py:53-117`). Narrow the parameter type to
    `Optional[Literal["hand","catapult","runway"]]` and **raise** on an unrecognised
    non-None launch instead of returning `None`. Replace `if launch_limit` with
    `if launch_limit is not None`.
  - Locks: `::test_invalid_flight_conditions_raise` parametrized over
    `v_ms ∈ {0, -5, nan, inf}` × `weight_n ∈ {0, -10, nan, inf}`;
    `::test_unknown_launch_string_raises_not_silently_disables_the_gate`.
  - Done when: no input produces a silently wrong panel; every rejection names the field.

- [x] **S6-F11 — `BWBParams` has `validate_assignment=False`, so mutation bypasses revalidation** · defect · HIGH · cost S
  - Anchors: `domain_packs/bwb/schema.py:29-58` declares no `model_config`, so pydantic's
    default `validate_assignment=False` applies; CLAUDE.md flags mutable-by-reference
    state as a live pattern (`GraphState`).
  - Now: `p.centre_body_fraction = 0.0` → `effective_c4_sweep_deg` and `cl_max_3d`
    raise `ZeroDivisionError`, while `neutral_point_m` returns a **silently wrong**
    0.1727 and `mean_tc` a wrong 0.11. The `taper = ct/cr if cr > 0 else 1.0` guard at
    `aero_estimates.py:167` shows the author anticipated `cr=0`, but it is ineffective:
    the real division by zero is the area normalisation at `:177`.
  - Fix: `model_config = ConfigDict(validate_assignment=True)` on `BWBParams` (and
    consider `frozen=True` — **INFERRED**: check whether any caller mutates params in
    place before choosing; `model_copy(update=...)` is the safe alternative and is
    already used in `candidates.py:183`, though see S5-H9). Independently, guard the
    normalisation at `aero_estimates.py:177` and `:94`.
  - Locks: `tests/test_bwb_derivation.py::test_post_construction_mutation_is_revalidated`
    parametrized over the fields whose zero/extreme values break the estimators.
  - Done when: no assignment can put a `BWBParams` into a state the constructor rejects.

- [x] **S6-AE1 — `AeroEstimate` has no validator (unlike `AtmosphereState` in the same pack)** · defect · HIGH · cost M
  - Anchors: `AeroEstimate` (`aero_estimates.py:35-78`) accepts NaN, inf,
    `oswald_e=99`, `ld_max=-99`. Contrast `AtmosphereState`
    (`atmosphere.py:53-117`), which enforces finiteness, positivity, and
    cross-consistency. Inconsistent rigor within one pack.
  - **Decision: yes, add one — see Open questions, Q4.** Scope it as an
    *output-consistency* validator: all floats finite; `s_ref_m2`, `aspect_ratio`,
    `span_m`, `mac_m`, `v_ms`, `rho_kgm3`, `weight_n`, `cd0`, `cd_cruise` positive;
    `0 < oswald_e <= 1`; and the **five already-holding invariants** from S0-9 as
    cross-checks. Do **not** add `cl_cruise <= cl_max_3d` — that condition is violated
    by 22% of the legal envelope and is the subject of S6-F1's flag, not a rejection.
  - Sequencing: land **after** S6-F9 (which stops the NaN/inf producers), so the
    validator does not convert today's silent-NaN paths into new 500s in
    `variants`/`sensitivity`/`objective`.
  - Locks: `::test_aero_estimate_rejects_non_finite_and_impossible_values` parametrized
    over the four probed bad payloads; plus the S0-9 invariant tests, which now hold at
    two levels (model and test).
  - Done when: `AeroEstimate` cannot be constructed in a state the physics forbids, and
    the full suite (including MDO and sensitivity) still passes.

- [x] **S6-F2 — CD0 is systematically 8–12% optimistic; the Cf attribution is wrong; the Re floor launders invalid input** · defect + honesty · HIGH · cost M
  - Anchors: `aero_estimates.py:113-118`. The area-fraction blend evaluates the laminar
    Cf at the **full-chord** Re instead of at `Re_transition = 0.30*Re`, understating the
    laminar segment by `1/sqrt(0.30) = 1.83×`. Measured bias: **−12.4%** at Re 2e5,
    −10.1% at 5e5, −8.5% at 1e6, −7.1% at 2e6. Every L/D inherits it. Separately, the
    comment at `:116` attributes `0.455/(log10 Re)^2.58` to **Schlichting**; it is
    **Raymer eq. 12.27** (Schlichting's own mean-Cf form is 17.6–19.3% lower). The
    `max(re, 1e4)` floor at `:115` launders invalid input: Cf is exactly flat at
    `0.012892296409079974` for Re = 9999, 1000, 100, 1.0, 0.0 **and −5.0**, and that
    floor value is **3.26× below** true Blasius at Re 1e3 (non-conservative).
  - Fix: (a) evaluate `cf_lam` at `laminar_fraction * re` and document the transition
    assumption; (b) correct the attribution comment to Raymer eq. 12.27; (c) replace the
    silent floor with an explicit `ValueError` for `re <= 0` and a **flagged** floor
    (return value plus a note) for `0 < re < 1e4`, so a sub-1e4 Re is visible rather
    than laundered.
  - Consequence: every L/D drops ~7–12%. **This is the largest golden diff in the
    plan.** Regenerate goldens with a ledger entry, and re-check every threshold
    calibrated against the old numbers (notably `MAX_CRUISE_CL`, the MDO objective's
    reference score, and `verification/verify_asb_aero.py`'s L/D ±25% tolerance).
  - Locks: `::test_laminar_cf_uses_the_transition_reynolds` (assert
    `skin_friction_coeff` at the four probed Re values equals the corrected closed form
    to 1e-12); `::test_invalid_reynolds_raises`;
    `::test_cf_below_the_floor_is_flagged`;
    `test_bwb_disclosure.py::test_cf_attribution_names_raymer`.
  - Done when: the four measured biases are gone, the goldens are regenerated with a
    ledger entry, and no Re ≤ 0 reaches the formula.

- [x] **S6-F4 — Two mutually contradictory "effective sweep" definitions** · defect · HIGH · cost M
  - Anchors: `aero_estimates.py:97-100` averages **angles** (half-width weighted);
    `:103-110` averages **tangents** then takes `atan`. By Jensen's inequality this makes
    the quarter-chord sweep **exceed** the leading-edge sweep in **50,817/596,700 legal
    geometries (8.5%)** — geometrically impossible when both panels taper aft, which the
    schema guarantees. Worked case: `span 1.8, cr 0.25, cbf 0.45, mcf 0.85, taper 0.7,
    sweeps 60/40` → LE 49.000, c/4 50.493 (**+1.49°**), while both per-panel values are
    correctly below their LE sweep. One definition feeds Oswald e (`:198`), the other
    feeds CLmax (`:154`) — the drag and stall estimates use **contradictory sweep
    conventions**. The `tan(Lc4) = tan(LLE) − 0.25(cr−ct)/half_width` relation itself is
    derived correctly.
  - Fix: pick one convention (recommend **tangent averaging** for both, which is the
    geometrically meaningful mean for sweep) and apply it to `effective_le_sweep_deg`
    too. Then add the invariant `effective_c4_sweep_deg <= effective_le_sweep_deg` for
    aft-tapering planforms as a model-level cross-check.
  - Locks: `::test_c4_sweep_never_exceeds_le_sweep` as a hypothesis property over
    `bwb_params()` (xfail-encoded in Stage 0; flips here) plus the F4 worked case as a
    golden.
  - Done when: the property holds over ≥5,000 hypothesis examples and the two functions
    share one documented convention. Note this changes Oswald e (via the LE sweep) for
    every geometry — golden diff plus ledger entry.

- [x] **S6-F3 — Oswald e is pinned at a clamp over 23% of the design space, with zero gradient** · defect + honesty · HIGH · cost M
  - Anchors: `aero_estimates.py:126-138`. **23%** of the legal (AR × effective-LE-sweep)
    space is pinned at a clamp (11.7% at `e=0.500`, 11.2% at `e=0.950`) where e has
    **zero gradient** in both AR and sweep — and `domain_packs/bwb/sensitivity.py`
    computes finite-difference derivatives of exactly this quantity. At AR=10 every
    effective sweep above 25.95° returns identical 0.500. The blend **is** continuous at
    0° and 30° (verified 9e−9 at the breakpoint) but **C1-discontinuous** (slope jumps
    2.4× at AR=8). Worse: **e increases with sweep for AR 3.6–4.7** (wrong sign; e.g.
    AR=4.0: 0.9426 at 6.2° → 0.9462 at 12°) because Raymer's straight- and swept-wing
    fits cross at **AR=4.800**, and the unclamped fits give physically impossible
    **e>1** below AR 4.8 (1.123 at AR 2.5), which the 0.95 clamp **hides rather than
    flags**. The coefficients themselves are correct Raymer transcriptions.
  - Fix (staged, and deliberately conservative — replacing the fits would move every
    L/D and invalidate every golden for a model that is not obviously better):
    1. **Now (honesty):** return the clamp state, not just the value. Add
      `oswald_e_clamped: Optional[str]` (`"floor"|"ceiling"|None`) to `AeroEstimate`
      and an assumptions line saying e is at a bound and therefore insensitive.
      `sensitivity.py` must refuse to report a derivative through a clamped e.
    2. **Now (honesty):** document the AR 3.6–4.7 wrong-sign band and the AR=4.800
      crossing in the ledger, and add a documenting test that pins the crossing so it
      cannot move unnoticed.
    3. **Decision required (defect):** whether to replace the blend with a formulation
      valid below AR 4.8. **Recommend deferring** to a separate, sourced piece of work
      — flag it in the ledger as a known model limitation with its measured extent, not
      a quick edit.
  - Locks: `::test_clamped_oswald_is_reported`,
    `::test_sensitivity_refuses_derivatives_through_a_clamp`,
    `::test_oswald_fit_crossing_is_at_ar_4_800` (documenting),
    `::test_oswald_wrong_sign_band_is_documented`.
  - Done when: no consumer can treat a clamped e as a smooth function, and the
    wrong-sign band is measured in the suite rather than remembered.

- [x] **S6-F5 / F6 — `cl_max_3d` ignores Reynolds, thickness, and washout; and penalises forward sweep** · defect + capability · HIGH · cost L
  - Anchors: `aero_estimates.py:151-155`, `CL_MAX_2D = 1.30` at `:27`.
    Verified **bit-identical** `cl_max_3d = 1.120849286` across Re
    181,807 → 1,022,665, mean t/c 0.0639 → 0.1678, and washout 0 → 12°. Section Clmax is
    ~0.9 at Re 1.5e5 and ~1.45 at Re 1e6, so `v_stall_ms` is Reynolds-invariant and so
    is the **entire `launch_ok` gate** — the most consequential blind spot for a low-Re
    small UAV. Envelope stats: min 0.744, median 1.089, max 1.170 (= the hard cap), CoV
    8.65%, **46.1% of the envelope within 6% of the cap**. Separately, the effective c/4
    sweep goes **negative in 5.4%** of the envelope (min −31.03°) and `math.cos` is
    **even**, so a forward-swept quarter chord is penalised identically to aft sweep —
    Raymer's `cos(Lc/4)` knockdown is an **aft-sweep** correlation. Reproducing geometry
    (legal, passes every cross-check at AR 2.583): `span 0.8, root_chord 1.2` (chord
    1.5× span!), `cbf 0.12, mcf 0.35, taper 0.15, sweeps 15/5`.
  - Fix, in two clearly separated parts:
    - **Honesty (now, cheap):** state in the assumptions string and the field
      description that `cl_max_3d` is Reynolds-, thickness-, and washout-independent,
      and therefore that `v_stall_ms` and `launch_ok` are too. Clamp the sweep
      knockdown to aft sweep only (`cos(max(sweep, 0))`) and add an assumption note for
      forward-swept planforms — the even-function behaviour is unambiguously wrong and
      the fix is one line. Also flag the `root_chord 1.2 m on a 0.8 m span` geometry as a
      schema gap (the AR gate admits it) — **INFERRED**: a `root_chord_m <= span_m`
      cross-check would exclude it; verify it does not reject legitimate BWB centre
      bodies before adding.
    - **Capability (S7-CAP-4, deferred):** a Reynolds-dependent section Clmax needs a
      **sourced** correlation (Re-scaled Clmax from a NACA 4-series dataset, or
      NeuralFoil, which the repo already depends on — `pyproject.toml` pins
      `neuralfoil>=0.3`). Do not invent a scaling. Until then, keep the constant and
      keep the disclosure.
  - Locks: `::test_clmax_is_documented_as_reynolds_independent` (assumptions string),
    `::test_forward_sweep_is_not_penalised_like_aft_sweep` (the reproducing geometry),
    `::test_clmax_reynolds_dependence` marked `xfail(strict=True, reason="S7-CAP-4")`.
  - Done when: the even-cos bug is gone, the blind spot is disclosed in the panel the
    user sees, and the capability is an explicit xfail rather than an unknown.

- [x] **S6-F13 — `neutral_point_m` is area-weighted, biasing static margin optimistic on the one safety-critical number** · honesty · HIGH · cost S
  - Anchors: `aero_estimates.py:158-177`. It **passes** sign, positivity, and all three
    monotonic physics directions (verified: aft with more sweep, aft with less taper,
    `x_np > 0` in all 596,700 cases) — the implementation is sound. But it is a pure
    **area**-weighted planform centroid, ignoring that outer panels carry more lift per
    unit area than the low-aspect centre body, which biases `x_np` **forward** → static
    margin **optimistic** → the CG advice is **unconservative** on the one number that
    is safety-critical for a tailless aircraft. The docstring (`:160-161`) says VSPAERO
    supersedes it but **does not disclose the bias direction**. Also `x_np` lands outside
    `(0, root_chord)` in **6.8%** of the envelope (up to 2.48× the root chord,
    `x_np/MAC` up to 3.398) with no feasibility check on whether payload can physically
    sit there.
  - Fix: (a) state the bias **direction** in the docstring, the field description
    (`:70`), and the user-facing assumptions line (`:218`): "area-weighted planform
    centroid; ignores the higher lift-per-area of the outer panels, which biases x_np
    forward and the static margin **optimistic** — treat the CG recommendation as a
    lower bound on required nose-heaviness until VSPAERO confirms". (b) Flag
    `x_np` outside `(0, root_chord_m)` as a `cg_placement_infeasible` note.
  - Locks: `::test_np_docstring_discloses_the_bias_direction`,
    `::test_np_outside_the_root_chord_is_flagged` (the 6.8% envelope slice).
  - Done when: no CG recommendation is shown without its bias direction, and physically
    unplaceable CGs are flagged.

- [x] **S6-G1 — `easy_launch`'s unconditional 5.5 kg/m² floor makes it the worst-stalling design** · defect · HIGH · cost M
  - Anchors: `variants.py:130` — `wl = max(5.5, 0.8 * derived.wing_loading_kgm2)`. The
    floor is unconditional, so whenever baseline wing loading is below 6.875 kg/m²
    (**64.1%** of conditions: slow, light, or high-altitude) the floor **raises** loading
    instead of lowering it. Measured: easy_launch loading higher than baseline in
    **31.8%**; `v_stall` worse than baseline in **24.0%**; it has the **highest
    `v_stall` of all five in 21.6%** — the card titled "Easy launch / Lowest stall
    speed" is the hardest to launch. Worst case: mtow 0.8, alt 4000, v 10, span cap 1.2
    → easy_launch `v_stall` 8.20 vs baseline 5.40.
  - Fix: the floor exists to stop absurdly large wings; make it a *structural/
    practicality* bound that can never exceed the baseline: `wl = min(0.8 *
    derived.wing_loading_kgm2, max(5.5, 0.8 * derived.wing_loading_kgm2))` collapses to
    `0.8 * baseline` — i.e. **drop the floor** and instead bound the resulting **area**
    by the span cap and root-chord gates (which `_feasible_planform` already does). If a
    minimum practical loading is genuinely required, apply it only when it *lowers*
    loading, and otherwise state that this lens cannot improve on the baseline here
    (S3-G1h already provides the string).
  - Locks: `test_bwb_lens_intent.py::test_easy_launch_never_stalls_worse_than_baseline`
    (hypothesis over the condition grid; xfail-encoded in Stage 0),
    `::test_easy_launch_is_never_the_highest_stall_of_the_five`.
  - Done when: the two measured failure rates (24.0% and 21.6%) go to zero, or the
    remaining cases are explicitly reported as "this lens cannot help here".

- [x] **S6-G2 / G3 — `max_range` and `span_efficiency` do not own their objectives** · defect · MED · cost M
  - Anchors: `variants.py:152-192` (`max_range` sizing) and `:70-76` / `:136-138`
    (`span_efficiency`). Measured: `max_range` reaches CL* within 2% in only **16.2%**
    (mostly physics — an area floor binds in 2516/2880; in the 364 unconstrained
    conditions the fixed point is exact to 2e−5). A user picking "Max range" gets
    **lower cruise L/D than "Span efficiency" in 39.1%** of conditions
    (span_efficiency's AR 8.5 beats sizing-to-CL* at AR 6.5) — the lens **ranking is
    inverted** across a large slice of the envelope. `span_efficiency` does not own
    induced drag: `easy_launch` has lower induced drag in **57.3%**.
  - Fix: this is a lens-**design** question, not a bug in the arithmetic. Recommended
    scope here: (a) let `max_range` optimise AR as well as area when sizing to CL* (its
    fixed AR 6.5 is what loses to AR 8.5), or (b) rename/re-scope the lenses so their
    titles match what they actually optimise, and add an explicit *achieved-objective*
    metric per card so the gallery can order by the thing the user asked for. **Do (b)
    first** (it is honesty and it ships immediately, covered by S3-G2h/S3-G3h); treat
    (a) as a bounded experiment with the ranking test below as its acceptance criterion.
  - Locks: `::test_max_range_has_the_best_cruise_ld_among_the_lenses` (xfail-encoded,
    flips only if (a) is implemented), `::test_span_efficiency_has_the_lowest_induced_drag`
    (same).
  - Done when: either the two ranking properties hold, or each card's title and
    objective are provably about a metric on which it does win (asserted per card).

- [x] **S6-G5 — Make the five concepts actually distinct at tight span caps** · capability · MED · cost L
  - Anchors: the degeneracy measured in S3-G5; four cards collapse to one design at
    `span_cap = 1.2`.
  - Fix: when the sizing cluster collapses, the honest options are to (a) reduce the
    number of cards shown, or (b) differentiate on the dimensions still free at a fixed
    span and area (sweep, twist, thickness, **dihedral** — see S7-CAP-2). **Do (a) plus
    S3-G5's report first**; (b) is real design work with a real risk of producing
    differences the tier-0 physics cannot resolve (the 12-parameter distance is already
    ≥0.15 while the panels are within 2%).
  - Locks: `::test_gallery_never_shows_two_identical_sizings_as_two_choices`.
  - Done when: the gallery never presents duplicates as choices.

- [x] **S6-J1 — Solver robustness (`inverse.py` "never raises" contract) — PROBE COMPLETE** · defect · CRITICAL · cost S
  - **Method (reproducible):** deterministic 8-dimensional grid over the full legal
    `DesignRequirements` envelope, seed `20260725`, script
    `scratchpad/fuzz_solver.py`. Grid: `mtow_kg` {0.05, 0.2, 0.5, 1, 2, 5, 10, 25} ×
    `cruise_speed_ms` {8, 12, 18, 25, 40, 60} × `cruise_altitude_m` {0, 100, 1000, 2500,
    4000} × `launch` {hand, catapult, runway} × `span_max_m` {None, 0.8, 1.0, 1.5, 2.0,
    3.0} × `v_stall_max_ms` {None, 5, 8, 10.5, 15, 20} × `cl_cruise_target` {None,
    0.050001, 0.2, 0.5, 0.75} × `ld_min` {None, 5, 15, 25, 40} = **648,000 combinations,
    all legal, all solved**.
  - **The "never raises" contract HOLDS: 0 exceptions in 648,000 solves.** Pin it.
  - **Every structural invariant HOLDS with zero violations**: params re-validate
    against `BWBParams`; `span_m <= span_max` and within [0.8, 3.0]; AR within
    [2.5, 10]; root chord within [0.25, 1.2]; tip chord >= 0.04; planform area finite,
    positive, and matching the reported `s_ref_m2`; **no NaN/inf in any `AeroEstimate`
    float field**; `binding_constraint` is `None` iff every set target is met and
    otherwise a valid `BINDING_LABELS` key; `binding_explanation` non-empty whenever
    set; every `met` flag agrees with recomputation under `MET_TOLERANCES`. These are
    the Stage-0 invariant locks — they are real and worth defending.
  - **The ONLY violated invariant is the stall cap — and it is the core solver, not
    just the lenses.** `solve_requirements` returns a design whose `v_stall_ms` exceeds
    `min(v_stall_max_ms, LAUNCH_VSTALL_MAX[launch])` in **214,441 / 648,000 = 33.1% of
    the entire legal envelope.** This confirms S4-C1 is rooted in `inverse.py`, not only
    in `variants._size_wing`: the baseline candidate is `derived.params` straight from
    this solver, so it inherits the defect (this is also the mechanism behind S4-C2).
  - **Blast radius — it scales with mass, and it is worst exactly where users care:**

    | `mtow_kg` | violating | | `span_max_m` | violating | | `v_stall_max_ms` | violating |
    |---|---|---|---|---|---|---|---|
    | 0.05 | 0.0% | | 0.8 | 49.2% | | 5.0 | 51.7% |
    | 0.2 | 0.0% | | 1.0 | 42.6% | | 8.0 | 41.4% |
    | 0.5 | 5.1% | | 1.5 | 31.8% | | 10.5 | 31.0% |
    | 1.0 | 14.0% | | 2.0 | 29.1% | | 15.0 | 25.2% |
    | 2.0 | 27.8% | | 3.0 | 22.9% | | 20.0 | 24.6% |
    | 5.0 | 50.2% | | None | 22.9% | | None | 24.6% |
    | 10.0 | 71.8% | | | | | | |
    | 25.0 | **95.9%** | | | | | | |

    Roughly launch-independent (hand 36.2%, catapult 33.2%, runway 29.9%). Tighter user
    constraints make it *more* likely, which is the opposite of what a constraint should
    do. Minimal reproducer (smallest overshoot):
    `DesignRequirements(mtow_kg=0.5, cruise_speed_ms=8.0, cruise_altitude_m=0.0,
    launch="hand", span_max_m=0.8, v_stall_max_ms=5.0)` → stall 5.24 vs cap 5.00.
- [x] **S6-J2 — The launch-method stall cap is enforced NOWHERE in the verdict when the user has not set a stall row** · defect · CRITICAL · cost S
  - Anchors: `inverse._statuses` (`inverse.py:188-212`) emits a `v_stall_max_ms` status
    with `target=req.v_stall_max_ms`; when that is `None` the status carries
    `met=None`, so it can never be "unmet", so `solve_requirements` never sets a
    binding constraint for it (`:296-328`). The launch cap
    (`LAUNCH_VSTALL_MAX`) is applied as an **area floor** (`:247-248`) but is never
    **checked** afterwards. `intake.derive` (`intake.py`) builds the feasibility verdict
    from `res.binding_constraint` and `res.requirements` and never reads
    `panel.launch_ok`.
  - Now (measured, all four verified directly):

    | case | stall | cap | `status.met` | `binding_constraint` | verdict text |
    |---|---|---|---|---|---|
    | user stall 5.0, tiny overshoot | 5.24 | 5.00 | `False` | **`span_cap`** | "span cap 0.80 m binds — allowing more span would raise aspect ratio and L/D" |
    | user stall 5.0, 25 kg | 14.14 | 5.00 | `False` | `stall_requirement` | correct |
    | **no stall row, 25 kg hand** | **14.14** | **10.50** | **`None`** | **`None`** | **"all requirements met"** |
    | **no stall row, 20 kg, span 0.9** | **29.58** | **10.50** | **`None`** | **`None`** | **"all requirements met"** |

    A design needing a **29.6 m/s hand throw** is reported as *"all requirements met"*.
    The only signal anywhere is `panel.launch_ok = False`, which no Stage-1 surface
    reads. This is the single most dishonest output in the pipeline and it covers the
    24.6% `v_stall_max_ms=None` column above.
  - Also: in row 1 the binding constraint **misdiagnoses the cause** — it blames the
    span cap and talks about aspect ratio and L/D while the actual failure is the
    user's stall requirement breached by 0.24 m/s.
  - Fix: after the solve, compare `panel.v_stall_ms` against the *effective* cap
    `min(req.v_stall_max_ms or inf, LAUNCH_VSTALL_MAX[req.launch])` and set
    `binding_constraint = "stall_at_launch"` (already in `BINDING_LABELS`) when the
    launch cap is the breached one; make `_statuses` emit a `v_stall_max_ms` status
    whose target defaults to the launch cap rather than `None`. Pairs with S4-C1: fixing
    the floor-vs-repair ordering reduces the population, and this item makes whatever
    remains **visible** rather than silent.
  - Locks: `::test_launch_cap_breach_sets_a_binding_constraint`;
    `::test_verdict_is_never_all_requirements_met_when_launch_ok_is_false`;
    `::test_binding_constraint_names_the_actually_breached_cap` (row 1).
- [x] **S6-J3 — Loop-cap saturation: no silent wrong answers found, but one pathological timing outlier** · info · cost S
  - No case in 648,000 produced an un-repaired planform: every returned params object
    re-validates, so the 80-iteration repair loop (`inverse.py:151`) and the 40-step AR
    loop (`:286-294`) always converged to a schema-valid result within their caps.
  - Timing: mean **0.67 ms**, p99 **4.62 ms**, worst **118.87 ms** — the worst case is
    **177× the mean**. The solver is on the hot path of every feasibility re-derive
    (which fires on every row edit) and of every generate. Worth a timing regression
    guard, not a redesign.
  - Lock: `::test_solve_requirements_p99_latency_under_10ms` (marked `slow`).
- [x] **S6-J4 — Determinism holds; idempotence has a 0.7% non-convergence tail** · defect · LOW · cost S
  - Determinism: **0 failures in 300 seeded random cases** — the same input twice gives
    a byte-identical params dump. Pin it.
  - Idempotence: re-solving from the solver's *own* output
    (`solve_requirements(req, shape_from=result.params)`) fails to reach a fixed point
    in **2 / 300** cases (span or root chord moves by >1e-6 on the second pass). Small,
    but it means "morph the existing design" (the documented purpose of `shape_from`,
    `inverse.py:4-6`) can drift under repeated edits — exactly the Stage-1 usage
    pattern, where every row edit re-solves.
  - Lock: `::test_solve_requirements_is_idempotent` (hypothesis, ≥300 examples).
  - Locks (write these now, independent of the probe):
    `tests/test_bwb_inverse_properties.py::test_solve_requirements_never_raises`
    (hypothesis over `design_requirements()`, ≥1,000 examples);
    `::test_solve_requirements_is_deterministic` (same input twice → identical dump);
    `::test_solve_requirements_is_idempotent` (re-solving from its own output is a fixed
    point); `::test_binding_constraint_is_set_whenever_a_target_is_missed`;
    `::test_repair_loop_reports_saturation`.
  - Done when: the probe's findings are appended as S6-J2…J5 with anchors, and the five
    property tests above are green.

**Stage 6 exit:** no panel reports an infeasible cruise point without a flag; no input
produces a silently wrong panel; the CD0 bias, the contradictory sweep conventions, and
the even-cosine sweep knockdown are corrected with regenerated goldens and ledger
entries; the Oswald clamp and the `x_np` bias are disclosed; the lens intent
properties hold or the titles are corrected; the solver's never-raises contract is a
tested property.

---

## Stage 7 — Parity, deduplication, and the capability backlog

**Depends on:** Stage 0 (the node runner) and Stage 6 (parity goldens must be pinned
after the physics settles, or they will be regenerated twice).

**The JS geometry mirror is FAITHFUL — do not rewrite it.** `stage1.js` vs
`cockpit.js` station tables are **bit-identical** (all 6 fields × 19 stations, and 2,997
preview floats); planform area/MAC/AR agree with `schema.py` to **0.0 error** across all
five independent copies. The problem is duplication without enforcement, not
correctness.

**Files:**
- Create: `src/cockpit/static/planform.js`, `tests/js/planform_parity.mjs`,
  `tests/js/station_parity.mjs`
- Modify: `src/cockpit/static/{cockpit.js,stage1.js,aero_mirror.js}`,
  `src/cockpit/server.py`, `domain_packs/bwb/vsp_builder.py`

- [ ] **S7-I1 — Five copies of the planform algebra, two of the station algebra, zero parity tests** · defect (drift risk) · HIGH · cost M
  - Anchors — planform: `domain_packs/bwb/schema.py:62-88` (authoritative),
    `src/cockpit/server.py:228-249` (`_planform`),
    `src/cockpit/static/aero_mirror.js:18-41`,
    `src/cockpit/static/cockpit.js:86-95`,
    `src/cockpit/static/stage1.js:253-259` (partial). Station algebra:
    `cockpit.js:273-297`, `stage1.js:278-302`, plus `domain_packs/bwb/asb_aero.py:96-137`.
    `stage1.js:5-6` documents the duplication ("if the BWBParams schema changes, update
    both") but nothing enforces it; a normalized diff shows `stage1.js` is a **literal
    hand copy** of `cockpit.js`.
  - **Decision: deduplicate within each language, then parity-test the one remaining
    cross-language pair (see Open questions, Q2).**
    - JS: extract one `src/cockpit/static/planform.js` ES module exporting
      `chords/halfWidths/planformArea/meanAeroChord/aspectRatio/stations/sectionLoop`;
      `cockpit.js`, `stage1.js`, and `aero_mirror.js` import it. Three copies → one.
      This also **permanently kills S7-I4**.
    - Python: `server.py:_planform` (`:228-249`) becomes a thin wrapper that builds a
      `BWBParams` when possible and falls back to the raw-dict math **only** for
      out-of-range params (which is its documented reason to exist, `server.py:225-226`)
      — and even then it should call one shared helper, not a second implementation.
      `asb_aero.py:96-137` stays separate (it is an AeroSandbox adapter, not a copy of
      the preview) but gets a parity test against the station module.
    - Remaining boundary: Python `schema.py` ↔ JS `planform.js`. That pair cannot be
      deduplicated (two languages), so it is parity-tested — permanently.
  - Locks: `tests/js/planform_parity.mjs` and `tests/js/station_parity.mjs`, each
    reading a committed fixture of geometries + expected values generated from Python
    (same pattern as `tests/js/atmosphere_parity.mjs`, which already reads the Python
    CSV), run by S0-4's node runner. Plus
    `tests/test_js_parity.py::test_planform_fixture_matches_python` which regenerates
    the fixture from `schema.py` and asserts it is byte-identical to the committed one —
    so a Python change that is not mirrored fails on the Python side too.
  - Done when: one JS implementation, one Python implementation, and a bidirectional
    fixture test between them.

- [ ] **S7-I2 — `asb_aero.py` snaps t/c to integer percent, so the fast tier scores a different geometry than the preview shows** · honesty · MED · cost S
  - Anchors: `domain_packs/bwb/asb_aero.py:88-93` — `int(round(...))`, giving up to
    **7.7% relative thickness error** (0.060 vs 0.065 at one station). Documented as a
    deliberate simplification (and in the MDO plan) but **unbounded by any test**.
  - Fix: keep the simplification (NeuralFoil's NACA-4 parametrization is the reason),
    but bound it: a test asserting the maximum relative t/c error over the legal
    envelope stays below a stated tolerance, and an assumptions line in the ASB result
    stating the snap and its measured worst case.
  - Locks: `tests/test_bwb_asb_aero.py::test_thickness_snap_error_is_bounded`.
  - Done when: the snap error is a measured, asserted number rather than a comment.

- [ ] **S7-I3 — The OpenVSP span gate is 5% while the real bias is +0.22% to +1.10%** · defect · MED · cost M · **DEFERRED, gated on a measurement campaign**
  - Anchors: `domain_packs/bwb/vsp_builder.py:216` —
    `span_ok = abs(span_stl - p.span_m) / p.span_m < 0.05`. Real OpenVSP STL span runs
    **+0.22% to +1.10%** larger than the requested `span_m` (the `ROUND_END_CAP` tip);
    the 5% tolerance is far too loose to detect it, so a systematic bias goes unmeasured.
    STL projected area is within **−0.11% to +0.31%** of `schema.py` S_ref; the lofted
    surface's projected area is −0.30% (twist foreshortening).
  - Fix (and why it is deferred): tightening the gate to ~1.5% **will start failing
    builds that pass today** and it treats a known, explainable bias as an error.
    Correct sequence: (1) record the measured span delta in `GeometryReport` as a
    first-class field so the bias is tracked on every build; (2) accumulate deltas over
    a batch of real builds; (3) *then* either subtract the cap contribution before
    comparing, or tighten the tolerance to the measured band. **INFERRED:** setting the
    tip cap to a flat/none style, or comparing against the *projected* span, would
    remove the bias at the source — verify against OpenVSP before choosing.
  - Locks: `::test_geometry_report_records_the_span_delta` (no OpenVSP needed — assert
    the field exists and is populated from the bbox); the tightened tolerance test comes
    with step (3).
  - Done when: the delta is recorded on every build. Tightening the gate is a separate,
    later decision with data behind it.

- [ ] **S7-I4 — Latent shared bug: un-coerced `root_chord_m` in the station builder** · defect · LOW · cost S
  - Anchors: `src/cockpit/static/stage1.js:290` and `cockpit.js:284` —
    `chord: p.root_chord_m + (pf.cMid - p.root_chord_m) * f` does **not** numeric-coerce
    `p.root_chord_m`, although its neighbours do (`+p.root_twist_deg`,
    `+p.root_thickness_pct`). With string params this produces 7 corrupted centre-body
    chords like `"0.55-0.03666666666666667"`, while `planform()` (which coerces at
    `cockpit.js:87`) stays correct — the preview would silently deform with the numbers
    still reading fine. **Unreachable today** (the server sends JSON numbers;
    `cockpit.js:445` commits `+raw`), one value-passthrough away from live.
  - Fix: `+p.root_chord_m`. Subsumed by S7-I1's shared module (coerce once at the
    module boundary).
  - Locks: `tests/js/station_parity.mjs` gains a string-params case asserting identical
    output to the numeric case.
  - Done when: the station builder is type-agnostic at its boundary.

- [ ] **S7-I5 — Twist and sweep rely on undocumented OpenVSP defaults in three places** · defect (latent) · MED · cost S
  - Anchors: `domain_packs/bwb/vsp_builder.py:67-69` sets `Twist` but never
    `Twist_Location`. The probe empirically confirmed **quarter-chord** twist from four
    archived STLs (predicted vs measured `bbox_m[0]` matched to 1e−5) and confirmed
    OpenVSP `Span` is the **projected** span (so the JS `y=b`, `z=b*tan(dihedral)`
    convention is correct) — but **nothing pins either**. An OpenVSP version change
    would silently alter every built geometry.
  - Fix: set `Twist_Location` and `Sweep_Location` explicitly to the values the probe
    confirmed, and record both in `GeometryReport` provenance so a future change is
    visible in the manifest.
  - Locks: `verification/verify_gates.py` gains a row asserting the explicit locations
    are set (this needs OpenVSP, so it belongs in `verification/`, not `tests/` — match
    the existing style: `t.add("twist location pinned", ok, detail)`).
  - Done when: no geometry depends on an unstated OpenVSP default.
  - Note **I6 is by design, not a defect**: JS interpolates linearly between stations,
    OpenVSP lofts a spline in U, AeroSandbox interpolates between 3 xsecs; station
    counts differ three ways (JS N1=6/N2=12 → 19 stations / 999 verts / 1922 tris with a
    flat tip cap; `vsp_builder` `Tess_W=41` with ~30 U stations and a round cap → 4784
    facets; `asb_aero` 3 xsecs). Bounded by the ≤0.31% measured area difference.
    **Document the bound; do not unify the discretizations.**

### Capability backlog (explicitly out of scope for this plan)

These are missing capabilities, not defects. They are listed so the honesty items above
can point at them, and so nobody mistakes a disclosure for a fix.

- [ ] **S7-CAP-1 — Endurance / battery / propulsion modelling.** The `endurance` row is
  parsed and stored but drives nothing (`intake.py:272-276`, `schema.py:22-25`).
  Out of scope per CLAUDE.md ("keep the scope aerodynamics-first"); `endurance_fom`
  (`aero_estimates.py:61`) is the aerodynamic proxy that exists. Disclosed by S3-E2.
- [ ] **S7-CAP-2 — Dihedral freedom.** Dihedral is **2.0° in all five generated
  concepts** (verified) because every `_Lens` inherits the default at
  `variants.py:58`; the gull-wing / dihedral-distribution shape that motivated the whole
  design is unreachable in Stage 1. A per-lens dihedral is a one-line change; a
  *distribution* needs the 16-parameter station schema already planned in the MDO
  design spec's future phases.
- [ ] **S7-CAP-3 — Locks that actually constrain the solver.** Requires deciding what
  "fixed" means for each of the twelve rows (a hard equality? a frozen search
  dimension, as `design_space.DesignSpace` already models for MDO?). Disclosed by
  S3-E1; test placeholder in S0-5. Reusing `DesignSpace`'s `frozen` concept is the
  obvious route.
- [ ] **S7-CAP-4 — Reynolds-dependent section Clmax.** Needs a sourced correlation
  (NACA 4-series Re-scaled dataset, or NeuralFoil, already a dependency). Do not invent
  a scaling. Disclosed by S6-F5; xfail placeholder there.
- [ ] **S7-CAP-5 — Custom rows as real constraints.** `intake.py:326-333` records them
  and promises Stage-2 constraint handling "when recognised"; nothing consumes them.
  Either implement a recognised-key registry or narrow the promise (S3-E2/E3).

**Stage 7 exit:** one planform implementation per language with a bidirectional fixture
test; the ASB thickness snap and the OpenVSP discretization differences are bounded by
asserted numbers; OpenVSP's twist/sweep conventions are explicit; the capability
backlog is written down and pointed at by the disclosures that stand in for it.

---

## Test strategy

Six layers. Each answers a different question, and no layer substitutes for another.
**Line coverage answers none of them** — see "What NOT to do".

### 1. Characterization / golden layer — "did this physics edit change anything?"

- `tests/data/aero_golden.json` + `tests/test_bwb_aero_golden.py`: the full 22-field
  reference panel at `rel=1e-12` (S0-7), plus component goldens for `mean_tc`, both
  effective sweeps, `skin_friction_coeff` over the Re ladder that exposes the `max(re,
  1e4)` floor, `form_factor`, `oswald_efficiency` over an (AR × sweep) grid including
  both clamp plateaus and the AR=4.800 crossing, `cl_max_3d`, and `neutral_point_m`
  (S0-8).
- `verification/verify_aero_goldens.py` in the house style (`_common.Table`) with
  `--update`, so a physics change produces a reviewable table and a reviewable diff.
- **Golden-update policy:** `--update` may only be run alongside a ledger entry naming
  the physical reason and the source. A golden diff without a ledger entry is a rejected
  change. This is the mechanism that makes S6-F2's ~10% L/D shift a decision rather than
  an accident.

### 2. Invariant layer — "what must remain true no matter what?"

The five invariants measured with **zero violations and zero NaN** across 117,600 cases
(S0-9): `ld_cruise <= ld_max`; `cl_star <= cl_max_3d`;
`endurance_fom == cl^1.5/cd`; `x_cg_for_target_sm_m == x_np - 0.08*mac`; `x_np > 0`.
Locked in `tests/test_bwb_aero_invariants.py` and, after S6-AE1, in `AeroEstimate`'s
model validator as well. Plus the invariants added by fixes: `c4_sweep <= le_sweep`
(S6-F4), `cruise_feasible == (v >= v_stall)` (S6-F1), and "every candidate satisfies its
caps or carries a violation" (S4-C1).

### 3. Property / fuzz layer — "does it hold across the whole envelope?"

`hypothesis==6.161.2` is installed and pinned (`environment.yml:372`). Strategies live
in `tests/strategies.py` (S0-10). Targets:

- `inverse.solve_requirements` never raises, is deterministic, and is idempotent
  (S6-J1) — this is the module's **documented contract**, currently untested.
- Every candidate satisfies its effective caps or carries a violation (S4-C1).
- `_num`/the unit parser never returns `inf`/`NaN` and never raises (S2-D1).
- `estimate()` rejects every non-finite or non-positive flight condition (S6-F9).
- `effective_c4_sweep_deg <= effective_le_sweep_deg` (S6-F4).
- `apply_edits` survives arbitrary JSON payloads (S2-D3).

Keep `max_examples` modest (100–200) in the default run and add a `slow`-marked profile
for the ≥1,000-example sweeps.

### 4. API layer — "what does a request actually do?" (no WSL-external services, no OpenVSP)

`fastapi.testclient.TestClient` against `cockpit.server.app` with `ASTRA_SESSIONS_DIR`
pointed at a temp dir — the proven pattern at `tests/test_requirements_api.py:17-27`,
hoisted into `tests/conftest.py` (S0-11). Covers: security (S1-A1/A2/A3), robustness
(S1-B3/B4, S2-B1/B5), concurrency via `ThreadPoolExecutor` against the same client
(S1-B2), cross-endpoint state (S2-D5/D6), staleness (S4-C3), and lifecycle transitions
(Stage 5, with OpenVSP monkeypatched — the existing tests already stub it).
**WSL-only** because importing `cockpit.server` loads FAISS (~30 s), so keep the API
tests in few modules and mark them `@pytest.mark.wsl`.

### 5. Parity layer — "do the copies agree?"

`tests/test_js_parity.py` globs and runs `tests/js/*.mjs` under `node` (S0-4), which
finally executes the orphaned `atmosphere_parity.mjs` and picks up
`planform_parity.mjs` / `station_parity.mjs` (S7-I1) automatically. The Python side
regenerates each fixture from `schema.py` and asserts byte-identity, so drift fails from
either direction. `asb_aero.py`'s station adapter and its t/c snap get bounded-error
tests (S7-I2).

### 6. xfail-as-backlog — "the suite *is* the todo list"

Write the **desired-behaviour** test now; mark it `@pytest.mark.xfail(strict=True,
reason="<item ID>: <one line>")`. With `xfail_strict = true` (S0-1), a fix flips exactly
one marker from xfail to pass, and an accidental fix fails the build until the marker is
removed — so the backlog cannot silently drift from reality.
`test_suite_integrity.py::test_every_xfail_names_a_plan_item` (S0-12) asserts every
reason string starts with an ID that appears in this document.

**Findings to xfail-encode in Stage 0** (each flips in the stage named):

| xfail test | Flips in |
| --- | --- |
| 11 of the 13 cap scenarios; every candidate under its effective stall cap | S4-C1 |
| baseline respects span and stall caps | S4-C2 |
| a requirements change invalidates the batch | S4-C3 |
| an 11-hour endurance brief still generates | S2-B1 |
| hostile `session_id`s rejected; mutating POSTs require a session | S1-A1/A2 |
| 12 parallel intake saves lose nothing | S1-B2 |
| the 12 number-parsing cases (sign, `1,500`, ft/in/mm, `1e400`, junk) | S2-D1 |
| the probed brief yields span 2.2 m and altitude 300 m | S2-D4 |
| parse preserves edits and custom rows | S2-D5 |
| generate does not clobber explicit requirements | S2-D6 |
| every clamp emits a warning naming typed and used values | S3-D2 |
| `cruise_feasible` exists and equals `v >= v_stall` | S6-F1 |
| `estimate()` rejects `v_ms ∈ {0,-5,nan,inf}` and unknown launch strings | S6-F9/F10 |
| `c4_sweep <= le_sweep` over the legal envelope | S6-F4 |
| the four corrected `skin_friction_coeff` values | S6-F2 |
| `easy_launch` never stalls worse than baseline | S6-G1 |
| the `max_range` retraction is present whenever CL* is missed | S3-G2h |
| every illegal candidate transition is refused | S5-H1/H2/H3 |
| a failed promote leaves no version directory | S5-H7 |
| Reynolds-dependent CLmax; locks constrain the solver | S7-CAP-4 / CAP-3 (capability — may stay xfail indefinitely, which is the honest state) |

---

## Open questions — decided

### Q1. How do JS tests get executed?

**Decision: pytest shells out to `node`.** `tests/test_js_parity.py` globs
`tests/js/*.mjs`, parametrizes over the glob, and runs each file with
`subprocess.run([node, path], cwd=repo_root)`, skipping when
`shutil.which("node") is None`.

**Reasoning.** (a) One command runs everything — `pytest tests/ -q` is already the
project's only test command, and the orphan existed precisely because a second command
would have had to be remembered. (b) No new toolchain: the repo is conda + pip with no
`package.json`, no `node_modules`, and no npm scripts; introducing npm to run four
assertions is more surface than the tests are worth. Node is used only as an ES-module
interpreter, exactly as `atmosphere_parity.mjs` already assumes. (c) It is CI-visible
and skip-visible, and S0-3 makes the skip loud. (d) Globbing plus
`test_every_js_file_is_collected` structurally prevents the next orphan — which is the
actual failure being fixed, not "the parity file does not run".
**Rejected:** a `verification/verify_js_parity.py` script — `verification/` is for
checks that need OpenFOAM, OpenVSP, or LLM credentials and therefore cannot live in the
suite; JS parity needs none of those and belongs in the suite. **Rejected:** an npm
script — a second entry point nobody runs is how we got here.

### Q2. Deduplicate the planform/station algebra, or merely parity-test it?

**Decision: both, in that order — deduplicate *within* each language, then parity-test
the one irreducible cross-language pair.** Three JS copies collapse into
`src/cockpit/static/planform.js`; `server.py:_planform` becomes a wrapper over the
`schema.py` math (keeping only its documented raw-dict fallback for out-of-range
params). That leaves exactly two implementations — Python `schema.py` and JS
`planform.js` — bridged by a committed fixture asserted from both sides.

**Reasoning.** Five copies means ten pairwise drift risks and one hand-maintained
comment (`stage1.js:5-6`) as the only defence. Deduplication removes eight of them at
near-zero cost (the copies are already bit-identical, so the refactor is provably
behaviour-preserving — the parity fixtures from S7-I1 are written *first* and must stay
green through the extraction). It also permanently eliminates S7-I4, which is a latent
bug that exists *only* because of the copy. A cross-language parity test is
irreducible — two runtimes must each have the math — so that one pair is tested forever,
not deduplicated. **Do not touch the algorithm**: the probe verdict is that the mirror
is faithful (bit-identical station tables, 0.0 planform error), so this is a pure
refactor with a test harness, not a rewrite. `asb_aero.py:96-137` stays separate: it is
an AeroSandbox adapter with a different discretization by design (I6), and it gets a
bounded-difference test rather than being merged.

### Q3. Should the two credential-gated test files be skipif-guarded?

**Decision: yes — skipif-guard them, and make the skip loud.** Module-level
`pytest.importorskip` for the credential-bound imports plus
`pytestmark = [pytest.mark.credentials, pytest.mark.skipif(not _have_credentials(), ...)]`,
and `tests/test_suite_integrity.py` asserting zero collection errors, a collected-test
floor, and that every skipped module carries a declared marker.

**Reasoning.** The current state is the worst of the three options: ~14 flow-contract
tests are hidden from every credential-less run with no signal at all, which means every
coverage number and every "119 pass" claim in this repo is measured against an unknown
denominator. Deleting them loses real coverage of the OpenFOAM flow contract. An
unconditional xfail would lie about why. A named skip is honest, keeps the tests
runnable where credentials exist, and — combined with the suite-integrity test — makes
the absence visible. **Additional recommendation (INFERRED, worth doing but not
required here):** the deeper problem is that `services.flow_conditions` transitively
imports `src/config.py`'s credential requirement even though the flow-contract logic
itself is deterministic. Making that import lazy would let ~14 tests run
unconditionally, which is strictly better than skipping them. Scope it separately;
verify the import chain first.

### Q4. Should `AeroEstimate` get a validator mirroring `AtmosphereState`?

**Decision: yes, with a narrower scope, and sequenced after S6-F9.** Add a
`model_validator` enforcing (a) finiteness of every float, (b) positivity of the
physically positive quantities (`s_ref_m2`, `aspect_ratio`, `span_m`, `mac_m`, `v_ms`,
`rho_kgm3`, `weight_n`, `cd0`, `cd_cruise`), (c) `0 < oswald_e <= 1`, and (d) the five
already-holding invariants as cross-checks.

**Reasoning.** The inconsistency is indefensible on its own terms: `AtmosphereState`
(`atmosphere.py:53-117`) in the *same domain pack* enforces finiteness, positivity, and
cross-consistency, while `AeroEstimate` accepts `oswald_e=99` and `ld_max=-99`. And
`AeroEstimate` is the object the user's numbers come from. Three scoping caveats drive
the "narrower" and the sequencing:
1. **Do not include `cl_cruise <= cl_max_3d`.** That condition is violated by 22.31% of
   the legal envelope (S6-F1); it is a *flag* (`cruise_feasible`), not a rejection.
   Rejecting it would make a fifth of the design space unrepresentable.
2. **Land it after S6-F9's input guards.** Today's NaN/inf panels come from unvalidated
   inputs. Adding an output validator first converts silent-NaN paths into
   `ValidationError`s inside `variants.generate_candidates`, `sensitivity.py`'s
   finite-difference loop, and the MDO objective — i.e. it would turn a silent wrong
   answer into a 500 before the cause is fixed. Fix the producer, then assert the
   product.
3. **Expect fallout in `sensitivity.py`.** Its finite-difference perturbations can
   legitimately probe near-boundary geometries; the validator must not make
   sensitivity analysis raise. Run the full suite plus
   `verification/verify_asb_aero.py` before merging.

---

## Effort and sequencing summary

| Stage | Items | defect / honesty / capability / infra | Est. effort | Gates |
| --- | --- | --- | --- | --- |
| 0 · Make the suite honest | 12 | 2 / 1 / 0 / 9 | 3–4 d | everything |
| 1 · Store integrity + boundary | 7 | 7 / 0 / 0 / 0 | 2–3 d | Stages 2–4 |
| 2 · Kill the 500s + honest parsing | 8 | 8 / 0 / 0 / 0 | 3–4 d | — |
| 3 · Honesty pass | 12 | 1 / 11 / 0 / 0 | 3–4 d | — (ship early) |
| 4 · Constraint adherence | 5 | 4 / 1 / 0 / 0 | 3–5 d | flips 11 xfails |
| 5 · Candidate lifecycle | 11 | 10 / 1 / 0 / 0 | 4–5 d | promotion safety |
| 6 · Physics + lens + solver | 17 (J complete) | 9 / 4 / 1 / 3 done | 6–10 d | needs Stage-0 goldens |
| 7 · Parity + dedupe + backlog | 6 + 5 backlog | 4 / 1 / 5 / 1 | 3–4 d | after Stage 6 |

**Total: ~78 tracked items, ~29–39 developer-days**, excluding the capability backlog.
The solver fuzz (S6-J1…J4) added one CRITICAL item (S6-J2, the unreported launch-cap
breach) and one LOW item (S6-J4, idempotence); its other two outcomes were *confirmations
that the code is sound* and become regression locks rather than work.

**If only three days are available**, do this and nothing else, in order:
1. **S0-2 + S0-3 + S0-6** — stop measuring against a suite that hides 14 tests and
   certifies a broken cap check. Half a day.
2. **S1-A2 + S1-B2** — a mutating POST that defaults to the newest session, on top of an
   unlocked read-modify-write, is silent data loss in normal single-user use. One day.
3. **S2-B1 + S4-C4 + S3-G1h** — one English phrase permanently kills Generate; concepts
   breach the user's own caps with `passed: True`; a card states a comparison its own
   numbers contradict. One and a half days, and all three are small.

**Natural parallelisation:** Stage 3 (strings and flags in `domain_packs/bwb`) and
Stage 5 (`src/services/candidates.py`) touch disjoint files and can run concurrently
once Stage 1 has landed. Stage 6 must be serial with itself — each physics item needs a
reviewed golden diff, and two concurrent diffs are unreviewable.

---

## Risks

- **S6-F2 moves every L/D by 7–12%.** This is the single largest behavioural change in
  the plan. It invalidates every golden, every screenshot, every number in
  `reports/`, and any threshold calibrated against the old values —
  `defaults.MAX_CRUISE_CL`, the MDO reference score in the Slice-1/2 notes, and
  `verification/verify_asb_aero.py`'s L/D ±25% tolerance. Mitigation: land it alone,
  with a regenerated golden table and a ledger entry, and re-run
  `verify_asb_aero.py` and the MDO tests in the same change.
- **Adding `cruise_feasible` (S6-F1) changes candidate ranking.** 22.31% of the envelope
  currently has infeasible cruise points competing as equals on `ld_cruise`
  (`variants.py:205`). Once they are excluded or demoted, the winning card changes for a
  large slice of missions — including, potentially, in demos and saved sessions.
  Mitigation: report the flag first (Stage 3-style disclosure), change the ranking in a
  separate, announced step.
- **Tightening the OpenVSP span gate from 5% to ~1.5% (S7-I3) will fail builds that
  pass today.** The bias is real and explainable (`ROUND_END_CAP`), so a tighter
  tolerance converts a known-good geometry into a rejection. This is why S7-I3 is
  deferred behind a measurement campaign and only records the delta for now.
- **`validate_assignment=True` (S6-F11) can break in-place mutation somewhere.** Any
  code path that assigns to a `BWBParams` field and relies on skipped validation will
  start raising. Mitigation: grep for attribute assignment on params objects before
  merging, and run the MDO/sensitivity/cockpit paths, which are the likely mutators.
- **`AeroEstimate`'s validator (S6-AE1) can turn silent wrong answers into 500s.**
  Mitigated by sequencing it after S6-F9 and by excluding the 22%-violated
  `cl_cruise <= cl_max_3d` condition. `sensitivity.py` is the likeliest casualty.
- **Session-id validation (S1-A1) breaks any saved bookmark, script, or session created
  outside the 8-hex-char convention.** Mitigation: `create_session` has always used
  `uuid4().hex[:8]` (`design_session.py:56`), so real sessions comply; still, scan
  `runs/design_sessions/` for non-conforming directory names before merging.
- **Stage 4's cap enforcement can make previously "successful" generates report
  violations or infeasibility.** That is the point — but it will look like a regression
  to anyone comparing against today's output. Ship it with the S4-C4 disclosure so the
  new state is legible.
- **Atomic writes plus per-session locking (S1-B2) add a failure mode of their own:** a
  stale lock file. Use a timeout with a clear error rather than blocking forever, and
  never leave a lock behind on an exception path.
- **The Stage-3 honesty pass adds response fields** (`warnings`, `used_value`,
  `drives_computation`, `violations`, `distinct_sizings`). `stage1.js` and any MCP
  consumer must tolerate them; add them as optional with defaults so older clients do
  not break.
- **The solver-fuzz probe (S6-J1…J4) is complete and moved the stall-cap defect's
  severity up.** It is not a lens bug that S4-C1 patches at the `variants.py` layer —
  `solve_requirements` itself violates the effective stall cap in **33.1% of the legal
  envelope** (95.9% at 25 kg), so the fix must land in `inverse.py` and S4-C1 must be
  re-verified against the core solver, not only against generated candidates. Expect
  S4's "flips 11 xfails" to under-count: the same fix changes baseline geometry
  everywhere and will move goldens pinned in Stage 0.

---

## What NOT to do

- **Do not rewrite the JS geometry mirror.** It is faithful: `stage1.js` vs
  `cockpit.js` station tables are bit-identical across all 6 fields × 19 stations and
  2,997 preview floats, and planform area/MAC/AR agree with `schema.py` to 0.0 error
  across all five copies. The work is deduplication plus parity tests (S7-I1), not
  reimplementation.
- **Do not "fix" the five invariants that already hold.** `ld_cruise <= ld_max`,
  `cl_star <= cl_max_3d`, `endurance_fom == cl^1.5/cd`,
  `x_cg_for_target_sm_m == x_np - 0.08*mac`, `x_np > 0` — zero violations and zero NaN
  across 117,600 cases. Lock them; leave them alone.
- **Do not treat high line coverage as evidence of correctness.** `aero_estimates` is at
  99% and every defect in section F lives inside those executed lines. The modules are
  *executed but unasserted*. Measure assertion density on values and invariants.
- **Do not chase the 0%-coverage modules.** `vsp_builder`, `vsp_aero`, and
  `gmsh_farfield` need OpenVSP; they belong to `verification/`, not to this plan.
- **Do not replace the Raymer Oswald fits on the way past.** The transcriptions are
  correct; the problems are clamping behaviour and a wrong-sign band below AR 4.8.
  Disclose and document them (S6-F3); a replacement is separate, sourced work.
- **Do not change `root_bending_index_nm`'s formula.** `W*b/8` is a defensible
  conservative comparative index and every existing comparison rests on it. Fix the
  label (S3-F7).
- **Do not guard against a lens/MDO key collision.** It is impossible — the keys are
  prefix-separated (`candidates.py:327`).
- **Do not turn the requirements store into a pipeline stage.** `design_session.py:182-187`
  deliberately keeps it out of `STAGES`, and that is correct. Detect staleness at read
  time (S4-C3).
- **Do not unify the three discretizations** (JS linear stations, OpenVSP U-spline,
  AeroSandbox 3 xsecs). They differ by design and are bounded by ≤0.31% projected-area
  difference. Document the bound.
- **Do not add `cl_cruise <= cl_max_3d` to `AeroEstimate`'s validator.** 22.31% of the
  legal envelope violates it; it is a flag, not a rejection.
- **Do not let a golden update ship without a ledger entry.** That rule is the only
  thing standing between a deliberate physics improvement and an accidental one.
