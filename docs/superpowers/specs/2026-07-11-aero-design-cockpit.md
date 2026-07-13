# Astra Aero Design Cockpit — design spec

Status: agreed 2026-07-11. Supersedes the informal draft discussed in chat.
Scope: aerodynamics and geometry only. This spec pulls the cockpit (roadmap
"JARVIS UI" phase) forward, ahead of the DOE/provenance phase.

## Product goal

A user describes a BWB drone mission conversationally in Claude Code
("surveillance drone, 1.5 kg payload, 25 m/s cruise at 100 m, 80 km range").
Astra converts that into explicit mission values and assumptions, builds an
initial parametric BWB geometry, and offers a link that opens an interactive
3D design cockpit in the browser. Chat and cockpit edit the same design
session; every accepted change is a new version; approved geometry exports a
half-model for symmetry-plane CFD.

## Decisions (from 2026-07-11 design discussion)

1. **Chat-first, Claude Code + MCP stays the controller.** The cockpit is a
   secondary surface opened from chat, not a replacement chat app.
2. **Cockpit is a browser-based local web app**, not a native window. A small
   FastAPI server in WSL serves a three.js frontend; Windows browser reaches
   it via localhost. A new MCP tool starts the server and prints the link.
3. **Session sync model:** "Apply Revision" in the browser calls the backend
   directly — rebuild, gates, and VSPAERO are deterministic and need no LLM.
   A plain-language explanation of consequences is queued; chat delivers it
   the next time the user interacts there (chat reads session state on every
   turn). Chat instructions likewise mutate the same session store.
4. **Derived-control rebalancing is fixed and documented** (see table below).
   No LLM guesses values; no hidden compensation beyond the stated rule.
5. **Live 3D morphing is a requirement.** The browser keeps its own
   parametric lofted surface (built from the same section/planform
   definitions as the VSP model) that deforms while a slider moves. The
   authoritative STL replaces the preview only after a successful OpenVSP
   rebuild. The preview is an approximation and is labelled as such.
6. **Half model is a CFD-export/mesh-stage feature only.** VSPAERO and the
   viewer use the full symmetric model. The centreline cut, symmetry patch,
   and halved farfield happen in the Gmsh/mesh-conversion stage.
7. **CG and static margin use a documented assumption:** payload as a point
   mass at a user-set location plus structure as uniform area density over
   the planform. Displayed static margin carries an explicit
   "assumed mass model" label until real mass modelling exists.
8. **Range and endurance are recorded mission goals, not inputs to sizing.**
   Labelled "not yet evaluated — propulsion/energy out of scope." Add
   `range_km` to `MissionSpec` as an inert field.
9. **Audience:** built to be shown soon via shared repo + demo video. No
   multi-machine packaging workstream in v1.

## Architecture

```text
Claude Code (chat)
   │  MCP tools (existing 6 Astra + new open_cockpit)
   ▼
FastAPI session server (WSL)  ◄──── browser cockpit (three.js)
   │
   ├─ design_session.py      (single source of truth: versions, locks)
   ├─ intent / geometry / vspaero / bwb_mesh / evaluate services
   ├─ OpenVSP via /opt/vsp-venv/bin/python subprocess
   └─ artifacts under ~/astra_runs (never the space-containing repo path)
```

- The MCP tools and the web UI call the same service layer; neither owns
  private state. `design_session.py` state moves behind the FastAPI layer so
  both surfaces share it.
- Existing MCP tools (`plan_design`, `build_geometry`, `run_fast_aero`,
  `generate_mesh`, `run_simulation`, `evaluate_results`) keep their
  contracts; the cockpit reuses them server-side rather than duplicating
  logic.

## Mission panel

Payload mass, cruise speed, cruise altitude, launch method, optional MTOW,
intended range (inert), intended endurance (inert). Meaningful derivations:
altitude → density; speed+density → q; payload+MTOW assumption → weight;
launch method → wing-loading assumption; W,q,S → required CL;
speed, density, viscosity, MAC → Reynolds number.

## Geometry controls

Primary (default view): wingspan, reference area, aspect ratio, centre-body
width, sweep, thickness, stability preference.

Advanced (expandable) — maps 1:1 to `BWBParams`: root chord, centre-body
fraction, mid-chord fraction, outer taper, inner/outer LE sweep, root/tip
thickness ratio, root incidence, tip twist, dihedral.

Every control shows: current value, original value, permitted range
(from the Pydantic field bounds), units, lock state, dependent values,
validation warnings, and a one-line engineering explanation.

### Rebalancing rules for derived controls

| Control dragged | Rule |
|---|---|
| Reference area | Uniformly scale all chords (root chord scales, fractions preserved); span fixed |
| Aspect ratio | Adjust span, scale chords to hold S fixed |
| Span | Span only; S and AR update as displayed consequences |
| Any rule touching a **locked** parameter | Control refuses and names the lock; never picks a different victim |

"Stability preference" maps to a target static margin band that biases
sweep/twist during rebalancing, computed with the assumed mass model.

## Evidence levels

Every displayed number carries a source label:

- **Instant preview** — deterministic equations and planform analytics
  (S, AR, MAC, required CL, Re). Mathematical consequence, not prediction.
- **Fast validated** — VSPAERO results (lift curve, trim, L/D, neutral
  point, static margin estimate, spanwise loading).
- **CFD verified** — OpenFOAM results after approval (forces, convergence,
  fields, comparison against VSPAERO).

Nothing is called "verified" because a preview updated.

## Versioning

Visual design-history tree backed by `design_session.py`: compare, create
revision, restore-as-new-version, approve, reject, run fast aero, export for
CFD. Restoring never deletes later versions; upstream revisions invalidate
downstream results while retaining them for comparison.

## Validation gates

Existing geometry gates (parameter ranges, cross-checks, manufacturable tip
chord, twist direction, AR limits, VSP build success, watertight STL,
bounding box, reference-area agreement) plus new half-model gates:

- Cut lies exactly on the centre plane and the cut face is planar
- Left/right symmetry verified before cutting
- No duplicate/overlapping surfaces; correct normals
- Half projected area consistent with full model
- Mesh conversion identifies aircraft wall vs symmetry plane correctly —
  the centre cut must never become a no-slip wall
- Farfield domain halved with its centre boundary assigned symmetry

Gate failures name the affected control ("outer taper produces a 32 mm tip
chord, below the 40 mm minimum").

## Export artifacts

Full `.vsp3`, full STL, half-model STL, centre-plane definition, reference
quantities, surface/patch metadata, geometry validation report. Export
choice: full aircraft (visuals) or half aircraft with symmetry plane
(default for CFD on local hardware).

## v1 release loop

1. Enter mission in chat → structured mission + assumptions.
2. Deterministic sizing → initial BWB geometry.
3. Click link in chat → cockpit opens with live parametric preview.
4. Adjust mission/geometry; see instant equation consequences.
5. Apply revision → OpenVSP rebuild + gates (deterministic, no LLM).
6. Run VSPAERO; compare against previous version.
7. Approve; export half-model for symmetry CFD; hand off to inherited graph.
8. Return to chat; Astra explains what changed and what it implies.

## Deferred

Structures, aeroelasticity, propulsion, battery/power, authoritative
range/endurance, manufacturing planning, general text-to-CAD, DOE registry
(resumes after cockpit v1), multi-machine packaging.
