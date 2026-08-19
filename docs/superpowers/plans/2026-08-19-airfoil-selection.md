# Airfoil Selection Gate Implementation Plan

> **For agentic workers:** Execute this plan with `superpowers:executing-plans` and test-driven development. Stop at each stated verification checkpoint if the evidence does not match the acceptance criterion.

**Goal:** Insert a deterministic, reviewable Airfoil sets approval gate between Requirements and Concepts so one approved root/mid/tip section schedule is applied unchanged to every Stage 1 concept and carried into Stage 2.

**Architecture:** A pure `domain_packs/bwb` selector owns engineering calculations and ranking. A `src/services` store owns immutable, versioned recommendations and approvals. A dedicated FastAPI router and MCP tools expose that service. Candidate generation consumes an explicit current approval. A self-contained Stage 1 UI module owns the new screen and emits a documented navigation event without reaching into Findings.

**Technology:** Python 3.12 domain code, Python 3.10 OpenVSP subprocesses, FastAPI/Pydantic, pytest, browser-native JavaScript/CSS, Three.js, gzip JSON coordinate data.

**Approved specification:** This document is the complete approved contract for the Airfoil Selection Gate. Values labelled `[D]` are explicit design judgements, not measured facts; their required sensitivity tests and falsifiers are stated below.

## 1. User-visible workflow

1. The Stage 1 path is `Requirements ✓ → Airfoil sets → Concepts`.
2. Airfoil recommendation produces exactly three strategy cards: **Balanced**, **Efficiency**, and **Low-speed/control**.
3. Every card shows one coordinated root/mid/tip schedule. A schedule may repeat a section at multiple stations, but the three strategy schedules must be distinct.
4. The mission badge is deterministic:
   - Balanced is the default.
   - Low-speed/control is preferred when a launch or stall constraint binds.
   - Efficiency is preferred only when the brief explicitly asks for range, endurance, loiter, flight time, or power efficiency.
5. Selecting a card creates an immutable approval. One **Continue** action advances to Concepts. Repeating the same approval is idempotent.
6. The exact approved root/mid/tip names persist across the baseline and every concept lens; there is no station replacement or library-search interaction.
7. A station click opens a modal with the exact coordinate outline, a prestall lift preview plus a separate `CLmax` indication, a `CD`-versus-`CL` ladder, Reynolds number, thickness, pitching moment, confidence/validity, and evidence/reference states.
8. Reload and browser Back restore the current screen and approval. If intake, requirements, an external plan, or the approval changes, prior cards remain visible under a stale banner, Continue is disabled, and recompute is offered.
9. The Requirements airfoil row reads **Chosen in Airfoil sets** after approval.
10. The Three.js preview lofts the selected coordinate archive; it does not synthesize a generic four-series shape.

## 2. Engineering model and evidence contract

### 2.1 Station state

The domain selector derives root, mid, and tip operating states from the current sized plan. For each station it records local chord, twist, cruise and low-speed Reynolds number, cruise and low-speed sectional lift demand, thickness ratio, pitching moment, and usable polar bounds. Reynolds number follows the definition `Re = rho V c / mu`: inertia scales with local chord while viscous resistance scales with dynamic viscosity. Section lift demand is obtained from the existing spanwise load model; it is not invented independently for the selector.

The selector reuses the existing whole-wing profile-drag, stall, trim, stability, sizing, and geometry models. It must not introduce a second aerodynamic truth. Every derived quantity records its input/model provenance and evidence grade.

### 2.2 Candidate gates

A station candidate is ineligible when any of these is true:

- its polar row is missing;
- its exact coordinate row is missing or fails its recorded hash;
- the requested Reynolds number or lift demand is clamped or outside the table envelope;
- it cannot satisfy the local thickness demand;
- it cannot participate in an admissible root-to-tip thickness progression;
- the resulting schedule fails geometry validation, reachable trim, static-stability, or whole-wing stall checks.

Missing experimental references are not an eligibility failure. They increase the evidence-weakness objective and are displayed plainly.

### 2.3 Search, Pareto filtering, and strategies

Each strategy retains six station candidates `[D]`, so the coordinated search evaluates at most `6^3 = 216` root/mid/tip schedules per pass. The bound keeps an interactive exhaustive search while still admitting materially different sections. A 10-candidate sensitivity fixture must preserve the selected strategy ordering/schedule or expose a sensitivity warning; a schedule change without warning falsifies the six-candidate judgement.

For every admissible combination, record:

- whole-wing profile drag;
- stall/control margin;
- reachable-trim residual and stability state;
- evidence weakness;
- all gate failures and rejection reasons.

Remove dominated schedules with a four-objective Pareto filter. A schedule is dominated only when another is no worse in every objective and strictly better in at least one.

Rank the Pareto set as follows:

- **Balanced:** minimize the maximum normalized regret across profile drag, stall/control, trim, and evidence weakness, with deterministic tie-breaks.
- **Efficiency:** drag-first lexicographic ranking, followed by stall/control, trim, evidence, and schedule name.
- **Low-speed/control:** stall/control-first lexicographic ranking, followed by trim, drag, evidence, and schedule name.

If two strategies choose the same schedule, the later strategy takes the next ranked unique admissible schedule. If three distinct admissible schedules do not exist, recommendation fails closed with a conflict rather than fabricating diversity.

### 2.4 Coupled resizing and sensitivity

For each strategy, apply the proposed schedule, solve only reachable trim twist, resize using the existing deterministic sizing path, and recompute station state. Stop when the schedule hash is stable or after five passes `[D]`. Five passes bounds UI latency and detects a selector/sizer oscillation; an eight-pass fixture must reach the same stable hash or record non-convergence. A changed hash after pass five that disappears only by pass eight falsifies the production pass bound.

Close each strategy decision with leave-one-metric-out sensitivity: rerank after omitting drag, stall/control, trim, and evidence in turn. Record whether the winner changes and the alternate schedule. Sensitivity is decision evidence and never silently overrides the approved deterministic rank.

## 3. Coordinate and reference evidence

1. Generate and commit a gzip-compressed, standard-library-readable coordinate archive from the same AeroSandbox airfoil library and version used by `airfoils_neuralfoil.json`.
2. The archive contains exactly the polar-table section names, normalized upper/lower coordinate arrays, per-section SHA-256 hashes, archive SHA-256, source attribution, generator version, AeroSandbox version, and NeuralFoil/table linkage.
3. `verification/generate_airfoil_table.py` generates or verifies both artifacts and refuses library/version/name/hash drift. Runtime readers use only the Python standard library.
4. Polar/coordinate parity is a release gate: name sets must be equal, every coordinate hash must verify, and the archive and polar provenance must identify the same AeroSandbox version.
5. The exact reference ledger includes fixture entries for `e472`, `e474`, and `s5010`. The UIUC Low-Speed Airfoil Data volumes are primary experimental evidence where a section is present. No experimental source is invented for `e474`; its missing reference weakens evidence only.
6. OpenVSP root/mid/tip sections use `ChangeXSecShape(..., XS_FILE_AIRFOIL)` and `ReadFileAirfoil` with exact archive-generated coordinate files. Generic `ThickChord` rescaling is prohibited for selected sections. Geometry reports and handoff record section names, hashes, and `XS_FILE_AIRFOIL` types.

## 4. Immutable storage and staleness

Recommendation artifacts live at:

```text
sessions/<id>/airfoil_recommendations/vN/recommendations.json
```

Approval artifacts live at:

```text
sessions/<id>/airfoil_selections/vN/selection.json
```

Both use atomic replace under the session lock and an index pointing at the current version. They include schema version, created time, intake version/hash, requirements version/hash, plan version/hash, polar table hash, coordinate archive hash, algorithm/ranking provenance, all considered/rejected schedules with reasons, sensitivity, and content hash.

Selection additionally contains recommendation version/hash, selected strategy, exact root/mid/tip names and coordinate hashes, selection version/hash, and idempotency identity. Approval writes the chosen converged `MissionDerived` geometry as the next immutable plan version and records both `source_plan_version/hash` and `plan_version_created/hash`. Re-selecting the same current recommendation and schedule returns the existing approval and does not create another plan. A different approval creates the next selection and plan versions.

An artifact is stale when any recorded intake, requirements, plan, recommendation, selection, polar table, or coordinate archive identity differs from current state. The plan created by an approval is explicitly linked as that recommendation's output, so it does not make its own cards stale; any later external plan does. Intake, requirements, external plan, and selection writes mark dependent candidate batches stale. Previous artifacts remain readable for audit. A replacement Airfoil Sets selection keeps the prior candidate gallery visible but stale for comparison; unrelated plan changes retain the existing behavior of hiding the orphaned batch.

HTTP semantics:

- `404`: session or immutable artifact does not exist;
- `409`: prerequisite missing, artifact stale, competing state transition, or Concepts requested without a current approval;
- `422`: request body or domain value is malformed/invalid.

## 5. API and propagation contract

A dedicated router owns:

```text
POST /api/stage1/airfoils/recommend
GET  /api/stage1/airfoils
GET  /api/stage1/airfoils/detail
POST /api/stage1/airfoils/select
```

The router also gates `POST /api/stage1/generate`: a missing or stale approval returns `409`; a current approval is passed explicitly into candidate generation. The router is registered before the legacy route so there is one effective behavior.

MCP exposes `recommend_airfoil_sets` and `select_airfoil_set`. Public geometry overrides accept string-valued named airfoil sections as well as numeric parameters.

Candidate generation has an explicit approval argument. It applies the exact approved schedule to the baseline and every lens after all lens overrides. Candidate index, promotion manifest, restoration state, and Stage 2 handoff carry selection version/hash. Geometry and handoff reports carry the names, coordinate hashes, and OpenVSP section types.

## 6. UI isolation and navigation contract

Implementation lives in `src/cockpit/static/stage1-airfoils.js` and `stage1-airfoils.css`. Shared shell changes are limited to:

- `stage1.html`: stylesheet import and Airfoil screen/mount/modal containers;
- `stage1.js`: module import, screen registration, Requirements/Airfoils/Concepts navigation, and consumption of the dedicated exact-coordinate section provider;
- `src/cockpit/server.py`: router import and registration only.

No Findings element, module, route, DOM ID, or transition logic is edited or renamed. `stage1.css` remains unchanged unless a demonstrated incompatibility makes that impossible.

On Continue, the module dispatches:

```js
window.dispatchEvent(new CustomEvent("astra:stage1-airfoil-continued", {
  detail: { sessionId, selectionVersion, selectionHash, scheduleHash, schedule }
}));
```

It also supports an optional `onContinue(detail)` callback passed to its initializer. Findings may consume either interface; the Airfoil module never queries or mutates Findings DOM.

## 7. Test-first execution plan

### Task 1 — Record the decision before the fix

- Save this specification.
- Reproduce the existing OpenVSP mismatch against the reference geometry.
- Write `docs/decisions/0006-airfoil-set-selection.md` with severity, blast radius, options, chosen design, graded evidence, and predeclared proof.
- Commit documentation separately.

### Task 2 — RED: coordinate parity and exact OpenVSP sections

- Add domain tests for archive readability, name/version parity, per-section hashes, mutation detection, and the exact fixture ledger.
- Add an OpenVSP integration test that reads the generated `.vsp3` and asserts all three wing curves are `XS_FILE_AIRFOIL` with the approved names/hashes reported.
- Run the focused tests and record the expected failures before implementation.
- Implement the archive reader, generator extension, committed archive, and builder change; rerun to green.

### Task 3 — RED: recommendation contract

- Add deterministic fixture tests for station state, six-candidate production search `[D]`, Pareto filtering, three distinct schedules, strategy ranking, badge routing, stable coupled passes, leave-one-metric-out closure, 10-candidate/8-pass sensitivity `[D]`, evidence weakening, and failure gates.
- Implement `domain_packs/bwb/airfoil_selection.py` using existing aero/stall/trim/sizing primitives.
- Run focused domain tests in the fast Windows loop and the WSL environment.

### Task 4 — Store, API, and MCP

- Test immutable version paths, atomic/idempotent writes, locks, path traversal, reload, source staleness, and status codes.
- Implement `src/services/airfoil_selection.py` and the dedicated router.
- Test all four routes and the Concepts `409` gate.
- Add and test MCP recommendation/selection tools and mixed-type geometry overrides.

### Task 5 — Concepts, promotion, and Stage 2 propagation

- Test explicit approval consumption and exact schedule equality across baseline/every lens.
- Test candidate index, promotion, restore, stale batch behavior, and handoff names/version/hash/types.
- Update candidate generation, geometry/reporting, promotion, and handoff without changing the CFD LangGraph.

### Task 6 — Airfoil Sets UI

- Add DOM/JavaScript tests first for three cards, modal graphs, strategy switching, selection, Continue event/callback, reload/back restore, keyboard handling, stale/loading/error states, responsive behavior, and Three.js coordinate use.
- Implement the self-contained UI module and stylesheet.
- Verify no Findings selector or DOM mutation exists in the module.

### Task 7 — Shared-shell commits and documentation

- Commit feature implementation separately from shared shell wiring.
- Commit `src/cockpit/server.py` router import/registration alone.
- Commit `stage1.html` and `stage1.js` registration/navigation alone.
- Update README, intake, handoff, and evidence docs to remove obsolete static/recorded-only claims.
- Raise `minimum_collected_tests` by the number of newly collected tests; never lower it.
- Record every shared file, DOM ID/event, route, Findings transition contract, and exact shared-shell commit SHA in the decision record or a linked implementation note.

### Task 8 — Verification and handoff

- Run targeted Windows domain tests.
- Run targeted WSL service/API/UI/OpenVSP tests.
- Run the full WSL pytest suite.
- Run `verification/doctor.py`, VSPAERO smoke, and `verification/verify_golden.py --no-llm`.
- Record exact commands, pass/fail/skip counts, and concrete environmental blockers.
- Request code review, address verified issues, use verification-before-completion, and leave the branch unmerged.

## 8. Completion criteria

The gate is complete only when:

- all three deterministic schedules are distinct and auditable;
- the current approval is the single source of airfoil names for every generated concept;
- stale or absent approval cannot reach Concepts;
- coordinate/polar parity and exact OpenVSP file-airfoil behavior are proven;
- immutable history survives reload and idempotent retries;
- UI and programmatic interfaces expose the same state and errors;
- the selection identity reaches promotion and Stage 2;
- the full requested verification evidence is recorded, or a concrete environmental blocker is named without claiming the blocked item passed.
