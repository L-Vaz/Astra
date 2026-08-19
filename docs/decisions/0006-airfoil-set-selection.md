# 0006 — Add a deterministic Airfoil Sets approval gate

- **Status:** Accepted and verified
- **Date:** 2026-08-19
- **Owners:** Astra maintainers
- **Area:** Stage 1 airfoil selection, evidence, geometry propagation

## Problem

Stage 1 currently stores airfoil names in `BWBParams`, but OpenVSP does not construct those named sections. On the unmodified `192bf4b9a2708e5ecd6864ebfc6a64d1800c576e` baseline, the golden reference names are `e474`, `e472`, and `s5010`, while all three OpenVSP wing curves are generic four-series shapes.

Reproduction from the repository root:

```bash
/opt/vsp-venv/bin/python - <<'PY'
import json
import os
import openvsp as vsp
from domain_packs.bwb.schema import BWBParams
from domain_packs.bwb.vsp_builder import build_bwb

raw = json.load(open("tests/data/aero_golden.json"))["reference"]
p = BWBParams.model_validate(raw["params"])
os.makedirs("/tmp/astra_airfoil_before_33c7", exist_ok=True)
out = build_bwb(p, "/tmp/astra_airfoil_before_33c7", [])
vsp.ClearVSPModel()
vsp.ReadVSPFile(out["vsp3"])
wing = vsp.FindGeoms()[0]
surf = vsp.GetXSecSurf(wing, 0)
print([p.root_airfoil, p.mid_airfoil, p.tip_airfoil])
print([vsp.GetXSecShape(vsp.GetXSec(surf, i)) for i in range(3)])
print(vsp.XS_FOUR_SERIES, vsp.XS_FILE_AIRFOIL)
PY
```

Observed `[C]`:

```text
['e474', 'e472', 's5010']
[7, 7, 7]
7 12
```

The builder also writes `ThickChord` values, which rescales the generic sections but cannot turn them into the named coordinate shapes. The defect is therefore demonstrated in the produced geometry, not inferred from code appearance.

## Severity and blast radius

Severity is **high for design traceability** `[D]`: airfoil-dependent polar estimates and the geometry evaluated downstream describe different section shapes. The blast radius covers Stage 1 concept geometry, VSPAERO runs, promotion manifests, and Stage 2 handoff/report evidence. It does not alter the inherited CFD LangGraph orchestration.

From first principles, a section name is meaningful only if both sides of the calculation use the same boundary shape. Pressure gradients, separation, lift, moment, and profile drag arise from the surface coordinates. Matching thickness alone does not make two cambers or leading-edge shapes aerodynamically identical.

## Evidence ledger

| Quantity or claim | Grade | Source / method | Decision use |
| --- | --- | --- | --- |
| Current curve types are `[7, 7, 7]` while `XS_FILE_AIRFOIL == 12` | `[C]` | Reproduction above with OpenVSP bindings and golden fixture | Proves the mismatch before its fix |
| Polar table contains 2,171 named sections and declares AeroSandbox 4.2.8 / NeuralFoil 0.3.3 | `[C]` | Committed `domain_packs/bwb/data/airfoils_neuralfoil.json`, verified by tests | Defines the parity target |
| `s5010` is a flying-wing section with measured low-speed polars and near-zero quarter-chord moment | `[A]` | Selig et al., *Summary of Low-Speed Airfoil Data, Vol. 2*, §4.3/Table 4.5 and Fig. 5.90 onward, UIUC: https://m-selig.ae.illinois.edu/pubs/Low-Speed-Airfoil-Data-V2.pdf | Exact fixture reference and evidence strength |
| `e472` has measured low-speed data; its listed geometry is 12.13% thick and symmetric | `[A]` | Selig et al., *Summary of Low-Speed Airfoil Data, Vol. 3*, Table 4.1 and Fig. 5.45 onward, UIUC: https://m-selig.ae.illinois.edu/pubs/Low-Speed-Airfoil-Data-V3.pdf | Exact fixture reference and evidence strength |
| UIUC lists coordinates for `e472`, `e474`, and `s5010`, but no exact experimental polar reference was located for `e474` | `[A]` for the database listing; evidence gap for a polar | UIUC Airfoil Coordinates Database: https://m-selig.ae.illinois.edu/ads/coord_database.html | Missing `e474` experiment weakens evidence only |
| Six retained candidates per station | `[D]` | Interactive exhaustive-search budget: at most 216 triples/pass | Must agree with a 10-candidate sensitivity fixture or warn |
| Five coupled resize/trim passes | `[D]` | Bounded-latency convergence judgement | Must agree with an 8-pass fixture or report non-convergence |
| Ten candidates and eight passes for sensitivity | `[D]` | Wider/higher comparison selected to challenge both production bounds | A changed result without a surfaced warning falsifies the production bounds |

## Decision drivers

- One airfoil schedule must be reviewable and approved before Concepts.
- Correctness cannot depend on an LLM choosing or interpreting a section.
- The section used in polar evidence must be the section built in OpenVSP.
- Whole-wing consequences matter more than independent station scores.
- Missing experimental evidence must be visible without excluding otherwise valid library data.
- History, retries, and staleness must remain deterministic and auditable.

## Considered options

### 1. Keep lens-specific hard-coded airfoils and record them after generation

Rejected. It preserves the demonstrated geometry/polar mismatch and never creates a user approval boundary.

### 2. Let users replace individual stations from a searchable library

Rejected. Independent station edits can defeat thickness progression, trim, stability, and stall behavior. It also expands the approved scope beyond a coordinated schedule gate.

### 3. Rank coordinated schedules, approve once, and propagate exact coordinates

Chosen. Exhaustive station combinations are evaluated with existing whole-wing models, Pareto-filtered, ranked under three deterministic strategies, and closed with sensitivity. The approval fixes exact root/mid/tip identities for every concept.

## Decision

Implement the contract in `docs/superpowers/plans/2026-08-19-airfoil-selection.md`.

The selector is deterministic domain code. It derives local operating conditions, gates unavailable or out-of-envelope data, evaluates coordinated triples with existing drag/stall/trim/stability/geometry logic, Pareto-filters them, and produces three distinct strategies. Balanced minimizes worst normalized regret; Efficiency is drag-first; Low-speed/control is stall/control-first.

The low-speed screen evaluates the actual mission speed. It never raises speed to the polar-table Reynolds floor: a requested station condition outside the committed envelope rejects that candidate, and failure to produce three feasible schedules is a `409` conflict. Approval writes the chosen converged parameters into a new immutable plan version. The approval records the recommendation's source plan and the plan it created so its own output remains current while any later external plan change is detected.

Commit an exact coordinate archive generated from the same AeroSandbox library/version as the polar table. OpenVSP must use `XS_FILE_AIRFOIL` and `ReadFileAirfoil` for root/mid/tip. Recommendation and approval artifacts are immutable and versioned. Candidate generation explicitly requires a current approval, applies its schedule after every lens override, and propagates the selection version/hash into promotion and Stage 2.

## Predeclared proof

The implementation is proven only if all of the following are observed:

1. Coordinate and polar name sets and AeroSandbox versions are identical; every coordinate hash verifies.
2. A real OpenVSP integration test reads the built `.vsp3` and reports curve types `[12, 12, 12]` for the selected root/mid/tip, with the selected names and hashes in the geometry report.
3. Domain fixtures prove deterministic output, three distinct schedules, correct Pareto/ranking/badge behavior, leave-one-metric-out closure, and explicit 10-candidate/8-pass sensitivity.
4. Store/API tests prove immutable paths, idempotency, locking, reload, staleness, and `404`/`409`/`422` semantics.
5. Propagation tests prove exact schedule equality across baseline and every lens and carry selection identity through promotion and handoff.
6. UI tests prove modal/graphs/switching/reload/keyboard/stale/loading/error/responsive behavior and the documented navigation event without Findings DOM access.
7. The full WSL suite, environment doctor, VSPAERO smoke, and no-LLM golden are run and their exact results recorded.

Until those checks pass, the verdict remains **unproven**; the reproduction proves the defect, not the fix.

## Consequences

Positive consequences are a visible decision gate, one coordinate/polar identity through the design loop, deterministic retries, and an evidence weakness that can be reviewed independently from feasibility.

Costs are a larger committed data artifact, additional Stage 1 state/versioning, an extra approval interaction, and bounded but nontrivial whole-wing schedule evaluation. The production search/pass limits remain explicit `[D]` judgements and can be revised only with recorded sensitivity evidence.

## Isolation contract

The CFD LangGraph is unchanged. Findings DOM IDs, modules, routes, and transitions are unchanged. Shared shell edits are limited to router registration in `src/cockpit/server.py` and Airfoil screen/navigation registration in `stage1.html`/`stage1.js`, each in the separately required commit.

## Verification verdict

**Solved.** The produced geometry now uses the same exact named coordinate sections as
the approved schedule and its polar evidence. The following checks were run on
2026-08-19 from the feature worktree:

| Check | Result |
| --- | --- |
| Full WSL suite: `/opt/conda/envs/FoamAgent/bin/python -m pytest tests/ -q -rx` | 874 collected; 860 passed, 5 named skips, 10 strict expected failures; 360.75 s |
| Environment doctor | PASS: 15 passed, 0 failed, 1 credential check skipped |
| No-LLM golden | PASS: 21 passed, 0 failed, 2 simulation/evaluation phases skipped by `--no-llm` |
| Exact-section VSPAERO smoke | PASS: 6 polar points; `e474/e472/s5010`; three `XS_FILE_AIRFOIL` curves; polar, lift-slope, and trim gates passed |
| Real OpenVSP integration test | PASS: selected curve types `[12, 12, 12]`, with exact names and hashes in `geometry_report.json` |
| Coordinate regeneration | PASS: 2,171 sections, 1.28 MB compressed archive, no repository diff |
| Browser-math parity | PASS: atmosphere, planform, spanwise/stall, and station Node scripts |

The no-LLM and VSPAERO commands logged four unreadable inherited FAISS indices before
continuing; neither command consumed RAG and both completed successfully. Doctor's
FAISS path/import checks passed. This is an existing data-index warning, not evidence
for or against the Airfoil Sets implementation.

Interactive browser verification `[C]` exercised all three strategy cards; exact
station outline/lift/drag graphs; Aerodynamics and Evidence & References tabs;
keyboard tab switching, close, and focus restoration; approval; exact-coordinate
Concepts generation; reload restoration; stale-card preservation and disabled
Continue after an intake change; and a 390 px viewport. The measured document width
equalled the viewport width. It also reproduced a cached `stage1.js?v=1` bundle still
calling the obsolete direct-generate transition; shared-shell commit `e321f65` bumps
the three Airfoil Sets assets to `v=2`, and the corrected browser run loaded those
URLs with no console error.

## Shared-shell and public-interface inventory

Only these shared files changed:

| Shared file | Exact commit | Change |
| --- | --- | --- |
| `src/cockpit/server.py` | `19defac35e6da597ed9db844af9f5180a82c9779` | Two-line router import and registration only |
| `src/cockpit/static/stage1.html` | `570475d1f0682c62632cdcf270100735e50b9c72` | Dedicated stylesheet import and Airfoil Sets screen/modal mount only |
| `src/cockpit/static/stage1.js` | `570475d1f0682c62632cdcf270100735e50b9c72` | Module import, screen registration, and Requirements → Airfoil Sets → Concepts navigation only |
| `src/cockpit/static/stage1.js` | `6830d0926eeb8545965a05e21513ba48c7a24f13` | Consume the dedicated exact-coordinate provider in the Concepts loft |
| `src/cockpit/static/stage1.html` | `985a9524b377396963af6757f2c4280794a84fc0` | Rename the entry action to “Recommend airfoil sets” |
| `src/cockpit/static/stage1.html`, `src/cockpit/static/stage1.js` | `e321f65f9716f65b2e6b9869edfff25c97837273` | Bump dedicated Airfoil Sets asset URLs to invalidate the demonstrated stale browser cache |

`src/cockpit/static/stage1.css` is unchanged. No Findings file, selector, DOM ID,
route, or transition was edited. The inherited CFD LangGraph is unchanged.

The added Airfoil Sets DOM IDs are:

```text
screen-airfoils
airfoil-loading  airfoil-error  airfoil-stale  btn-airfoil-recompute
airfoil-cards  airfoil-loft-panel  airfoil-loft-canvas
btn-airfoil-back  btn-airfoil-continue
airfoil-modal  airfoil-modal-title  btn-airfoil-modal-close
airfoil-outline  airfoil-lift-graph  airfoil-clmax  airfoil-drag-graph
airfoil-detail-metrics  airfoil-evidence
airfoil-detail-tabs  airfoil-tab-aerodynamics  airfoil-tab-evidence
airfoil-panel-aerodynamics  airfoil-panel-evidence
```

The dedicated router owns the four Airfoil Sets routes and shadows the two legacy
transitions that must enforce approval:

```text
POST /api/stage1/airfoils/recommend
GET  /api/stage1/airfoils
GET  /api/stage1/airfoils/detail
POST /api/stage1/airfoils/select
POST /api/stage1/generate
GET  /api/handoff/{session_id}
```

Continue dispatches `astra:stage1-airfoil-continued` on `window` and invokes the
optional `onContinue(detail)` callback with the same payload:
`sessionId`, `selectionVersion`, `selectionHash`, `scheduleHash`, and an exact
root/mid/tip `schedule`. Findings may consume either notification. The Airfoil Sets
module does not query, write, or navigate through Findings DOM; its only transition is
the documented public notification, after which the shared Stage 1 shell opens
Concepts.
