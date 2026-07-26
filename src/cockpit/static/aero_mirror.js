/* Equation-level aero estimates — JS mirror of domain_packs/bwb/aero_estimates.py.
 *
 * Same classical methods (mixed flat-plate skin friction, Hoerner form factor,
 * Raymer wetted-area + Oswald fits, quarter-chord-sweep CLmax knockdown,
 * planform quarter-MAC neutral point). Every number is a TEXTBOOK ESTIMATE for
 * instant feedback while dragging — VSPAERO (fast) and OpenFOAM (CFD) results
 * always supersede these. Keep in sync with the Python module.
 */
import * as PLANFORM from './planform.js?v=atm1';

export const LAMINAR_FRACTION = 0.30;
export const INTERFERENCE_MARGIN = 1.10;
export const CL_MAX_2D = 1.30;
export const CL_MAX_3D_FACTOR = 0.90;
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
  const tm = (tr + tt) / 2;
  return (a1 * (tr + tm) / 2 + a2 * (tm + tt) / 2) / (a1 + a2);
}

function effLeSweepDeg(p) {
  const { b1, b2 } = halfWidths(p);
  return (b1 * +p.sweep_inner_deg + b2 * +p.sweep_outer_deg) / (b1 + b2);
}

function effC4SweepDeg(p) {
  const { cr, cMid, cTip } = chords(p);
  const { b1, b2 } = halfWidths(p);
  const t1 = Math.tan(rad(+p.sweep_inner_deg)) - 0.25 * (cr - cMid) / b1;
  const t2 = Math.tan(rad(+p.sweep_outer_deg)) - 0.25 * (cMid - cTip) / b2;
  return Math.atan((b1 * t1 + b2 * t2) / (b1 + b2)) * 180 / Math.PI;
}

export function skinFriction(re) {
  re = Math.max(re, 1e4);
  const cfT = 0.455 / Math.pow(Math.log10(re), 2.58);
  const cfL = 1.328 / Math.sqrt(re);
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

export function clMax3d(p) {
  return CL_MAX_2D * CL_MAX_3D_FACTOR * Math.cos(rad(effC4SweepDeg(p)));
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

  const clmax = clMax3d(p);
  const vStall = Math.sqrt(2 * ctx.w / (rho * s * clmax));
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
    launchLimit: cap, launchOk: cap == null ? null : vStall <= cap,
    xNp, xCgTarget: xNp - STATIC_MARGIN_TARGET * mac,
    bending: ctx.w * +p.span_m / 8,
    tipRe: ctx.v * cTip / nu,
  };
}
