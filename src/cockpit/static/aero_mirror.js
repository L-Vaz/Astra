/* Equation-level aero estimates — JS mirror of domain_packs/bwb/aero_estimates.py.
 *
 * Same classical methods (mixed flat-plate skin friction, Hoerner form factor,
 * Raymer wetted-area + Oswald fits, quarter-chord-sweep CLmax knockdown,
 * planform quarter-MAC neutral point). Every number is a TEXTBOOK ESTIMATE for
 * instant feedback while dragging — VSPAERO (fast) and OpenFOAM (CFD) results
 * always supersede these. Keep in sync with the Python module.
 */
import * as PLANFORM from './planform.js?v=atm1';
import * as SPANWISE from './spanwise.js?v=atm1';

export const LAMINAR_FRACTION = 0.30;
export const INTERFERENCE_MARGIN = 1.10;
/* CL_MAX_2D = 1.30 and CL_MAX_3D_FACTOR = 0.90 were DELETED here (S1-C2), as they
   were in aero_estimates.py. One hard-coded section value and one unsourced
   knockdown produced the same CLmax for every aircraft this project has ever drawn.
   Both are now computed by spanwise.js. */
export const LAUNCH_VSTALL_MAX = { hand: 10.5, catapult: 13.0, runway: 16.0 };
export const STATIC_MARGIN_TARGET = 0.08;

const rad = (d) => d * Math.PI / 180;

/* The planform algebra lives in planform.js — one copy in this language (S7-I1).
   These re-exports keep the existing call sites working; `cr` is preserved as an
   alias for `cRoot` because callers here use that name. */
export function chords(p) {
  const { cRoot, cMid, cTip } = PLANFORM.chords(p);
  return { cr: cRoot, cRoot, cMid, cTip };
}

export const halfWidths = PLANFORM.halfWidths;
export const planformArea = PLANFORM.planformArea;
export const meanAeroChord = PLANFORM.meanAeroChord;

function meanTc(p) {
  const { cr, cMid, cTip } = chords(p);
  const { b1, b2 } = halfWidths(p);
  const a1 = b1 * (cr + cMid) / 2, a2 = b2 * (cMid + cTip) / 2;
  const tr = +p.root_thickness_pct / 100, tt = +p.tip_thickness_pct / 100;
  /* S1-C: the blend station has its own aerofoil, so read its t/c instead of
     assuming the root/tip mean. Mirrors aero_estimates.mean_tc exactly. */
  const tm = +p.mid_thickness_pct / 100;
  return (a1 * (tr + tm) / 2 + a2 * (tm + tt) / 2) / (a1 + a2);
}

/* Half-width-weighted leading-edge sweep, averaged in TANGENT space.

   SECOND PRE-EXISTING PARITY BUG, found alongside the skin-friction one and fixed
   here: this averaged ANGLES while effC4SweepDeg below averages tangents (S6-F4).
   One definition feeds Oswald e and the other feeds CLmax, so the browser had drag
   and stall running on contradictory conventions, and by Jensen's inequality the two
   do not agree. Tangent averaging is the geometrically meaningful mean — it is the
   sweep of the line joining the panel leading edges over the same span. */
function effLeSweepDeg(p) {
  const { b1, b2 } = halfWidths(p);
  const t1 = Math.tan(rad(+p.sweep_inner_deg));
  const t2 = Math.tan(rad(+p.sweep_outer_deg));
  return Math.atan((b1 * t1 + b2 * t2) / (b1 + b2)) * 180 / Math.PI;
}

function effC4SweepDeg(p) {
  const { cr, cMid, cTip } = chords(p);
  const { b1, b2 } = halfWidths(p);
  const t1 = Math.tan(rad(+p.sweep_inner_deg)) - 0.25 * (cr - cMid) / b1;
  const t2 = Math.tan(rad(+p.sweep_outer_deg)) - 0.25 * (cMid - cTip) / b2;
  return Math.atan((b1 * t1 + b2 * t2) / (b1 + b2)) * 180 / Math.PI;
}

/* Mixed laminar/turbulent flat-plate Cf. Mirrors aero_estimates.skin_friction_coeff.

   PRE-EXISTING PARITY BUG, found while wiring sections (S1-C) and fixed here: the
   laminar segment must be evaluated at the TRANSITION Reynolds number, not the
   full-chord one (S6-F2a). Python was corrected; this mirror was not, so the browser
   ran a Cf about 9.5% low — measured at Re 642732, JS 0.003900356434 against Python
   0.00431070101 — which made cd0 ~10.5% low and cruise L/D ~8% optimistic on screen.

   Blasius Cf ~ 1/sqrt(Re), so evaluating the first 30% of the chord at the
   full-chord Re understates its friction by 1/sqrt(0.30) = 1.83x.

   The parity test did not catch it because its expected value had been taken from
   this file's own output, so it compared the mirror against itself. */
export function skinFriction(re) {
  re = Math.max(re, 1e4);                       // RE_FLOOR
  const cfT = 0.455 / Math.pow(Math.log10(re), 2.58);   // Raymer eq. 12.27
  const cfL = 1.328 / Math.sqrt(Math.max(LAMINAR_FRACTION * re, 1.0));
  return LAMINAR_FRACTION * cfL + (1 - LAMINAR_FRACTION) * cfT;
}

export function oswald(ar, leSweepDeg) {
  const eS = 1.78 * (1 - 0.045 * Math.pow(ar, 0.68)) - 0.64;
  const eW = 4.61 * (1 - 0.045 * Math.pow(ar, 0.68)) *
    Math.pow(Math.cos(rad(leSweepDeg)), 0.15) - 3.1;
  let e;
  if (leSweepDeg <= 0) e = eS;
  else if (leSweepDeg >= 30) e = eW;
  else { const w = leSweepDeg / 30; e = (1 - w) * eS + w * eW; }
  return Math.max(0.5, Math.min(e, 0.95));
}

export function parasiteDrag(p, reMac) {
  const tc = meanTc(p);
  return skinFriction(reMac) * (1 + 2 * tc + 60 * tc ** 4)
    * (1.977 + 0.52 * tc) * INTERFERENCE_MARGIN;
}

/* Wing CLmax from where the wing actually stalls (S1-C2). Mirrors
   aero_estimates.cl_max_3d, including the sweep factor's status: TM-948 is a straight
   monoplane derivation with NO sweep term, so cos(Lc/4) is a separately applied patch,
   and it is applied to AFT sweep only — Math.cos is even, so a forward-swept quarter
   chord would otherwise be penalised exactly like an aft-swept one (S6-F6). */
export function clMax3d(p, reynoldsMac, curves) {
  const wing = SPANWISE.solveStall(p, reynoldsMac, curves).clMaxWing;
  return wing * Math.cos(rad(Math.max(effC4SweepDeg(p), 0)));
}

/* Stall speed, CLmax and the spanwise solution, solved SELF-CONSISTENTLY. Mirrors
   aero_estimates.stall_solution.

   A wing stalls at its stall speed, not at cruise, so the Reynolds number that sets
   CLmax depends on the answer. Measured on the golden reference: 1.206 at the cruise
   Reynolds against 1.133 self-consistent — 6% apart. Skipping the iteration in the
   browser would reintroduce that error on screen only, which is the worst place for
   it, because the screen is what the operator believes. */
export function stallSolution(p, atmosphere, weightN, curves, maxIter = 8, tol = 1e-4) {
  const pf = PLANFORM.planform(p);
  const rho = atmosphere.density_kgm3;
  const nu = atmosphere.kinematic_viscosity_m2s;
  const cosSweep = Math.cos(rad(Math.max(effC4SweepDeg(p), 0)));

  let v = Math.sqrt(2 * weightN / (rho * pf.s * 1.0));
  let sol = null;
  for (let i = 0; i < maxIter; i++) {
    sol = SPANWISE.solveStall(p, Math.max(v * pf.mac / nu, 1), curves);
    const clmax = sol.clMaxWing * cosSweep;
    const vNew = Math.sqrt(2 * weightN / (rho * pf.s * clmax));
    if (Math.abs(vNew - v) <= tol * Math.max(v, 1e-6)) { v = vNew; break; }
    v = vNew;
  }
  return { vStall: v, clMax3d: sol.clMaxWing * cosSweep, stall: sol };
}

export function neutralPoint(p) {
  const { cr, cMid, cTip } = chords(p);
  const { b1, b2 } = halfWidths(p);
  const panelAc = (crP, ctP, hw, xleRoot, sweepDeg) => {
    const taper = ctP / crP;
    const yMac = (hw / 3) * (1 + 2 * taper) / (1 + taper);
    const mac = (2 / 3) * crP * (1 + taper + taper * taper) / (1 + taper);
    const area = hw * (crP + ctP) / 2;
    return { x: xleRoot + yMac * Math.tan(rad(sweepDeg)) + 0.25 * mac, area };
  };
  const p1 = panelAc(cr, cMid, b1, 0, +p.sweep_inner_deg);
  const xleMid = b1 * Math.tan(rad(+p.sweep_inner_deg));
  const p2 = panelAc(cMid, cTip, b2, xleMid, +p.sweep_outer_deg);
  return (p1.area * p1.x + p2.area * p2.x) / (p1.area + p2.area);
}

/* Full tier-0 panel for one geometry at one flight condition.
 * ctx: { atmosphere, v, w, launch } */
export function estimate(p, ctx) {
  const s = planformArea(p);
  const ar = +p.span_m * +p.span_m / s;
  const mac = meanAeroChord(p);
  const { cTip } = chords(p);
  const rho = ctx.atmosphere.density_kgm3;
  const nu = ctx.atmosphere.kinematic_viscosity_m2s;
  const q = 0.5 * rho * ctx.v * ctx.v;
  const reMac = ctx.v * mac / nu;

  const cd0 = parasiteDrag(p, reMac);
  const e = oswald(ar, effLeSweepDeg(p));
  const k = 1 / (Math.PI * e * ar);
  const cl = ctx.w / (q * s);
  const cd = cd0 + k * cl * cl;
  const clStar = Math.sqrt(cd0 / k);

  /* The stall block needs the selected sections' Reynolds curves, which arrive from
     the server (spanwise.curve_payload). If they have not landed yet, every stall
     quantity is NULL — not a fallback constant. The deleted CL_MAX_2D path is exactly
     how the browser came to read 2.86% optimistic; a blank field is honest and a
     plausible wrong stall speed is not. */
  const haveCurves = SPANWISE.hasCurves(
    ctx.sectionCurves, p.root_airfoil, p.mid_airfoil, p.tip_airfoil);
  const stallBlock = haveCurves
    ? stallSolution(p, ctx.atmosphere, ctx.w, ctx.sectionCurves)
    : { vStall: null, clMax3d: null, stall: null };
  const clmax = stallBlock.clMax3d;
  const vStall = stallBlock.vStall;
  const cap = LAUNCH_VSTALL_MAX[ctx.launch] ?? null;
  const xNp = neutralPoint(p);

  return {
    s, ar, mac, span: +p.span_m,
    wingLoading: ctx.w / 9.81 / s,
    washout: +p.root_twist_deg - +p.tip_twist_deg,
    reMac, cd0, e, cl, cd,
    ldCruise: cl / cd,
    clStar, ldMax: clStar / (2 * cd0),
    clMax3d: clmax, vStall,
    stallEta: stallBlock.stall ? stallBlock.stall.stallEta : null,
    stallIsOutboard: stallBlock.stall ? stallBlock.stall.stallIsOutboard : null,
    launchLimit: cap,
    launchOk: (cap == null || vStall == null) ? null : vStall <= cap,
    xNp, xCgTarget: xNp - STATIC_MARGIN_TARGET * mac,
    bending: ctx.w * +p.span_m / 8,
    tipRe: ctx.v * cTip / nu,
  };
}
