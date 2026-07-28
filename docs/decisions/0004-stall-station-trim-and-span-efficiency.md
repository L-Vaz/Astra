# 0004 — The browser stops guessing, twist is reckoned aerodynamically, and span efficiency is computed but not trusted

**Date:** 2026-07-28 · **Status:** accepted
**Decided by:** Lindon Vaz (physics calls are the maintainer's; the two judgment calls
recorded under "Still unproven" were taken by the agent under a standing instruction to
decide and are flagged for review)

Covers S1-C2's browser half, S1-D (washout-only trim) and S1-G (span efficiency).
[0003](0003-airfoil-section-screen.md) is the prerequisite: it put the section table in
place, and everything here reads from it.

## Problem

Three separate defects, each demonstrated before anything was written.

**1 — The browser ran deleted constants.** S1-C2 replaced `CL_MAX_2D * 0.90 * cos(Λ)`
in Python with a computed stall solve and did not touch `aero_mirror.js`. The cockpit
recomputes the whole tier-0 panel client-side so that dragging a slider is instant, so
the screen kept showing the old number:

```
browser (aero_mirror.js) CLmax : 1.1208
server  (Python, S1-C2)  CLmax : 1.0897
divergence                     : +2.86%  (browser optimistic)
server stall station           : 0.647  — browser had no equivalent
```

Sweeping the parity fixture's geometries afterwards showed the reference case was the
*mild* one — on a small-span, high-sweep planform the deleted formula read **+66.4%
optimistic** (0.9011 against 0.5414), which is a stall speed 29% too low on the number
that decides whether the aircraft can be hand-launched.

**2 — Twist was reckoned geometrically.** NACA TM-948 p. 2 reckons all angles "from the
zero lift direction". With one aerofoil that is one direction; this wing has three, and
`spanwise.py` was using raw geometric twist. Measured on the golden reference:

```
alpha_L0  root e474  -0.000 deg     geometric washout   +1.0 -> -3.0  =  4.00 deg
          mid  e472  -0.000 deg     aerodynamic washout +1.0 -> -1.65 =  2.65 deg
          tip  s5010 -1.350 deg     => camber cancels 33.7% of the washout
```

The cambered tip on a symmetric root is aerodynamic **wash-in**. The model was crediting
the wing with half again as much washout as it has, on the exact lever that decides
inboard versus outboard stall.

**3 — The Oswald fits are non-monotonic and clamped.** Carried from the limitations
ledger (L7–L9, measured previously): Raymer's straight- and swept-wing fits cross at
AR 4.799, so adding sweep reports *better* span efficiency for AR 2.50–4.79; unclamped
they return e = 1.123 at AR 2.5, a wing beating the elliptic minimum; and **20.7%** of
the (AR × sweep) grid sits on a clamp with zero gradient, which `sensitivity.py`
finite-differences as if it were physics.

## Severity and blast radius

CLmax sets stall speed, which sets the wing-area floor, which sets span, area, aspect
ratio and every drag number downstream — so 1 and 2 reach every quantity Stage 1
reports. 1 is worse than a wrong number because the two surfaces disagreed: the operator
saw one stall speed while the solver used another. 3 is latent for Stage 1's own output
(concepts land AR 5.0–8.5, above the crossing) and live for whatever the Stage-2 MDO
inherits.

Trim was not wrong, it was absent: nothing in Stage 1 could say whether a concept
balances itself, and a tailless aircraft has nothing but its own wing to do it with.

## Physics

**Stall station (unchanged from S1-C2, restated for the browser port).** Schrenk's
approximation, NACA TM-948: the real spanwise load lies between an ideal distribution
independent of wing shape and one proportional to chord, and the arithmetic mean of the
two is a usable estimate. The twist component takes the *zero line*, not the ellipse, as
its ideal — using the ellipse doubles the effect of washout.

**Aerodynamic twist.** A section's lift is `cl = cl0 + a·α`, so its zero-lift angle is
`α_L0 = −cl0/a`. Two sections with different camber have different zero-lift directions,
and the twist that matters aerodynamically is the geometric twist measured from each
station's own. This is not an addition to TM-948 — it is what p. 2 already requires,
generalised to the only case the paper does not cover.

**Trim.** Take x aft of the root LE apex, nose-up positive. Each strip carries its own
moment about its quarter chord (`q c² cm0 dy`, the point about which the moment does not
change with incidence) plus its lift acting there, a distance `(x_c4 − x_cg)` from the
CG:

```
Cm(x_cg) = (2/(S·MAC)) ∫ [ cm0(y) c(y)² − cl(y) c(y) (x_c4(y) − x_cg) ] dy
```

On a swept wing `x_c4` grows with y, so the outboard strips have the long arms — which
is why sweep substitutes for a tail, why washout is a pitch control, and why an outboard
stall pitches the nose up. `cl_add`, `cm0`, `c` and `x_c4` are all independent of twist
and `cl_basic` is affine in it, so **Cm is affine in tip twist and linear in x_cg** and
both inversions are exact, not iterative.

**Span efficiency.** Prandtl's lifting line writes the circulation as a Fourier sine
series in the angular span coordinate; the monoplane equation gives the coefficients,
and integrating lift and induced drag collapses them to `CL = πAR·A₁` and
`CDi = πAR·Σn·Aₙ²`. Comparing with `CDi = CL²/(πeAR)`:

```
δ = Σ_{n≥2} n (Aₙ/A₁)²        e = 1/(1+δ)
```

δ is a sum of squares, so δ ≥ 0 and **e ≤ 1 by construction**. The elliptic wing has
every higher harmonic zero. No clamp is possible, and there are no fits to cross.

| Quantity | Value | Grade | Source or basis |
|---|---|---|---|
| Schrenk method, twist decomposition, zero-line ideal, chord-weighted reference | — | `[A]` | NACA TM-948 pp. 2–3, read in session |
| "suitable for … maximum lift coefficient, stalling, and static equilibrium problems" | — | `[A]` | TM-948 p. 5 |
| Induced drag / downwash excluded from Schrenk | — | `[A]` | TM-948 p. 5 — why S1-G is a separate solve |
| Lifting-line framework, `e = 1/(1+δ)` | — | `[B]` | Stengel, Princeton MAE331 L5 slides 6, 8 (cites McCormick p. 172) |
| Quarter chord as the section aerodynamic centre | — | `[C]`/convention | The moment reference of the committed screen: NeuralFoil `CM` is quoted about c/4, so trim and the section data share one origin |
| Section CLmax, lift slope, cm0, cl0 vs Reynolds | table | `[C]` | `verification/generate_airfoil_table.py`, provenance in `airfoils_neuralfoil.json` |
| α_L0 of s5010 / e474 at Re 3e5 | −1.350° / −0.000° | `[C]` | `section_at(name, 3e5)`, `−cl0/cl_alpha_per_deg` |
| Elliptic-chord δ | 4.8e-33 | `[C]` | `tests/test_bwb_lifting_line.py::test_elliptic_wing_has_zero_delta` |
| Reference wing δ, e_planar | 0.0222, 0.9782 | `[C]` | `lifting_line.span_efficiency(reference)` |
| `OUTBOARD_STALL_FRAC = 0.60` | 0.60 | `[D]` | Flags only; never enters a computed number. Falsified by a tier-1 departure analysis putting pitch-up onset elsewhere |
| `w10_stall_station = 1.0` | 1.0 | `[D]` | Units are L/D: a tip-stalling wing must buy back ~7% cruise efficiency to break even. At 0.5 it rarely changes a ranking; at 5.0 it dominates |
| `CM_TRIM_TOLERANCE = 0.005` | 0.005 | `[D]` | ~1° of tip twist at the reference dCm/dtwist of 0.0032/deg. Moves only the boolean |
| `cos(Λ_c/4)` on CLmax | — | `[D]` | TM-948 has **no sweep term**. A separately labelled patch, aft sweep only |

## Options considered

**Browser stall.** (a) Ship the 2.33 MB table — rejected, absurd for a 30-number lookup.
(b) Make the browser ask the server on every drag — rejected, it destroys the one thing
the mirror exists for. (c) **Ship the three selected sections' Reynolds curves with the
params payload** — chosen; measured at **881 bytes** live. (d) Leave the browser on the
constants and document it — rejected, that is the defect.

**Missing curves.** (a) Fall back to `CL_MAX_2D` while the fetch is in flight — rejected,
it reintroduces the retired defect *intermittently*, which is harder to find than
always. (b) **Return null and render a dash** — chosen.

**Curve storage.** (a) Module-level cache in `spanwise.js` — written, then rejected when
the parity test failed on its first run: `aero_mirror.js` imports
`./spanwise.js?v=atm1` and the test imports `./spanwise.js`, and an ES module loaded
under two specifiers is two instances with two caches. (b) **Pass curves through the
flight context; the physics module holds no state** — chosen, which removes the failure
class rather than the instance.

**Twist reference.** Leaving geometric twist was considered only long enough to measure
it. 33.7% is not a refinement.

**Span efficiency.** (a) Swap the lifting-line e into the drag polar as planned — **not
done**, see below. (b) Digitise McCormick's δ chart — rejected, a solve is exact and a
digitised chart is not. (c) **Compute and report beside the Oswald fit, polar unchanged**
— chosen.

## Chosen

Schrenk ported to `src/cockpit/static/spanwise.js` as a pure function, with
`aero_mirror.js` mirroring the CLmax ↔ Reynolds ↔ V_stall fixed point; server ships
`spanwise.curve_payload` at bootstrap plus a `/api/sections/curves` top-up route;
`tests/js/spanwise_parity.mjs` asserts against a fixture generated **from Python**, and
`test_js_parity.py` asserts the fixture against Python so a change fails on the
interpreter where it was made (the WSL env has no Node).

Twist reckoned from each station's own zero-lift direction, in both languages.

`trim.py` added; `AeroEstimate` gained six trim fields; the concept card states the
verdict with both escape routes (twist, or CG and the static margin it costs).

`lifting_line.py` added and **reported only**. `oswald_efficiency` still drives
`cd_cruise`, unchanged, so no L/D moved.

## Evidence it worked

| Check | Before | After | Command |
|---|---|---|---|
| Browser vs server CLmax, live cockpit | +2.86% | **0.00e+00** | `/api/bootstrap` + `estimate()` both sides, live geometry |
| Browser vs server V_stall, stall station | no equivalent | **0.00e+00** | same |
| Browser vs server L/D | — | 1.11e-16 (1 ULP) | same |
| Worst measured mirror error across the fixture | +66.4% | ≤ 1e-11 rel | `node tests/js/spanwise_parity.mjs` |
| Section payload to the browser | 2.33 MB (table) | **881 bytes** | `JSON.stringify(bootstrap.section_curves).length` |
| Reference CLmax (aerodynamic twist) | 1.0897 | **1.0659** (−2.19%) | `verification/verify_aero_goldens.py` |
| Reference V_stall | 10.538 | **10.655 m/s** (+1.11%) | same |
| Reference stall station | 0.647 | **0.703** | same |
| Reference `launch_ok` | True | **False** (10.655 > 10.5) | same |
| Elliptic-chord δ (the one analytic case) | — | **4.8e-33** | `pytest tests/test_bwb_lifting_line.py` |
| Taper optimum recovered | — | **0.40** at AR 5.83 (classical 0.35–0.40) | same |
| Reference trim residual | not computed | **Cm −0.0284**, needs −9.77° vs a −7° floor | live cockpit geometry |
| Browser estimate cost (must stay drag-rate) | — | **0.79 ms** | 10 estimates in-page |
| Full suite | 685 collected | **731 collected, 538 passed, 0 failed** | `pytest tests/ -q -p no:randomly` |
| xfail backlog | 11 | 11 (unchanged) | `pytest -rx` |

Live-drag sweep in the browser, tip twist +2° → −7°: stall station **0.721 → 0.300**,
crossing the outboard flag between −4° and −5°, `launch_ok` false throughout.

## Still unproven

- **The plan's span-efficiency validation datapoint does not reproduce.** "AR 5.83,
  taper 0.5 → δ 0.0557, e 0.947" gives **δ 0.0112, e 0.9889** here; 0.0557 corresponds
  to taper ~1.05 or AR ~22.5. The elliptic case says the solver is right, so the
  hypothesis is that the source quotes a whole-aircraft **Oswald** efficiency (viscous
  and interference included, 0.75–0.85) rather than a planar **span** efficiency
  (0.98–0.99); the reference wing reads 0.978 inviscid against 0.816 from Raymer.
  Substituting would cut induced drag ~20% and inflate every L/D, so the polar was left
  alone. **Closing this needs a sourced viscous lift-dependent term — a maintainer
  decision, not an agent one.** Pinned by
  `test_the_published_datapoint_does_not_reproduce_and_that_is_recorded`.
- **Two judgment calls taken by the agent that are arguably the maintainer's:**
  `w10_stall_station = 1.0` (how much a departure risk should cost against cruise
  efficiency) and `CM_TRIM_TOLERANCE = 0.005` (what counts as "trims itself"). Both are
  labelled `[D]` with sensitivities at their definitions.
- **Sweep is still uncovered by the loading model.** Schrenk has no sweep term, so trim
  uses an exact moment arm multiplied by a straight-wing load distribution, and CLmax
  carries a labelled `cos(Λ)` patch. This is the weakest link in both results.
- **The `(1+τ)` lift-slope refinement (TM-948 p. 3, fig. 1) is still skipped**, recorded
  by `TAU_REFINEMENT_APPLIED = False`. Not measured.
- **Section CLmax and cm0 are interpolated as values between stations**, not by blending
  aerofoil shapes. A modelling choice, stated, not validated.
- **S1-E, S1-F, S1-H and S1-I were not started.** S1-H (effective dihedral) was
  deliberately deferred: its algebra is attributed to Kuhlman (RCSD 2000), which was not
  read in this session, so implementing it would have meant guessing a coefficient.
