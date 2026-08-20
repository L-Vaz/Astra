# 0006 — CFD practice rules: what is sourced, and what the mesh cannot deliver

**Date:** 2026-08-20 · **Status:** accepted (research only — nothing encoded yet)
**Commits:** this commit · **Decided by:** research pass; the two open physics calls are the maintainer's

## Problem

Almost every CFD practice assertion Astra makes lives in one place: the requirement
template at `src/services/simulate.py:45-76`, as prose handed to the LLM that writes the
case. Boundary condition types, discretisation schemes, relaxation factors, corrector
counts and a claimed mesh non-orthogonality are all asserted there with no source and no
deterministic check behind them.

That is not symmetric with how Astra treats the quantities it has already been burned
by. The atmosphere is hash-pinned to USSA-1976 and kinematic viscosity is re-read and
re-verified before *every* solver attempt (`src/services/flow_conditions.py:189-219`).
Everything else is a sentence in a prompt.

**Evidence it is real.** Complete inventory read this session `[C]`. Reproduce with
`sed -n '20,90p' src/services/simulate.py`, `sed -n '15,60p' domain_packs/bwb/gmsh_farfield.py`,
`sed -n '205,245p' src/services/evaluate.py`, `grep -n "field\|Mesh\." domain_packs/bwb/gmsh_farfield.py`.

| Assertion | Where | Sourced? |
|---|---|---|
| Atmosphere T, p, ρ, μ, ν, Re | `simulate.py:31-44` | **yes** — USSA-1976, SHA-256 pinned |
| ν enforced before every solve | `flow_conditions.py:189-219` | **yes** — contract, re-read pre-solve |
| Turbulence model `kOmegaSST` | `schema.py:295` → `intent.py:69` | default, unsourced |
| Inlet/outlet BCs | template | no |
| **Farfield: `slip`** | template | no |
| Wall functions nutk / kqR / omega | template | no |
| All divSchemes / gradSchemes / laplacianSchemes | template | no |
| `nNonOrthogonalCorrectors 2` | template | no |
| Relaxation p 0.3, U 0.5, k 0.3, ω 0.3 | template | no |
| "non-orthogonality near 85 degrees" | template | asserted; never compared to the measured value |
| Residual convergence < 5e-3 | `evaluate.py:217` | no |
| y⁺ band 5–500 (warning, not a gate) | `evaluate.py:233` | no |
| y⁺ target 50 | `schema.py:296` | no |
| Domain 8×MAC up, 20×MAC down, lateral max(8·MAC, 0.75·span) | `gmsh_farfield.py:47-49` | no |

**Two claims from the prior session are wrong and are corrected here** `[C]`:

- *"The turbulence model is never specified."* It is. `schema.py:295` defaults
  `turbulence_model = "kOmegaSST"` and `intent.py:69` writes it into the enriched
  requirement, which `compose_requirement` prepends to the template. The template's own
  prose does not name it; the requirement the LLM receives does.
- *"`Aref`/`lRef`/`rhoInf`/`CofR` are absent."* They exist, at
  `evaluate.py:124-129` — but only inside `_posthoc_function_objects`, the **fallback**
  that fires when the input writer omitted the function objects entirely. The template
  specifies `liftDir`, `dragDir`, `pitchAxis` and `magUInf` for the in-solve
  `forceCoeffs` and does **not** specify `Aref`, `lRef`, `rhoInf` or `CofR`. So the
  reference area and length that divide force into coefficient are LLM-chosen on the
  happy path and deterministic only on the fallback path. That is a narrower defect than
  "absent", and a worse one: the correct code exists and the good path does not use it.

**One genuine absence remains** `[C]`: freestream k and ω are never specified anywhere.
`grep -rn "turbulentKE\|kInf\|omegaInf\|turbulence intensity" src/services/` returns
nothing. The template names the wall functions and never says what turbulence enters the
domain.

## Severity and blast radius

Wide. Every drag and lift number Astra reports passes through these settings. Two
findings below (y⁺, farfield) bear directly on whether reported $C_L$ and $C_D$ are
trustworthy at all; the rest are a hygiene backlog.

## Physics

### Why freestream k and ω matter, from basics

The SST model carries two transported scalars: turbulent kinetic energy $k$ (energy per
unit mass in velocity fluctuations, m²/s²) and specific dissipation rate $\omega$
(roughly, how fast that energy is handed down to smaller scales, 1/s). Their ratio is an
eddy viscosity, $\nu_t = k/\omega$ — the extra "stickiness" turbulence adds on top of
molecular viscosity. Set them at the inflow and they *decay* as the fluid drifts toward
the body, so what the aircraft actually sees is not what you set. That decay is why the
recommended values look arbitrary: they are chosen to land somewhere sane after
decaying.

NASA's Turbulence Modeling Resource states the ranges below. All read this session from
the TMR source mirror (`raw.githubusercontent.com/tmbwg/turbmodels/main/sst.html`,
`.../spalart.html`; the `turbmodels.larc.nasa.gov` host is egress-blocked from this
container).

| Quantity | Value | Grade | Source |
|---|---|---|---|
| SST ω farfield | $U_\infty/L < \omega < 10\,U_\infty/L$ | `[A]` | TMR SST page, "boundary conditions recommended in the original reference" |
| SST k farfield | $10^{-5}U_\infty^2/Re_L < k < 0.1\,U_\infty^2/Re_L$ | `[A]` | same |
| L | "the approximate length of the computational domain" | `[A]` | same, TMR's own quotation marks |
| Freestream eddy viscosity | $10^{-5} < \nu_t/\nu < 10^{-2}$ | `[A]` | same — "the combination of the two farfield values should yield…" |
| SST wall | $k_w=0$, $\omega_w = 10\cdot 6\nu/(\beta_1 \Delta d_1^2)$ | `[A]` | same; $\beta_1 = 0.075$ |
| SST-sust farfield | $\omega = 5U_\infty/L$, $k = 10^{-6}U_\infty^2$ | `[A]` | TMR SST-sust section; here L is "the defining length scale for the particular problem", **not** the domain length |
| SST-sust turbulence level | Tu = 0.08165% | `[A]` | same, stated explicitly |
| SA farfield | $\tilde\nu = 3\nu_\infty$ to $5\nu_\infty$ | `[A]` | TMR SA page |
| SA farfield, as eddy viscosity | $\nu_t/\nu = 0.210438$ to $1.294234$ | `[A]` | same, stated explicitly |
| SA variant | prefer SA-neg, "generally recommended because of its more robust numerical behavior" | `[A]` | TMR SA page |
| Production limiter | $\min(\mathcal{P},\,20\beta^*\rho\omega k)$ recommended | `[A]` | TMR SST page, citing Menter AIAA 93-2906 |

**The three SST rules are not independent.** Instantiated for the golden case
(U = 25 m/s, h = 100 m, Re_MAC = 814947, MAC = 0.4799 m, L_domain ≈ 29·MAC ≈ 13.9 m) `[C]`:

```
omega_farfield in [1.796, 17.964] 1/s
k_farfield     in [2.6447e-10, 2.6447e-06] m2/s2
corner check nut/nu = k/omega/nu:
  k_lo,w_lo -> 1.000e-05      k_lo,w_hi -> 1.000e-06
  k_hi,w_lo -> 1.000e-01      k_hi,w_hi -> 1.000e-02
```

The $\nu_t/\nu$ window is exactly the diagonal of the k–ω box. Picking k at its top and
ω at its bottom gives $\nu_t/\nu = 0.1$ — ten times over the limit. So a rule library
must treat the eddy-viscosity bound as the **binding** constraint that couples the other
two, not as a third independent check. Encoding all three as separate range tests would
pass a combination NASA rejects.

For reference, the SST-sust preferred values on the same case give $\nu_t/\nu = 0.163$
`[C]` — well outside the standard-SST window. That is not a contradiction: TMR says the
standard values "have been adjusted to allow a drastic decay before the fluid approaches
the body", whereas the sustaining variant maintains its ambient values. The two are
different regimes and must not be checked against each other's bounds.

### y⁺ — the wall-function question, settled from source

A wall function avoids resolving the viscous sublayer by assuming the first cell centre
sits in the logarithmic region of the boundary layer, where $u^+ = \ln(E y^+)/\kappa$.
Below that region the assumption is simply false.

The relevant primary here is not a guideline but the code that runs. From
`OpenFOAM-10/.../nutkWallFunctionFvPatchScalarField.C` `[A]`, read this session:

```cpp
scalar yPlus = Cmu25*y[facei]*sqrt(k[celli])/nuw[facei];
if (yPlus > yPlusLam_)
{
    nutw[facei] = nuw[facei]*(yPlus*kappa_/log(E_*yPlus) - 1.0);
}
```

`nutw` is initialised to `0.0` and there is no `else`. Below `yPlusLam_` the wall
function contributes **zero** eddy viscosity — silently, with no warning. `yPlusLam` is
the fixed point of `ypl = log(max(E*ypl,1))/kappa` with κ = 0.41, E = 9.8
(`nutWallFunctionFvPatchScalarField.C`), which evaluates to **11.5301** `[C]`
(reproduced with the same 10-iteration loop). It is the crossover where the log law
meets $u^+ = y^+$.

So Astra's lower bound of 5 admits meshes on which the turbulence wall treatment is
switched off. That does not need a blog citation or an ERCOFTAC page — it is what the
solver does. The commonly quoted 30 is a statement about where the log layer *begins*
and remains `[D]`; 11.53 is a statement about where this code *stops modelling* and is
`[A]`.

### The finding that matters most: the mesh cannot reach its own y⁺ target

`domain_packs/bwb/gmsh_farfield.py` has **no boundary-layer inflation** `[C]` — no
prism layers, no anisotropic near-wall cells, no `BoundaryLayer` field. It is isotropic
tets sized by a Distance→Threshold field with `SizeMin = near_mac × MAC` at the wall
(lines 91-101; `grep -n "field\|Mesh\." domain_packs/bwb/gmsh_farfield.py`). The
wall-normal spacing is therefore the *surface* mesh size, and y⁺ is a rigid consequence
of the refinement level rather than something that can be tuned.

Estimating $u_\tau$ from the flat-plate turbulent correlation $C_f = 0.0576\,Re^{-1/5}$
`[D]` at Re_MAC = 814947 gives $C_f = 0.00379$, $\tau_w = 1.436$ Pa,
$u_\tau = 1.088$ m/s `[C]` from those inputs:

| Level | wall cell h | y⁺ at 0.25h | y⁺ at 0.5h |
|---|---|---|---|
| coarse (0.055·MAC) | 26.40 mm | 488 | 975 |
| medium (0.035·MAC) | 16.80 mm | 310 | 621 |
| fine (0.022·MAC) | 10.56 mm | 195 | 390 |

`y_plus_target = 50` (`schema.py:296`) demands a wall spacing of **0.677 mm**. The
mesher delivers 10.6–26.4 mm — a factor of **15 to 40 too coarse**. The target is stated
to the LLM in the enriched requirement (`intent.py:69`) and the mesh contradicts it.

Refinement cannot fix this. Reaching y⁺ ≈ 50 needs h ≈ 2.7 mm, roughly 4× finer than
"fine" *in all three directions* — order 64× the cell count. Without anisotropic prism
layers, y⁺ and cell count are rigidly coupled. This is a structural property of the
mesher, not a tuning oversight.

**Sensitivity:** y⁺ ∝ $\sqrt{C_f}$, so even a 2× error in the correlation moves these
numbers by only 1.41×. The conclusion — y⁺ in the hundreds, target unreachable — holds
across any plausible $C_f$.

**What would falsify it:** one run. `checkMesh` plus the existing yPlus function object
on the golden case, reading `metrics["yplus"]["avg"]`. If it comes back near 50, the
reasoning above is wrong. No `~/astra_runs` exists in this container, so this was not
measured on a real mesh — it is a prediction, and it is cheap to settle.

It is worth noting the two unsourced numbers are consistent with each other in a
suggestive way: a y⁺ acceptance band whose ceiling is 500 is exactly what you would
choose if you needed the coarse level to pass.

### Domain extent and the farfield boundary

TMR's NACA 0012 validation case `[A]`:

> "To minimize issues associated with effect of the farfield boundary (which can
> particularly influence drag and lift levels at high lift conditions), the farfield
> boundary in the grids provided have been located almost 500 chords away from the
> airfoil. Otherwise, a 'farfield point vortex' boundary condition correction should be
> employed (see Thomas and Salas, AIAA Journal 24(7):1074-1080, 1986)."

And from the numerics page for the same case `[A]`:

> "The grids have a farfield extent of about 500c. Nonetheles, a farfield point vortex
> boundary condition correction is recommended… While this correction is relatively
> small for such a large farfield extent, its influence will be noticeable at the
> detailed levels being investigated here."
>
> "The farfield boundary conditions are based on inviscid characteristic methods."

Three things follow, and one does not.

1. The mechanism is confirmed `[A]`: farfield placement influences lift and drag, most
   at high lift. Not speculation.
2. The reference farfield BC is **characteristic**, not a slip wall `[A]`. Astra uses
   `slip`. A slip wall enforces zero normal velocity — it is an inviscid *wall*. For a
   lifting body it produces an image-vortex system, i.e. wind-tunnel wall interference,
   constraining downwash and changing lift. This is now a documented divergence from the
   authoritative reference practice, not a hunch.
3. There is a named remedy for a small domain: the Thomas & Salas point-vortex
   correction `[A]`.

**What does not follow: that Astra needs 500 chords.** 500c is a 2D *lifting* airfoil
number. TMR's own 3D cases are far smaller. From the 3D hemisphere cylinder case `[A]`:

> "for the structured grid, it is located approximately 20 unit lengths from the body;
> whereas for the unstructured grid, it is located approximately 10 unit lengths from
> the body"

with "the unit length in the grid is 1 inch" and a hemisphere radius of 0.5 in — so the
unit length is the body diameter. TMR accepts *both* grids for the same validation case,
tolerating a 2× difference in extent.

But that case's reference solutions are computed at **angle of attack of 0 degrees**
`[A]` — a non-lifting body. And that is precisely the variable that explains the spread.
A lifting body carries bound circulation; its induced velocity field decays slowly
(in 2D, as $1/r$), so the boundary must go far away before it stops mattering. A
non-lifting body's disturbance is a doublet-like field that decays much faster. This is
the same mechanism as the slip-wall concern in (2) above — both are about circulation
reaching the boundary.

So the three data points are consistent, and they bracket rather than answer the
question `[A]`:

| Case | Lifting? | Farfield extent |
|---|---|---|
| NACA 0012, 2D | yes | ~500 chords, *plus* point-vortex correction |
| 3D hemisphere cylinder | no (α = 0) | 10–20 body lengths |
| ONERA M6, 3D wing | yes | **no extent stated** `[C]` |

TMR states no farfield extent for any 3D *lifting* case — the one category Astra's BWB
falls into. So whether 8×MAC is adequate is **not established** and cannot be settled by
citation from this source. It is bounded below by the non-lifting 10–20 (Astra's 8×MAC
upstream is slightly under even that) and above by the 2D lifting 500.

TMR does show how to settle it, on the flat plate `[A]`:

> "a test in which the upper extent was moved down to y=0.48 L only changed results
> (integrated drag or skin friction at x=0.97) by less than 0.2%."

That is the template: a domain-sensitivity test, with the tolerance stated first.

## Options considered

- **Encode the seven NASA rules immediately.** Rejected as the first step: the research
  turned up two findings (y⁺ unreachable, slip farfield) that outrank freestream
  turbulence values in effect on reported forces. Encoding first would have polished the
  cheapest layer.
- **Cite 30–300 for y⁺ from secondary sources.** Rejected — and now unnecessary. The
  solver source gives a harder and better-grounded number (11.53) for the floor.
- **Assert a larger domain from the 500c figure.** Rejected: 2D→3D transfer is invalid,
  and asserting it would be exactly the unsourced-number failure this protocol exists to
  prevent.

## Chosen

Record the sourced rule set and the two structural findings; encode nothing yet. The
rule library, when built, should contain:

**Ready to encode, `[A]`-sourced** — the eleven rows in the Physics table, with the
$\nu_t/\nu$ bound implemented as the coupling constraint on k and ω rather than a third
independent range check.

**Ready to encode, `[A]` from solver source** — y⁺ floor of 11.53: below it,
`nutkWallFunction` contributes zero eddy viscosity. Astra's current floor of 5 admits
meshes where the wall model is inactive.

**Deterministic checks that need no new source** — compare the template's asserted
"non-orthogonality near 85 degrees" against the measured `max_non_orthogonality`, which
is already parsed at `bwb_mesh.py:133` and printed at `verify_golden.py:121` but
**never compared to anything** `[C]`; and specify `Aref`/`lRef`/`rhoInf`/`CofR` on the
in-solve `forceCoeffs` instead of only on the post-hoc fallback.

**Two physics calls that are the maintainer's, not mine:**

1. **Farfield BC type.** Keep `slip`, or move to `freestream`/characteristic. Band: for
   a lifting body at 8×MAC the image-vortex effect on $C_L$ is plausibly percent-level
   but I have no measurement. Consequence: a systematic $C_L$ bias in every reported
   number. Recommendation: switch to a freestream/characteristic-type BC, which
   tolerates a smaller domain than a slip wall does — but run the test in (2) first,
   since it measures both.
2. **Mesh strategy for y⁺.** Either add prism-layer inflation to the mesher (decouples
   y⁺ from cell count, makes y_plus_target = 50 achievable), or accept y⁺ in the
   hundreds and revise `y_plus_target` and the 5–500 band to match what the mesher can
   actually produce. Doing neither leaves a stated target the mesh contradicts.

## Evidence it worked

Nothing was implemented, so there is no before/after. What was established:

| Check | Result | Command |
|---|---|---|
| TMR SST/SA rules read from primary | 11 rules, verbatim | `curl -sS raw.githubusercontent.com/tmbwg/turbmodels/main/sst.html` (and `spalart.html`) |
| SST k/ω/νt rules are coupled, not independent | corner νt/ν = 1e-5 … 1e-1 | corner-check script, this record |
| yPlusLam value | 11.5301 | 10-iteration fixed point of `log(max(E*ypl,1))/kappa`, κ=0.41, E=9.8 |
| Mesher has no BL inflation | confirmed | `grep -n -i "boundaryLayer\|prism\|extrude" domain_packs/bwb/gmsh_farfield.py` → no hits |
| Predicted y⁺ vs target 50 | 195–975 vs 50 | y⁺ table, this record |
| Non-orthogonality assertion never checked | confirmed | `grep -rn "nonOrtho" src/ verification/` → parsed and printed, never compared |
| TMR 3D extents: 10–20 body lengths, but at α=0 | confirmed | `grep -o -i ".\{170\}\(extent\|outer boundary\).\{230\}"` over `hc3dnumerics_val.html`, `onera.html`, `bump3d_mod_numerics_val.html` |
| TMR states no extent for a 3D *lifting* case | confirmed | same grep over `onerawingnumerics_val.html` → no hits |

## Still unproven

- **The y⁺ prediction is a prediction.** $C_f$ is `[D]`, and no real mesh was measured —
  no `~/astra_runs` in this container. Settle it with one golden run and read
  `metrics["yplus"]["avg"]`. The conclusion is robust to $C_f$ error (∝ √Cf) but not to
  being wrong about tet centroid placement.
- **Whether 8×MAC is adequate is unresolved.** It is now *bracketed* `[A]` — above a
  non-lifting 3D body's 10–20 body lengths would be the floor, and Astra's 8×MAC
  upstream sits just under it, while the 2D lifting 500c is the ceiling. No TMR 3D
  lifting case states an extent, so the gap cannot be closed by citation. It needs the
  domain sensitivity test: same case at 8, 20 and 40×MAC, watching $C_L$. Tolerance
  fixed *before* running — TMR's flat-plate precedent used <0.2% on integrated drag.
  Run it with both `slip` and `freestream` farfield and it settles the BC question at
  the same time, since both are the same circulation-reaching-the-boundary mechanism.
- **Scheme choices, relaxation factors and `nNonOrthogonalCorrectors 2` remain `[D]`.**
  Defensible on their face; no citation read. Not chased this session — they are lower
  blast radius than the two findings above.
- **The residual threshold 5e-3 remains `[D]`.** Untouched this session.
- **y⁺ 30–300 remains `[D]`.** No primary read. The 11.53 floor supersedes the need for
  it as a *floor*; an upper bound still has no source.
- Egress here reaches only `github.com`. NTRS, the LaRC TMR host, arxiv, openfoam.com
  and cfd.direct are all blocked. Anything not mirrored on GitHub could not be chased.
