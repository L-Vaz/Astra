# Astra Stage-1 limitations ledger — design spec

Status: proposed (2026-07-25). Companion to
[`plans/2026-07-25-stage1-hardening.md`](../plans/2026-07-25-stage1-hardening.md),
which owns the ordered work items and their tests. **This document owns three things
the plan deliberately does not carry:**

1. the **model-fidelity ledger** — every constant and model in the Stage-1 physics
   core, its status (cited / engineering judgment / known-biased / not modelled), its
   measured sensitivity or bias, and the exact wording the user must be shown;
2. the **honesty-vs-defect-vs-capability taxonomy** and the decision rules that put a
   finding in one bucket rather than another;
3. the **golden-update policy** — the procedure that makes a physics change a decision
   instead of an accident.

Nothing here restates a work item. Every ledger row names the plan item that acts on it.

## Why this document exists

`CLAUDE.md` carries one MUST-preserve rule that Stage 1 currently violates in at least
nine places:

> Cite physics and numerical choices to official sources or label them explicitly as
> engineering judgment.

`domain_packs/bwb/aero_estimates.py:24` opens a block headed
`# --- declared engineering constants (each echoed into assumptions) ---`, which is the
right intent. But four of the five constants under it carry a single-clause comment and
no source, one comment (`:29-31`) asserts a calibration that inverting the numbers
disproves, and one field description (`:74`) names a load distribution the formula does
not compute. The pack also mixes rigor levels: `AtmosphereState`
(`domain_packs/bwb/atmosphere.py:53-117`) validates finiteness, positivity, and
cross-consistency, while `AeroEstimate` in the same pack accepts `oswald_e=99`.

A ledger is the cheapest durable fix. It gives every disclosure string one reviewed
home, so the same sentence is not re-invented in `aero_estimates.assumptions`, the
cockpit ⓘ chips, and a docstring — and it gives the golden-update policy something
concrete to require.

## Scope

The Stage-1 tier-0 equation core and the deterministic path around it:

- `domain_packs/bwb/aero_estimates.py` — every L/D, CD0, Oswald e, CLmax, stall speed,
  and neutral point the user sees.
- `domain_packs/bwb/variants.py` — the five lens concepts and their provenance text.
- `domain_packs/bwb/inverse.py`, `domain_packs/bwb/intake.py`,
  `domain_packs/bwb/schema.py`, `domain_packs/bwb/requirements.py`.

Out of scope: tier-1 VSPAERO, tier-2 OpenFOAM, the AeroSandbox MDO tier (its own spec,
`2026-07-21-mdo-parametric-optimizer-design.md`), and the inherited LangGraph spine.

## Taxonomy and decision rules

Every finding lands in exactly one bucket. The bucket decides who fixes it, how
expensive it is, and what its test asserts.

| Bucket | Rule | Test asserts |
| --- | --- | --- |
| **DEFECT** | The output is wrong, or a documented contract is broken. A correct fix changes a number, a status, or an HTTP code. | the corrected value or invariant |
| **HONESTY** | The computation is defensible at this tier, but a string, flag, field description, or docstring states something the computation does not support. A correct fix changes no number. | the string/flag, **plus** a consistency check that the claim matches the computation |
| **CAPABILITY** | The user reasonably expects a behaviour that does not exist. A correct fix is new modelling and needs a source. | an `xfail(strict=True)` placeholder, plus a "not modelled" disclosure test that holds meanwhile |

Three rules resolve the ambiguous cases:

**Rule 1 — a claim is honesty, a computation is a defect.** `easy_launch`'s
`max(5.5, 0.8 * baseline)` floor (`variants.py:130`) raising the wing loading is a
defect. The unconditional sentence
`"Wing loading set to 5.5 kg/m^2 (below baseline 2.6)"` (`variants.py:132-134`) is a
separate honesty item, and it ships first because it is a conditional and it cannot
regress a number.

**Rule 2 — where the label and the formula disagree, change whichever is not load-bearing.**
`root_bending_index_nm` computes `W*b/8` (uniform load) while claiming "elliptic"
(`aero_estimates.py:74`, `:220`). Every candidate comparison and every tradeoff sentence
already rests on `W*b/8`; the label rests on nothing. Change the label. Contrast
`skin_friction_coeff`'s laminar-Re bug (`:113-118`), where the *formula* is what
downstream L/D depends on being right — so the formula changes and the goldens move.

**Rule 3 — a disclosure is never a substitute for a fix; it is a cheaper, earlier,
independent deliverable.** Every honesty row below names the capability or defect item
that would actually resolve it. A row with no such item is a row where the tier-0
behaviour is genuinely correct and only the wording was wrong.

## Model-fidelity ledger

Statuses: **CITED** (traceable to a named published method) · **JUDGMENT** (an Astra
engineering choice, no source) · **BIASED** (correct method, measured systematic error)
· **NOT MODELLED** (the quantity does not respond to a driver the user will assume it
does).

Percentages and case counts are probe-measured over 596,700 legal geometries /
117,600 flight cases unless noted.

### Drag

| # | Item | Anchor | Status | Measured | Plan item |
| --- | --- | --- | --- | --- | --- |
| L1 | Turbulent flat-plate Cf `0.455/(log10 Re)^2.58` | `aero_estimates.py:116` | **CITED** — but **misattributed** to Schlichting; it is **Raymer eq. 12.27**. Schlichting's own mean-Cf form is 17.6–19.3% lower. | — | S6-F2 |
| L2 | Laminar Cf evaluated at full-chord Re instead of `Re_transition = 0.30·Re` | `aero_estimates.py:113-118` | **BIASED** — understates the laminar segment by `1/sqrt(0.30) = 1.83×`. | CD0 optimistic **−12.4%** at Re 2e5, −10.1% at 5e5, −8.5% at 1e6, −7.1% at 2e6. Every L/D inherits it. | S6-F2 |
| L3 | `max(re, 1e4)` Reynolds floor | `aero_estimates.py:115` | **JUDGMENT**, and it **launders invalid input**: Cf is exactly flat at `0.012892296409079974` for Re = 9999, 1000, 100, 1.0, 0.0 **and −5.0**. The floor value is **3.26× below** true Blasius at Re 1e3 — non-conservative. | — | S6-F2, S6-F9 |
| L4 | `LAMINAR_FRACTION = 0.30` | `aero_estimates.py:25` | **JUDGMENT** — one clause of justification ("smooth small-UAV wing at Re ~5e5-1e6"), no source. | 0.30 → 0.00 moves CD0 **+22%** and `ld_max` **−11%**; → 0.60 moves `ld_max` to 19.410. | S3-F12 |
| L5 | `INTERFERENCE_MARGIN = 1.10` | `aero_estimates.py:26` | **JUDGMENT** — the **only** allowance for prop, nacelle, gaps, camera, antennas, and servo horns. | The pack's maximum CD0 is **0.0233**, while measured small-UAV flying wings sit at **0.020–0.035** — the whole predicted band is at the optimistic edge. | S3-F12 |
| L6 | Hoerner form factor, Raymer wetted-area fit `1.977 + 0.52·t/c` | `aero_estimates.py:121-123`, `:147` | **CITED** | — | — |
| L7 | `mean_tc`'s invented `t_mid = 0.5·(t_root + t_tip)` | `aero_estimates.py:93` | **JUDGMENT** — an interpolation choice presented as a computed quantity; `vsp_builder.py:73` uses the same midpoint, so at least the tiers agree. | — | S3-F12 |
| L30 | CD0 is blind to the aerofoil, and the two drag models disagree by 37% | `section_drag.py` (new) vs `aero_estimates.parasite_drag_coeff` | **ADDED, POLAR DELIBERATELY UNCHANGED (S1-E, 2026-07-28).** Strip theory over the Schrenk loading using the committed NeuralFoil CD ladder: each station at its own local cl and chord Reynolds. The wetted-area chain sees the section only through t/c — measured, swapping the tip mh60→e474 moves the strip integral **+8.3%** while the correlation barely moves. | Reference wing, cruise Re: strip profile CD **0.01032** at cruise CL, **0.00990** at zero lift, against Raymer CD0 **0.01360** — **ratio 1.375**, and Raymer's own lumped interference margin is only **1.10**, so interference does not explain it. Substituting (with the S1-G planar induced term) would take cruise L/D **15.93 → 20.16, +27%**. Caveats reported as fields: **66.6%** of the span is clamped to the ladder floor in the zero-lift comparison, and the ladder's own median interpolation error is **5.04%** (p90 16.52%). | S1-E — **open**: CFD (tier 3) settles which model is right |

### Induced drag / span efficiency

| # | Item | Anchor | Status | Measured | Plan item |
| --- | --- | --- | --- | --- | --- |
| L8 | Raymer straight/swept Oswald fits, blended 0–30° | `aero_estimates.py:126-138` | **CITED** — coefficients are correct transcriptions. | Blend **is** continuous at 0° and 30° (9e−9 at the breakpoint) but **C1-discontinuous**: slope jumps 2.4× at AR 8. | — |
| L9 | The `max(0.5, min(e, 0.95))` clamp | `aero_estimates.py:138` | **BIASED / hides a model failure** — the clamp is where 23% of the design space lives, and it **conceals** unclamped values of `e > 1` (1.123 at AR 2.5). | **23%** of the legal (AR × effective-LE-sweep) space is pinned at a clamp (11.7% at 0.500, 11.2% at 0.950) with **zero gradient** in both AR and sweep. At AR 10, every effective sweep above **25.95°** returns identical 0.500. | S6-F3 |
| L10 | Wrong-sign sweep response below the fit crossing | `aero_estimates.py:128-137` | **BIASED** — the straight- and swept-wing fits cross at **AR = 4.800**, so for **AR 3.6–4.7** Oswald e *increases* with sweep (AR 4.0: 0.9426 at 6.2° → 0.9462 at 12°). | — | S6-F3 |
| L11 | `sensitivity.py` differentiates a clamped quantity | `domain_packs/bwb/sensitivity.py` | **DEFECT** — finite-difference derivatives are computed through L9's zero-gradient plateaus. | — | S6-F3 |
| L29 | Planar span efficiency now computed, and the published validation point does **not** reproduce | `lifting_line.py` (new) | **ADDED, POLAR DELIBERATELY UNCHANGED (S1-G, 2026-07-28).** Prandtl lifting line solved directly: δ = Σ n(Aₙ/A₁)², e = 1/(1+δ). Monotone, unclamped, e ≤ 1 by construction — none of L7-L9's wrong-sign band, impossible e > 1, or 20.7% zero-gradient plateau. Reported beside `oswald_e`, never substituted into `cd_cruise`. | Elliptic chord → **δ = 4.8e-33** (the one analytic case, to machine precision). Taper optimum recovered at **0.40** (δ 0.0084 at AR 5.83), worst rectangular (0.0466). Converged to **1e-5** from N = 10 to 160. **The plan's target "AR 5.83, taper 0.5 → δ 0.0557" gives δ 0.0112 here** — 0.0557 corresponds to taper ~1.05 or AR ~22.5. Hypothesis: a whole-aircraft **Oswald** e (viscous + interference, 0.75-0.85) is not a planar **span** e (0.98-0.99). Reference wing: **0.978 inviscid against 0.816 from Raymer**. Substituting would cut induced drag ~20% and inflate every L/D. | S1-G — **open**: needs a sourced viscous lift-dependent term before the polar can move |

### Sweep conventions

| # | Item | Anchor | Status | Measured | Plan item |
| --- | --- | --- | --- | --- | --- |
| L12 | `effective_le_sweep_deg` averages **angles**; `effective_c4_sweep_deg` averages **tangents** | `aero_estimates.py:97-100` vs `:103-110` | **DEFECT** — two mutually contradictory definitions. By Jensen's inequality the quarter-chord sweep **exceeds** the LE sweep, which is geometrically impossible for the aft-tapering planforms the schema guarantees. One definition feeds Oswald e (`:198`), the other feeds CLmax (`:154`). | **50,817 / 596,700 (8.5%)** of legal geometries. Worked case `span 1.8, cr 0.25, cbf 0.45, mcf 0.85, taper 0.7, sweeps 60/40` → LE 49.000, c/4 **50.493** (+1.49°), while both per-panel values are correctly below their LE sweep. | S6-F4 |
| L13 | `tan(Λc/4) = tan(Λ_LE) − 0.25(cr−ct)/half_width` | `aero_estimates.py:105`, `:108-109` | **CITED** — the per-panel relation is derived correctly. The defect is only in how the two panels are combined (L12). | — | — |

### Stall and launch

| # | Item | Anchor | Status | Measured | Plan item |
| --- | --- | --- | --- | --- | --- |
| L14 | ~~`CL_MAX_2D = 1.30` "NACA 4-series at Re ~7e5"~~ | *deleted* | **RESOLVED (S1-C2, 2026-07-28).** The constant is gone. Section CLmax now comes from the committed NeuralFoil screen at each station's own chord Reynolds. | Wing CLmax now spans **0.945 → 1.160** across weight 10 → 120 N where it was previously bit-identical; reference **1.0897** against the old **1.1208** (−2.78%). | S6-F5, S7-CAP-4 (both xfails retired) |
| L15 | ~~`CL_MAX_3D_FACTOR = 0.90` finite-wing knockdown~~ | *deleted* | **RESOLVED (S1-C2, 2026-07-28).** The 0.90 stood in for "the wing does not stall everywhere at once". That is now the computed result of the Schrenk solve, not an input. | Reference stall station **η 0.703, outboard**. | S1-C2 |
| L15a | `cos(Λ_c/4)` retained on top of the Schrenk result | `aero_estimates.py:cl_max_3d` | **JUDGMENT, separately labelled.** NACA TM-948 is a straight-monoplane derivation with **no sweep term anywhere** (p. 2-5). The cosine is a patch over the method, applied to **aft** sweep only (L17). It is the weakest link in the CLmax result and is stated as such on the card. | — | S1-C2 |
| L15b | Section CLmax interpolated **as a value** between the three stations | `spanwise.py:cl_max_at` | **JUDGMENT / MODELLING CHOICE.** TM-948 assumes one aerofoil throughout and says nothing about a spanwise-varying section. Blending coordinates and re-running NeuralFoil per station would be more faithful and far too slow inside the sizing loop (0.78 ms/solve today). | — | S1-C2 |
| L16 | ~~`cl_max_3d` ignores Reynolds, thickness, and washout~~ | `aero_estimates.py:cl_max_3d`, `spanwise.py` | **RESOLVED (S1-C2, 2026-07-28).** All three now enter: Reynolds per station, thickness through the section's own screened data, washout through Schrenk's basic loading. `stall_solution` additionally iterates CLmax ↔ Reynolds ↔ V_stall to a fixed point, because a wing stalls at its stall speed and not at cruise. | Cruise-Re CLmax **1.2057** vs self-consistent **1.1333** — **6% apart**; ignoring it would repeat this row's own error at aircraft level. | S6-F5, S7-CAP-4 |
| L17 | `math.cos(sweep)` is **even**, so forward sweep is penalised like aft sweep | `aero_estimates.py:154-155` | **DEFECT** — Raymer's `cos(Λc/4)` knockdown is an **aft-sweep** correlation. | Effective c/4 sweep goes negative in **5.4%** of the envelope (min **−31.03°**). Reproducing geometry, legal at AR 2.583: `span 0.8, root_chord 1.2` (chord 1.5× span), `cbf 0.12, mcf 0.35, taper 0.15, sweeps 15/5`. | S6-F5/F6 |
| L18 | `LAUNCH_VSTALL_MAX = {hand: 10.5, catapult: 13.0, runway: 16.0}` claimed "calibrated so `defaults.WING_LOADING` sits at the boundary" | `aero_estimates.py:29-31` | **JUDGMENT presented as a derivation — the claim is false.** Inverting each pair gives implied CLmax **1.0169 / 0.9477 / 0.8133** — spread 0.2036, and none equals the code's own cap **1.1700**. Three independent guesses. | — | S3-F8 |
| L19 | An unknown launch string silently disables the gate | `aero_estimates.py:208`, `:236` | **DEFECT** — `LAUNCH_VSTALL_MAX.get(launch)` → `None` → `launch_ok=None`. `estimate(..., launch="glider")` returns `limit=None, ok=None` with `v_stall 24.71`, a value that fails all three real limits. `MissionSpec.launch` is a `Literal` (`schema.py:26`) but `estimate()`'s parameter is `Optional[str]`. `:236`'s truthiness test would also skip a limit of `0.0`. | — | S6-F10 |
| L20 | Cruise beyond stall is neither flagged nor bounded | `aero_estimates.py:200` vs `:206`; `defaults.MAX_CRUISE_CL = 0.75` applied only in `defaults.derive()` | **DEFECT** — `AeroEstimate` has no `cruise_feasible` / `stall_margin` field; `variants.py:214` checks only `launch_ok is False` and `variants.py:205` ranks on `ld_cruise`, so infeasible points compete as equals. | `cl_cruise > cl_max_3d` in **26,238 / 117,600 (22.31%)**, identically `v_ms < v_stall_ms`. `cl_cruise` reaches **136.43** (p99 20.74). Worst case reports `launch_ok=True`. | S6-F1 |
| L25 | Spanwise twist was reckoned **geometrically**, ignoring that each section has its own zero-lift angle | `spanwise.py:stations` | **DEFECT, fixed (S1-D, 2026-07-28).** TM-948 p. 2 reckons all angles "from the zero lift direction". With one aerofoil that is one direction; with three it is three. A cambered tip on a symmetric root is aerodynamic **wash-in** at zero geometric twist. | On the reference wing the s5010 tip sits at **α_L0 = −1.35°** against **0.00°** for the symmetric e474 root, so **1.35° of the 4.0° geometric washout is cancelled by camber — 34%**. Reference: CLmax **1.0897 → 1.0659** (−2.19%), V_stall **10.538 → 10.655 m/s** (+1.11%), stall station **0.647 → 0.703**, and **`launch_ok` flips True → False** (10.655 > the 10.5 m/s hand limit). Before the fix the reference appeared to reach an inboard stall at −5° of washout; it reaches one **nowhere** in the schema range. | S1-D |
| L26 | The browser mirror ran the deleted CLmax constants for one commit | `static/aero_mirror.js`, `static/spanwise.js` | **RESOLVED (S1-C2, 2026-07-28).** Schrenk is ported to JS and parity-tested against a Python-generated fixture; the server ships the three selected sections' Reynolds curves (~30 numbers) rather than the 2.33 MB table. Missing curves render **null**, never a fallback constant. | Divergence before the port: browser **1.1208** vs server **1.0897** (+2.86%), and up to **+66.4%** on a small-span high-sweep geometry (0.9011 vs 0.5414). | S1-C2 |
| L27 | Stage 1 could not say whether a concept **trims itself** | `trim.py` (new), `aero_estimates.AeroEstimate` | **ADDED (S1-D, 2026-07-28).** Strip-theory moment about the CG: section Cm0 about each quarter chord, plus local lift through the swept arm, on the Schrenk loading. Cm is **affine in tip twist and linear in CG**, so both inversions are exact rather than iterative. Washout-only per the maintainer's ruling — no elevon, no schema change. | Reference wing at the 8%-margin CG: **Cm = −0.0275** nose-down, needing **−11.55°** of tip twist against a **−7°** schema floor. The burden is **geometric, not camber**: lift-times-arm **−0.0263** against **−0.0012** from the sections. Trimming by CG instead leaves **0.58%** static margin. A swept + reflexed configuration (45/20°, esa40/mh110) **does** trim, at −2.25°. | S1-D |
| L28 | `CM_TRIM_TOLERANCE = 0.005` | `trim.py` | **JUDGMENT.** Roughly one degree of tip twist at the reference wing's dCm/dtwist of 0.0032/deg — finer than a 9°-wide schema field or a builder can set. Moves only the `trims_as_built` boolean, never `cm_trim_residual` or `tip_twist_for_trim_deg`. | — | S1-D |

### Stability and structures

| # | Item | Anchor | Status | Measured | Plan item |
| --- | --- | --- | --- | --- | --- |
| L21 | `neutral_point_m` is an **area**-weighted planform centroid | `aero_estimates.py:158-177` | **BIASED, safety-relevant** — sound implementation (passes sign, positivity, and all three monotonic physics directions: aft with more sweep, aft with less taper, `x_np > 0` in all 596,700 cases) but it ignores that outer panels carry more lift per unit area than the low-aspect centre body. That biases `x_np` **forward** → static margin **optimistic** → the CG advice is **unconservative** on the one number that is safety-critical for a tailless aircraft. The docstring (`:160-161`) says VSPAERO supersedes it but **never states the bias direction**. | `x_np` lands outside `(0, root_chord)` in **6.8%** of the envelope (up to 2.48× the root chord, `x_np/MAC` up to 3.398), with no check on whether payload can physically sit there. | S6-F13 |
| L22 | `STATIC_MARGIN_TARGET = 0.08` declared **twice** | `aero_estimates.py:32` and `MissionDerived.static_margin_target`, `schema.py:130` | **JUDGMENT, duplicated** — equal today, independently declared, free to drift silently. | — | S3-F12 |
| L23 | `root_bending_index_nm` labelled "elliptic half-span load", computes `W·b/8` | `aero_estimates.py:74` (field description), `:220` (user-facing assumption), `:239` (formula) | **HONESTY** — `W·b/8` is the **uniform**-load result; the elliptic result is `W·b/(3π)`. The code is **17.8% high** relative to the loading it claims. Per Rule 2, the formula stays and the label changes. | — | S3-F7 |
| L24 | `roll_damping_index = span²` | `aero_estimates.py:241` | **JUDGMENT**, and honestly labelled "relative" in its own field description (`:76`). No action. | — | — |
| L31 | Effective dihedral was not computed at all; a swept wing supplies it silently | `dihedral.py` (new), `aero_estimates.effective_c2_sweep_deg` | **ADDED, REPORTED NOT OPTIMISED (S1-H, 2026-07-28).** McCormick, *Aerodynamics, Aeronautics and Flight Mechanics*, Wiley 1979, **p. 547**, via Kuhlman, *RC Soaring Digest* Jan–Mar 2000, PDF pp. 15–18 (read this session): `Cl_β = −[(1+2λ)/(3(1+λ))]·CL·tan(Λ_c/2)`. New half-chord sweep helper, tangent-averaged like its neighbours — the equation is written at the **half** chord and the repo previously had only LE and c/4. | **Unit reconciliation, done before implementing:** the equation is **per radian**, the article's `Cl_β = −0.00021 ≈ 1° of dihedral` is **per degree**; 57.2958 applied once. Verified two ways — the per-radian reading puts Graph 3/4's top-right corner (Λ 30°, CL 1.2) at **26.4°/28.8°**, just under both charts' 30° axis, where the per-degree reading gives **1512°**; and the folk rule the article disputes ("3° sweep ≈ 1° dihedral") comes out exact at **CL 0.50/0.46**, ordinary cruise values, versus CL 0.0087 under the wrong reading. Reference wing: **5.08° at cruise → 8.95° at CLmax**, geometric 3.00 constant. Envelope **−5.88° to +49.91°**, sign flips included. Reference sweeps LE 23.27 / c4 16.67 / **c2 9.58** — using c/4 by mistake would have inflated the increment **1.7×**. | S1-H |
| L32 | ~~Lock toggles are display-only~~ | `inverse.LockedValues`, `intake.to_locked` | **RESOLVED (S1-F, 2026-07-28).** `span_max`, `s_ref` and `mtow_cap` now constrain the sizing solve. A lock is applied AND THEN VERIFIED against the delivered geometry, because `_feasible_planform`'s schema repairs are free to move area and span for their own reasons — checking the result is the only guarantee a future change to those repairs cannot bypass. An unhonourable lock returns a `CapViolation`; it never silently unlocks. The L/D loop is **skipped** under a geometry lock (it works by trading area against AR, which a lock removes) and says so in a note. | Locking every editable row moves wing loading **7.064 → 3.519 kg/m²** and area **0.4714 → 0.9378 m²**, where the two were previously byte-identical. Span locks at 1.2/1.5/2.0 m honoured to <1 mm; a 2.5 m lock reports a violation at 2.1795 m, blocked by the AR gate. A 0.30 m² area lock against a mission needing 0.4750 m² is honoured with the unmet stall requirement named. **Retires the S3-E1 xfail** (backlog 11 → 10). | S1-F, S3-E1, S7-CAP-3 |
| L33 | No artifact carried Stage 1's conclusions, or their evidence, to Stage 2 | `handoff.py` (new), `GET /api/handoff/{session}` | **ADDED (S1-I, 2026-07-28).** Versioned pydantic model: geometry, flight condition, requirements + which are unmet, locks + which could not be honoured, the tier-0 panel, and an **evidence row per exported quantity** carrying its grade, source and caveat. | 11 graded quantities. The three the polar actually uses — `cd0`, `oswald_e`, `x_np_m` — are graded **[D]** and travel with their caveats, because Stage 1 has better-evidenced alternatives for all three and deliberately did not swap them in. Three `known_disagreements` travel too. Provenance pins the airfoil table SHA-256 and the atmosphere dataset. | S1-I |

### Invariants that hold — the regression floor

Measured with **zero violations and zero NaN** across 117,600 cases. These are the
contract every physics change must preserve; they are not defects and must not be
"fixed".

| # | Invariant | Anchor |
| --- | --- | --- |
| L25 | `ld_cruise <= ld_max` | `aero_estimates.py:204`, `:231` |
| L26 | `cl_star <= cl_max_3d` | `:203`, `:206` |
| L27 | `endurance_fom == cl_cruise**1.5 / cd_cruise` | `:233` |
| L28 | `x_cg_for_target_sm_m == x_np_m - 0.08 * mac_m` | `:238` |
| L29 | `x_np_m > 0` | `:210` |

Pinned by `tests/test_bwb_aero_invariants.py` (plan item S0-9) and, after S6-AE1, by
`AeroEstimate`'s own model validator. Note the deliberate exclusion:
`cl_cruise <= cl_max_3d` is **not** an invariant — 22.31% of the legal envelope violates
it (L20), so it is a flag, never a rejection.

### Lens intent — what the five cards actually deliver

Measured over 2,880 conditions. Direction words in `tradeoff_sentences` are **clean** (a
41-point sweep found zero wrong-direction words) — that part of the code is correct and
should be left alone.

| # | Card | Claim | Reality | Plan item |
| --- | --- | --- | --- | --- |
| L30 | `easy_launch` — "Lowest stall speed" | `variants.py:77-83` | The `max(5.5, …)` floor (`:130`) **raises** loading whenever baseline loading is below 6.875 kg/m² — **64.1%** of conditions. Loading higher than baseline in **31.8%**; `v_stall` worse than baseline in **24.0%**; **highest `v_stall` of all five in 21.6%**. Worst: mtow 0.8, alt 4000, v 10, span cap 1.2 → `v_stall` 8.20 vs baseline 5.40. | S6-G1 |
| L31 | `easy_launch` provenance — "(below baseline …)" | `variants.py:132-134` | Printed **unconditionally**; verified live output `"Wing loading set to 5.5 kg/m^2 (below baseline 2.6)"` — false in **56.0%** of conditions. The card's own tradeoff sentence then contradicts its title: `"stall +2.0 m/s (8.2 m/s — harder launch)"`. | S3-G1h |
| L32 | `max_range` — "Cruise at the best-L/D lift coefficient" | `variants.py:64-66`, `:167-169` | Reaches CL* within 2% in only **16.2%**; median `\|CL−CL*\|/CL*` **0.612** vs baseline's 0.613. Mostly physics (an area floor binds in 2516/2880; in the 364 unconstrained conditions the fixed point is exact to 2e−5) — but the objective string is unqualified and the provenance asserts the sizing as fact, with the retraction in a *separate* `constraint_notes` entry that **nothing tests for**. | S3-G2h |
| L33 | `max_range` vs `span_efficiency` ranking | `variants.py:62-76` | A user picking "Max range" gets **lower** cruise L/D than "Span efficiency" in **39.1%** of conditions (AR 8.5 beats sizing-to-CL* at AR 6.5) — the lens ranking is inverted across a large slice of the envelope. | S6-G2 |
| L34 | `span_efficiency` — "at the same wing loading", "pay for it in span" | `variants.py:70-76`, `:136-138` | Broken in **14.8%** overall and **50.0%** at mtow 10 kg, because `_feasible_planform` (`inverse.py:133-192`) moves the area *after* the provenance line printed the pre-repair numbers as fact. "Pay for it in span" is false in **33.8%** (span *smaller* than baseline). It does not own induced drag either: `easy_launch` beats it in **57.3%**. | S3-G3h, S6-G2 |
| L35 | `control_margin` — concedes only "the L/D cost" | `variants.py:84-90` | Has the **highest `v_stall` of all five in 65.9%** of conditions. | S3-G4h |
| L36 | Five cards, fewer designs | `variants.py:259-297` | Clustering by sizing (span within 1e−4 m, area within 1%): **1 distinct sizing in 288/2880 (10.0%)**, all at `span_cap = 1.2`; 2 in 2.7%; 3 in 5.7%. At `span_cap = 1.2`: 40.0% of conditions have all five at identical AR, 56.1% at identical span, 27.1% with all five cruise L/D within 2%. Concrete: mtow 0.8, alt 4000, v 10, span_cap 1.2 → baseline/max_range/span_efficiency/control_margin all at S = 0.5754, b = 1.200, AR = 2.50, L/D 11.75–11.99 — **four cards, one design**. Worst pair: `baseline\|max_range` share sizing in 47.6%. **Cluster on sizing, not on the 12-parameter vector**: the full-parameter distance never drops below 0.15 (sweep/thickness/twist always differ), so it cannot detect the collapse. | S3-G5, S6-G5 |
| L37 | Dihedral | `variants.py:58` | **2.0° in all five concepts** — every `_Lens` inherits the default. The gull-wing / dihedral-distribution shape that motivated the whole design is unreachable in Stage 1. | S7-CAP-2 |

### Intake and requirement fidelity

| # | Item | Anchor | Status | Plan item |
| --- | --- | --- | --- | --- |
| L38 | Six of twelve editable rows drive **zero** computation | `intake.py:200-208` and the `mission_type` / `endurance` / `regulatory` rows | **NOT MODELLED** — editing each leaves all seven derived values bit-identical. The rows that do drive math: `payload`, `altitude`, `launch`, `span_max`, `v_stall_max`, `mtow_cap`. The `endurance` row already half-admits it (`intake.py:276`). | S3-E2 |
| L39 | Custom rows are recorded and never constrain anything | `intake.py:326-333` | **NOT MODELLED** — and the info string promises they are "passed to Stage 2 as a constraint when recognised", which nothing implements. | S3-E3, S7-CAP-5 |
| L40 | Lock toggles are inert | `intake.py:238` (the only write); no reader outside display code | **NOT MODELLED** — all-fixed and all-variable derive byte-identical values. | S3-E1, S7-CAP-3 |
| L41 | Driver chips name rows that cannot move the number | UI ⓘ metadata | **HONESTY** — `static_margin`'s listed drivers are `wind` and `mission_type`, but it is the hardcoded constant `aero_estimates.py:32`. `clmax`'s listed driver is `airfoil`, but `cl_max_3d = 1.30 · 0.90 · cos(sweep)` responds **only** to sweep. | S3-E4 |
| L42 | Clamping is silent on both paths | `intake.py:353-371`, `src/cockpit/server.py:1114-1118` | **DEFECT + HONESTY** — no `warnings` or `clamped` field exists; the row keeps the typed value and the verdict still reads "feasible". Ten probed divergences, including the sign flip altitude **−500 → +500** and span **100 m → 3.0 m**. | S3-D2 |
| L43 | Units are converted in the brief path but not the edit path | `intake.py:254-257` vs `:320` | **DEFECT** — the same string means different things depending on how it arrives. | S2-D1 |

## Required disclosure wording

One reviewed sentence per limitation, reused verbatim in
`aero_estimates.estimate()`'s `assumptions` list (`:213-224`), the matching pydantic
field description, and the cockpit ⓘ text. The plan's honesty items land these strings;
their tests assert them.

| Ledger row | Wording |
| --- | --- |
| L1, L2 | `"Mixed flat-plate skin friction (Raymer eq. 12.27 turbulent + Blasius laminar), {LAMINAR_FRACTION:.0%} laminar run assumed at the transition Reynolds number"` |
| L4 | `"CD0 assumes a {LAMINAR_FRACTION:.0%} laminar run — engineering judgment. With no laminar run, CD0 would be ~22% higher and best L/D ~11% lower."` |
| L5 | `"+{INTERFERENCE_MARGIN-1:.0%} interference margin is the only allowance for prop, nacelle, gaps, camera, antennas and servo horns — engineering judgment, and at the optimistic edge of measured small-UAV flying wings (CD0 0.020-0.035)."` |
| L9, L10 | `"Oswald e = {e:.3f} from Raymer fits (AR {ar:.2f}, effective LE sweep {sweep:.1f} deg)"` — and when clamped, append `" — at the {floor|ceiling} of the fit's valid range, so e is insensitive to AR and sweep here."` |
| L16 | `"Section CLmax {CL_MAX_2D} (NACA 4-series, Re~7e5) x {CL_MAX_3D_FACTOR} finite-wing x cos(aft sweep). Independent of Reynolds number, thickness and washout — so stall speed and the launch check are too."` |
| L18 | `"Launch '{launch}' tolerates stall up to {limit} m/s — engineering judgment, not derived from the pack's wing-loading table."` |
| L20 | When `cruise_feasible` is False: `"Requested cruise {v:.1f} m/s is BELOW this wing's stall speed {v_stall:.1f} m/s — this design cannot fly the requested cruise point."` |
| L21 | `"Neutral point = area-weighted planform quarter-MAC. It ignores the outer panels' higher lift per unit area, which biases x_np forward and the static margin OPTIMISTIC — treat the CG position as a lower bound on required nose-heaviness until VSPAERO confirms it."` |
| L23 | `"Root bending proxy W*b/8 — uniform half-span load, a conservative comparative index. Not elliptic loading (that value is W*b/(3*pi), ~18% lower). No structural sizing."` |
| L38, L39, L40 | `"Recorded only — not evaluated at this tier."` |
| L42 | `"You typed {typed}; using {used} ({limit_name})."` |

## Golden-update policy

The Stage-1 physics core has no committed reference values today, which is why a
12% CD0 bias survived unnoticed. The plan pins a 22-field reference panel plus component
goldens (items S0-7, S0-8). This policy is what gives them force.

**The reference case** (probe-measured, exact):
`BWBParams(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28, mid_chord_frac=0.6,
taper_outer=0.4, sweep_inner_deg=35, sweep_outer_deg=18, root_thickness_pct=14,
tip_thickness_pct=10, root_twist_deg=1.0, tip_twist_deg=-3.0, dihedral_deg=3.0)` at
`atmosphere_at(0.0)`, `v_ms=18.0`, `weight_n=34.335`, `launch="hand"`.

**Rules.**

1. Goldens live in `tests/data/aero_golden.json` at full repr precision and are compared
   at `rel=1e-12`. A tolerance that loose enough to absorb a physics change is not a
   golden.
2. `verification/verify_aero_goldens.py --update` is the **only** way to change them, and
   it may be run only in the same change as a new ledger row (or an amendment to an
   existing one) naming: the physical reason, the source or the explicit judgment label,
   and the measured magnitude of the shift.
3. A pull request containing a golden diff without a ledger entry is rejected. No
   exceptions for "small" diffs — L2's 12% bias would have looked like a small diff at
   any single Reynolds number.
4. Regenerating goldens obliges the author to re-check every threshold calibrated
   against the old values: `defaults.MAX_CRUISE_CL`, the MDO reference score recorded in
   `plans/2026-07-21-mdo-parametric-optimizer.md`, and
   `verification/verify_asb_aero.py`'s documented trend tolerances (L/D ±25%,
   α_trim ±2°, CL_alpha ±20%, x_np ±10% MAC).
5. The five invariants (L25–L29) are **never** regenerated. If a change breaks one, the
   change is wrong, not the invariant.

### Golden-update ledger

| # | Date | Change | Physical reason | Source | Measured shift on the reference case |
| --- | --- | --- | --- | --- | --- |
| G1 | 2026-07-26 | Laminar Cf is evaluated at the **transition** Reynolds number (`0.30·Re`) instead of at the full-chord Re (S6-F2a) | Blasius Cf ∝ 1/√Re, so evaluating the first 30 % of the chord at the trailing-edge Re understated that segment by 1/√0.30 = 1.83× | Blasius flat-plate laminar solution; transition-point evaluation is the standard mixed-flow construction (Raymer §12.5) | Cf **+14.15 %** @ Re 2e5, **+11.24 %** @ 5e5, **+9.32 %** @ 1e6, **+7.65 %** @ 2e6; CD0 **+11.85 %**; ld_cruise **−6.31 %** (17.0037 → 15.9316); ld_max **−5.63 %**; cl_star **+5.55 %** |
| G2 | 2026-07-26 | `effective_le_sweep_deg` averages **tangents**, matching `effective_c4_sweep_deg` (S6-F4) | Averaging one sweep in angle space and the other in tangent space made the quarter-chord sweep exceed the leading-edge sweep in 8.5 % of legal geometries — impossible when both panels taper aft | Geometric: the tangent mean is the sweep of the line joining the panel leading edges over the same span | LE sweep on the reference case 22.760° → 22.681°; oswald_e **−0.38 %** (0.819404 → 0.816260). On the F4 worked case LE 49.000° → 51.190°, restoring c/4 (50.493°) below LE |
| G3 | 2026-07-26 | CLmax sweep knockdown uses `cos(max(Λ_c/4, 0))` (S6-F6) | `math.cos` is even, so a forward-swept quarter chord — 5.4 % of the envelope, down to −31.03° — was penalised exactly like aft sweep, while Raymer's knockdown is an aft-sweep correlation | Raymer §12.4 (aft-sweep CLmax correlation) | No change on the reference case (aft-swept); affects only the 5.4 % forward-swept slice, where CLmax rises to the unswept value |
| G4 | 2026-07-26 | Re ≤ 0 raises instead of being floored (S6-F2c) | The `max(re, 1e4)` floor returned exactly 0.012892296409079974 for Re = 1000, 1.0, 0.0 **and −5.0** alike, so an impossible input produced a plausible Cf | — (input validation, not a model change) | The −5.0 and 0.0 rows are removed from the Cf ladder and replaced by an assertion that they raise |

Thresholds re-checked per rule 4 after G1–G4: `MAX_CRUISE_CL` (0.75) is a CL cap and is
unaffected by a CD0 change; `verify_asb_aero.py`'s L/D tolerance is ±25 % against
VSPAERO and the 6.3 % equation-tier shift stays inside it; the MDO reference score is
recomputed per run rather than pinned.

## Deliberate non-goals

- **Not replacing the Raymer Oswald fits.** The transcriptions are correct (L8); the
  problems are clamping behaviour (L9) and a wrong-sign band below AR 4.8 (L10). A
  replacement formulation valid at low AR is separate, sourced work; until then the
  limitation is disclosed and its extent is measured in the suite.
- **Not modelling propulsion, energy, or endurance.** `CLAUDE.md` scopes Astra
  aerodynamics-first; `endurance_fom` (`aero_estimates.py:61`) is the aerodynamic proxy
  that exists, and `MissionSpec.endurance_min` is documented "recorded goal only"
  (`schema.py:22-25`). L38's disclosure stands in for it.
- **Not inventing a Reynolds scaling for section CLmax.** L14/L16 need a sourced
  correlation (a Re-scaled NACA 4-series dataset, or NeuralFoil — already a dependency,
  `pyproject.toml`). Inventing one would violate the citation rule while appearing to
  satisfy it, which is worse than the current disclosed constant.
- **Not unifying the three geometry discretizations.** JS interpolates linearly between
  19 stations, OpenVSP lofts a spline over ~30 U stations with a round tip cap,
  AeroSandbox uses 3 xsecs. Bounded by a measured ≤0.31% projected-area difference; the
  bound is documented and asserted, the discretizations stay.
- **Not rewriting the JS mirror.** Probe verdict: faithful. `stage1.js` and `cockpit.js`
  station tables are bit-identical across all 6 fields × 19 stations and 2,997 preview
  floats; planform area/MAC/AR agree with `schema.py` to 0.0 error across all five
  copies. Deduplication and parity tests only.
