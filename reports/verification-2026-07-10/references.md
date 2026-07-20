# Sources & Justification — What Was Referred To, and What Was Not

**Companion to:** `report.md` (same folder) · **Date:** 2026-07-10

---

## 1. Honest statement of method — read this first

**During the build I did not consult official documentation in real time**, with one exception: I read the **installed OpenFOAM v10 source code** on your machine (`/opt/openfoam10/applications/...`) to establish how `gmshToFoam` behaves. Everything else came from three places:

1. **Model training knowledge** of OpenFOAM, Gmsh, and OpenVSP — i.e., knowledge absorbed from these tools' documentation, tutorials, and community material, but *not* looked up at decision time.
2. **Files shipped inside your own official OpenFOAM v10 installation** — source code and the official tutorial cases in `/opt/openfoam10/tutorials/` (these are authoritative: they ship with the code, from openfoam.org).
3. **Empirical verification on your machine** — every setting was exercised end-to-end: `checkMesh` output, solver logs, controlled A/B experiments (unlimited vs limited schemes; laminar vs turbulent on the same mesh; HXT vs Delaunay). Nothing below is claimed from reasoning alone.

**At your request, I have now (2026-07-10) verified every external citation below by fetching the live official pages.** Where a claim is *not* backed by official documentation — the engineering-judgment numbers in the sizing rules, the plausibility band — it is explicitly flagged as such in §4. You asked me to say plainly if something wasn't referred to: the items in §4 were not referred to anywhere; they are judgment calls encoded as auditable assumptions.

**Verified official sources used below:**

| ID | Source | URL / location | Verified |
|---|---|---|---|
| [UG] | OpenFOAM v10 User Guide (official, CFD Direct / openfoam.org) | https://doc.cfd.direct/openfoam/user-guide-v10/ | fetched 2026-07-10 |
| [CPP] | OpenFOAM v10 Source Code Guide (official Doxygen) | https://cpp.openfoam.org/v10/ | fetched 2026-07-10 |
| [SRC] | OpenFOAM v10 installed source on your machine | `/opt/openfoam10/applications/`, `/opt/openfoam10/src/` | read directly |
| [TUT] | Official OpenFOAM v10 tutorials shipped with your installation | `/opt/openfoam10/tutorials/` | read directly |
| [GMSH] | Gmsh 4.15.2 Reference Manual (official, Geuzaine & Remacle) | https://gmsh.info/doc/texinfo/gmsh.html | fetched 2026-07-10 |
| [VSP] | OpenVSP API documentation (official) | https://openvsp.org/api_docs/latest/ | fetched 2026-07-10 |
| [REPO] | The Foam-Agent repo itself + its paper | `user_req_tandem_wing.txt`; arXiv:2509.18178 | in-repo |
| [USSA] | U.S. Standard Atmosphere, 1976 (NOAA-S/T 76-1562 / NASA-TM-X-74335) | https://ntrs.nasa.gov/citations/19770009539 | fetched and implemented 2026-07-20 |

---

## 2. Decision-by-decision: what was done, why, and where it is documented

Legend — **Build-time source:** what I actually relied on when making the decision. **Official reference:** where you can verify it yourself (verified links/paths only).

### 2.1 CFD numerics (the settings that made the solve converge)

| Decision | Why | Build-time source | Official reference |
|---|---|---|---|
| `laplacianSchemes: Gauss linear limited corrected 0.33` and `snGradSchemes: limited corrected 0.33` | Unlimited `corrected` on a tet mesh with ~84° max non-orthogonality diverged (proven by A/B on your machine) | Training knowledge + A/B experiment | [UG] §4.5.5 Laplacian schemes — the guide itself lists the exact string `Gauss linear limited corrected 0.33` as a standard configuration |
| `div(phi,U) bounded Gauss linearUpwind grad(U)`; `div(phi,k) / div(phi,omega) bounded Gauss upwind` | The `bounded` wrapper stabilizes steady SIMPLE runs; k blow-up disappeared after adding it | Training knowledge + A/B experiment | [UG] §4.5.3 Divergence schemes (documents the bounded Gauss variant and why); **[TUT] motorBike** — the official external-aero-over-STL tutorial uses the same pattern verbatim: `div(phi,U) bounded Gauss linearUpwindV grad(U)`, `div(phi,k) bounded Gauss upwind`, `div(phi,omega) bounded Gauss upwind` (`/opt/openfoam10/tutorials/incompressible/simpleFoam/motorBike/system/fvSchemes`) |
| `gradSchemes: cellLimited Gauss linear 1` for U/k/omega | Gradient limiting on skewed cells | Training knowledge (the generated case used it; kept) | [UG] §4.5 Numerical schemes (gradient limiting subsection); also appears in shipped official tutorials |
| SIMPLE, `nNonOrthogonalCorrectors 2`, relaxation p 0.3 / U 0.5 | Standard steady incompressible practice; extra correctors for high non-orthogonality | Training knowledge | [UG] §4.6 Solution and algorithm control (fvSolution): https://doc.cfd.direct/openfoam/user-guide-v10/fvsolution |
| k/ω relaxation 0.3 (motorBike ships 0.5) | More conservative than the official tutorial after observing k blow-up on our worse-quality mesh | **Engineering judgment** (deviation from tutorial value, flagged) + A/B experiment | [TUT] motorBike `fvSolution` ships 0.5 — our 0.3 is deliberately more damped |
| Boundary conditions: `fixedValue` velocity inlet, `inletOutlet` outlet + fixed p, `slip` farfield, `noSlip` wall | Standard external-aero BC set | Training knowledge | [UG] Boundaries chapter: https://doc.cfd.direct/openfoam/user-guide-v10/boundaries ; same pattern in [TUT] motorBike `0/` files |
| kOmegaSST with `nutkWallFunction` / `kqRWallFunction` / `omegaWallFunction` | Standard RANS external aero with wall functions | Training knowledge | [TUT] motorBike uses exactly these; wall-function classes documented in [CPP] |

### 2.2 The wall-type fix (why the aircraft patch must be `wall`)

| Claim | Build-time source | Official reference |
|---|---|---|
| `gmshToFoam` creates every patch as generic type `patch` (no wall heuristic) | **[SRC] read at build time:** `/opt/openfoam10/applications/utilities/mesh/conversion/gmshToFoam/gmshToFoam.C` — patches constructed with `polyPatch::typeName` (~line 902); physical names used only for patch *names* | Same file in [CPP]/[SRC]; you can open it yourself |
| Wall functions abort on non-`wall` patches | **Observed directly:** solver FatalError *"Invalid wall function specification — Patch type for patch aircraft must be wall"* pointing to `nutWallFunctionFvPatchScalarField.C` line 45, `checkType()` | [SRC] `/opt/openfoam10/src/MomentumTransportModels/momentumTransportModels/derivedFvPatchFields/wallFunctions/nutWallFunctions/nutWallFunction/nutWallFunctionFvPatchScalarField.C` |
| Fix via `foamDictionary constant/polyMesh/boundary -entry entry0/aircraft/type -set wall` | Training knowledge of `foamDictionary`; verified by running it + `checkMesh` + a full solve | `foamDictionary -help` on your machine; [UG] post-processing/utilities chapter |
| "No cells read" failure mode | **[SRC] read at build time:** `gmshToFoam.C` ~line 726 emits exactly this FatalIOError when the .msh contains no 3D elements | Same file |

### 2.3 Meshing (Gmsh)

| Decision | Why | Build-time source | Official reference |
|---|---|---|---|
| `Mesh.Algorithm3D = 1` (Delaunay) instead of 10 (HXT) | HXT silently produced 0 cells under load (observed twice); Delaunay deterministic | Training knowledge + observed failure | [GMSH] §1.2.1 "Choosing the right unstructured algorithm" documents both algorithms |
| Use STL triangles directly as boundary mesh (`removeDuplicateNodes` + `createTopology` + surface loop + volume), no surface remeshing | `classifySurfaces`/`createGeometry` reparametrization produced overlapping facets at the sharp trailing edge (observed); discrete-boundary volume meshing avoids it | Training knowledge of the Gmsh discrete-mesh workflow + observed failure | [GMSH] official tutorials on discrete/STL meshing (t13 "remeshing STL without CAD"; x-series API tutorials cover `createTopology` for discrete entities) |
| Distance + Threshold size fields for near-body refinement | Standard Gmsh mechanism for proximity-based sizing | Training knowledge | [GMSH] §"Mesh size fields" / tutorial t10 (verified present in manual) |
| `Mesh.MshFileVersion = 2.2` for the export | `gmshToFoam` parses the legacy MSH2 format | Training knowledge + [SRC] gmshToFoam.C (written against MSH2 tags like `$PhysicalNames`, `$ENDELM`) | [GMSH] tutorial t1 documents `Mesh.MshFileVersion` for saving older MSH versions |
| `optimize("Netgen")` pass after generation | Raw Delaunay tets couldn't hold a steady solution; Netgen pass cut avg non-orthogonality 22.6→17.9, max skewness 2.6→1.5 (measured by `checkMesh`), after which laminar AND kOmegaSST both converged | Training knowledge + measured A/B on your machine | [GMSH] documents Netgen (Frontal) optimization; `gmsh.model.mesh.optimize` in the API section of the manual |
| checkMesh gates (Mesh OK, non-orthogonality, skewness, cell count) | Quality gates before any solve | Training knowledge | `checkMesh` is the official OpenFOAM mesh-quality utility; thresholds it enforces are internal to [SRC] |

### 2.4 CFD-through-the-graph mechanism

| Decision | Build-time source | Official reference |
|---|---|---|
| Feed the approved `.msh` via `--custom_mesh_path` so the **unchanged** Foam-Agent graph converts and locks it | **[REPO]:** the shipped `tandem_wing.msh` + `user_req_tandem_wing.txt` example is exactly this flow; graph code read directly (`src/services/mesh.py`, `src/nodes/meshing_node.py`, `src/router_func.py`) | The repo itself + Foam-Agent paper (arXiv:2509.18178) |
| Requirement wording must say "custom mesh provided", never "Gmsh mesh" | **[REPO] read at build time:** `src/router_func.py` `llm_requires_custom_mesh` — its prompt literally routes "create mesh using gmsh" → `gmsh_mesh` mode | `src/router_func.py` lines 20–45 in your repo |

### 2.5 Post-processing & evaluation

| Decision | Build-time source | Official reference |
|---|---|---|
| `forceCoeffs` function object with `liftDir`, `dragDir`, `pitchAxis`, `magUInf`, `Aref`, `lRef` (+ `rhoInf`, `CofR`) | Training knowledge | [CPP] forceCoeffs page (verified): https://cpp.openfoam.org/v10/classFoam_1_1functionObjects_1_1forceCoeffs.html — documents liftDir/dragDir/pitchAxis/magUInf/Aref/lRef; `rhoInf` and `CofR` are documented in the parent **forces** class on the same site |
| `rho rhoInf; rhoInf <density>` because simpleFoam pressure is kinematic (p/ρ) | Training knowledge | forces class docs in [CPP]; standard incompressible-solver convention |
| Post-hoc recomputation via `foamDictionary ... -entry functions -set {...}` + `simpleFoam -postProcess -latestTime` | Training knowledge | `simpleFoam -help` lists `-postProcess`; [UG] post-processing chapter covers running function objects on saved fields |
| Convergence judged by initial residuals + force-history tail; residual threshold 5e-3, force tail ±5% | **Engineering judgment** (flagged in §4) — thresholds are conventional, not from a single official source | — |
| liftDir/dragDir rotation by α; trim from CL(α) linear fit | Classical aerodynamics (textbook level) | Any aero text (e.g., Anderson, *Fundamentals of Aerodynamics*) — not an "official doc" |

### 2.6 OpenVSP / VSPAERO (tier-1 aero)

| Decision | Build-time source | Official reference |
|---|---|---|
| `VSPAEROComputeGeometry` then `VSPAEROSweep`; `GeomSet=SET_NONE` + `ThinGeomSet=SET_ALL` for VLM (thin+thick together duplicates every panel — observed "4 tris per edge" warnings) | **Largely discovered empirically at runtime** by probing the installed API (`PrintAnalysisInputs`, inspecting outputs) + training knowledge | [VSP] API docs (verified: Analysis Manager + VSPAERO groups documented); detailed input names were confirmed against your installed 3.49.0 binding, which is the ground truth for that version |
| Parse `aero_model.polar` file directly instead of the results API | Results-container names vary across OpenVSP versions; the file format is stable | Empirical + training | The .polar header/columns are produced by your installed VSPAERO 3.49.0 — self-documenting output |
| Wing parm names (`Span`, `Root_Chord`, `Tip_Chord`, `Sweep`, `Twist`, `ThickChord`, `SetDriverGroup`, ...) | Training knowledge, **defensively verified at runtime**: the builder checks every parm with `GetParm` before setting and logs any miss (zero misses in all runs) | [VSP] API docs; your installed binding as ground truth |
| Neutral point from dCM/dCL about x=0: `x_np = −dCM/dCL · cref`; static margin definition | Classical flight mechanics (textbook level) | Any stability & control text (e.g., Etkin; Nelson) — not an "official doc" |

---

## 3. Numbers cross-checked against official shipped examples

The single strongest validation available on your machine: the **official motorBike tutorial** (external aero over an STL body, kOmegaSST, simpleFoam — the same problem class as our BWB) ships inside your OpenFOAM installation and uses the same scheme family we converged to:

```
/opt/openfoam10/tutorials/incompressible/simpleFoam/motorBike/system/fvSchemes:
    div(phi,U)      bounded Gauss linearUpwindV grad(U);
    div(phi,k)      bounded Gauss upwind;
    div(phi,omega)  bounded Gauss upwind;
```

Differences from motorBike, each deliberate: motorBike's snappyHexMesh grid is hex-dominant with low non-orthogonality, so it can afford unlimited `corrected` Laplacians and U-relaxation 0.9; our tet mesh cannot, hence `limited corrected 0.33` (which [UG] §4.5.5 documents as the standard remedy) and gentler relaxation.

---

## 4. What is NOT backed by documentation — plainly stated

These are **engineering-judgment values**, chosen by me, not referred from any official source. They all live in `domain_packs/bwb/defaults.py` / `src/services/evaluate.py` as explicit, changeable, logged assumptions — and several are textbook-conventional, but I did not consult a text at build time:

| Value | Where | Nature |
|---|---|---|
| Wing loading defaults 7 / 10 / 13 kg/m² by launch method | `defaults.py` | small-UAV rule of thumb |
| Payload fraction 30% → MTOW estimate | `defaults.py` | rule of thumb |
| Aspect ratio 5.5 for a BWB | `defaults.py` | judgment |
| Static-margin target 8% MAC | `defaults.py` | tailless-aircraft convention |
| Max cruise CL 0.75 before growing the wing | `defaults.py` | judgment |
| BWB parameter hard ranges (sweep 15–60°, t/c 8–20%, etc.) | `schema.py` | judgment |
| Plausibility band CFD-vs-VLM: ±max(0.15, 60%) | `evaluate.py` | judgment, documented in-code as "coarse-tier, order-of-magnitude check" |
| CL-target tolerance ±25%; residual threshold 5×10⁻³; force-tail ±5% | `evaluate.py` | convention/judgment |
| Standard-atmosphere state from 0–4,000 m (temperature, pressure, density, dynamic and kinematic viscosity) | `atmosphere.py` + versioned CSV/metadata | [USSA] Equations 51–52 and the standard thermodynamic model; generated table is checksum-verified and linearly interpolated |
| CL_α sanity range 0.02–0.15 /deg; "expected ~0.05–0.09" | `vsp_aero.py` | lifting-line textbook estimate |
| "y+ ≈ 900 → CL under-prediction expected" interpretation | report | qualitative CFD experience; the 30–300 wall-function validity range is textbook (e.g., Versteeg & Malalasekera), not consulted at build time |

**Implication you should act on:** the *pipeline machinery* is verified against official sources and your own installation; the *sizing rules and acceptance thresholds* are my defaults awaiting your engineering review. You are the aerodynamicist here — §4 is your review checklist.

---

## 5. Verify any of this yourself (copy-paste)

```bash
# The official motorBike schemes (bounded divergence family):
grep -A6 divSchemes /opt/openfoam10/tutorials/incompressible/simpleFoam/motorBike/system/fvSchemes

# gmshToFoam creates generic 'patch' types (no wall heuristic):
grep -n "polyPatch::typeName" /opt/openfoam10/applications/utilities/mesh/conversion/gmshToFoam/gmshToFoam.C

# Wall functions demand wall-type patches:
grep -n -A8 "checkType" /opt/openfoam10/src/MomentumTransportModels/momentumTransportModels/derivedFvPatchFields/wallFunctions/nutWallFunctions/nutWallFunction/nutWallFunctionFvPatchScalarField.C

# simpleFoam's -postProcess mode:
source /opt/openfoam10/etc/bashrc && simpleFoam -help | grep -A2 postProcess
```

Official pages (all fetched and confirmed live on 2026-07-10):
- OpenFOAM v10 User Guide, numerical schemes: https://doc.cfd.direct/openfoam/user-guide-v10/fvschemes (§4.5.3 bounded divergence, §4.5.5 `limited corrected 0.33`)
- OpenFOAM v10 User Guide, solution control: https://doc.cfd.direct/openfoam/user-guide-v10/fvsolution
- OpenFOAM v10 User Guide, boundaries: https://doc.cfd.direct/openfoam/user-guide-v10/boundaries
- OpenFOAM v10 forceCoeffs source docs: https://cpp.openfoam.org/v10/classFoam_1_1functionObjects_1_1forceCoeffs.html
- Gmsh reference manual: https://gmsh.info/doc/texinfo/gmsh.html (§1.2.1 algorithms; mesh size fields; MshFileVersion)
- OpenVSP API docs: https://openvsp.org/api_docs/latest/

## 6. Going forward

Per your request: from now on, when I make a physics/numerics decision in this project, I will either (a) cite the official documentation section for it in the accompanying notes, or (b) state outright that it is engineering judgment / training knowledge with no document behind it — as done in §4 above.
