/* Browser mirror of domain_packs/bwb/spanwise.py — Schrenk's spanwise lift
 * distribution and the stall station (S1-C2).
 *
 * WHY THIS FILE EXISTS. The Python side replaced `CL_MAX_2D * 0.90 * cos(sweep)` with
 * a computed stall solve. The browser kept the constants, and was measured 2.86%
 * optimistic on CLmax with no stall station at all — the exact drift class that hid
 * the skin-friction (9.5% low) and LE-sweep (angle- vs tangent-averaged) bugs. The
 * mirror is not optional: the cockpit's whole point is that dragging a slider shows
 * the consequence immediately, and a stall speed is the consequence that decides
 * whether the aircraft can be hand-launched.
 *
 * THE METHOD IS NOT RESTATED HERE. spanwise.py carries the derivation and the NACA
 * TM-948 page citations; this file is a transcription of it and nothing more. If the
 * two disagree, Python is right and tests/js/spanwise_parity.mjs fails.
 *
 * WHAT THE BROWSER CANNOT HAVE: the 2.33 MB section table. The server ships the three
 * selected sections sampled on the table's own Reynolds grid (see
 * spanwise.curve_payload) — 30 numbers. Without them this module returns null rather
 * than a fallback number, because a plausible wrong stall speed is worse than a blank.
 */
import * as PLANFORM from './planform.js?v=atm1';

export const N_STATIONS = 80;
export const OUTBOARD_STALL_FRAC = 0.60;

const rad = (d) => d * Math.PI / 180;

/* ------------------------------------------------------- the section curves */
/* The curves are PASSED IN, never held here. The first draft of this module kept them
   in a module-level Map, and the parity test failed on its first run: aero_mirror.js
   imports './spanwise.js?v=atm1' while the test imports './spanwise.js', and an ES
   module loaded under two specifiers is TWO MODULE INSTANCES with two separate caches.
   In the browser that is a live split-brain waiting for someone to add a cache-buster
   to one import and not another — the stall panel would read from a map that another
   file had filled. A pure function cannot have that failure mode, so the app owns the
   data (cockpit.js `S.sectionCurves`) and the physics owns none. */

export function hasCurves(curves, ...names) {
  return !!curves && names.every(n => Object.prototype.hasOwnProperty.call(curves, n));
}

export function missingCurves(curves, ...names) {
  return [...new Set(names.filter(n => !hasCurves(curves, n)))];
}

/** CLmax and lift slope for one section at one Reynolds, linear in log(Re).
 *  Mirrors spanwise._SectionCurve.at, including the flat extrapolation outside the
 *  grid — the table's domain ends there and pretending otherwise invents data. */
function curveAt(curves, name, reynolds) {
  const c = curves && curves[name];
  if (!c) throw new Error(`no section curve loaded for ${name}`);
  const x = Math.log(Math.max(reynolds, 1));
  const lr = c.log_re;
  const last = lr.length - 1;
  const at = i => [c.clmax[i], c.cl_alpha[i], c.cm0[i], c.alpha_l0[i]];
  if (x <= lr[0]) return at(0);
  if (x >= lr[last]) return at(last);
  for (let i = 0; i < last; i++) {
    if (lr[i] <= x && x <= lr[i + 1]) {
      const t = lr[i + 1] > lr[i] ? (x - lr[i]) / (lr[i + 1] - lr[i]) : 0;
      const a = at(i), b = at(i + 1);
      return a.map((v, k) => v + t * (b[k] - v));
    }
  }
  return at(last);
}

/* ---------------------------------------------------------------- the method */

/** Piecewise-linear value at y, breaking at the centre-body/wing blend.
 *  Mirrors spanwise._panel_interp. */
function panelInterp(y, b1, b2, root, mid, tip) {
  if (y <= b1) return root + (mid - root) * (b1 > 0 ? y / b1 : 0);
  return mid + (tip - mid) * (b2 > 0 ? (y - b1) / b2 : 0);
}

/** Twist at the blend station — interpolated by CENTRE_BODY_FRACTION, not by
 *  half-span position. Mirrors spanwise._mid_twist and vsp_builder. */
function midTwist(p) {
  return p.root_twist_deg + (p.tip_twist_deg - p.root_twist_deg) * p.centre_body_fraction;
}

/** Cosine-spaced stations with the panel break inserted. Mirrors spanwise._grid.
 *  Uses a Set on the raw doubles exactly as Python does, so the two grids are
 *  bit-identical including the de-duplication when b1 lands on a cosine point. */
function grid(p) {
  const half = p.span_m / 2;
  const { b1 } = PLANFORM.halfWidths(p);
  const ys = [];
  for (let i = 0; i < N_STATIONS; i++) {
    ys.push(half * (1 - Math.cos(Math.PI * i / (N_STATIONS - 1))) / 2);
  }
  ys.push(b1);
  return [...new Set(ys)].sort((a, b) => a - b);
}

function trapz(values, xs) {
  let acc = 0;
  for (let i = 0; i < xs.length - 1; i++) {
    acc += (values[i] + values[i + 1]) * (xs[i + 1] - xs[i]) / 2;
  }
  return acc;
}

/** Demand and capability at every station (TM-948 pp. 2-3). Mirrors
 *  spanwise.stations; field names are camelCase per this side's convention. */
export function stations(params, reynoldsMac, curves) {
  const p = PLANFORM.numeric(params);
  const half = p.span_m / 2;
  const { b1, b2 } = PLANFORM.halfWidths(p);
  const { cRoot, cMid, cTip } = PLANFORM.chords(p);
  const pf = PLANFORM.planform(p);
  const sRef = pf.s, mac = pf.mac;
  const tRoot = p.root_twist_deg, tMid = midTwist(p), tTip = p.tip_twist_deg;

  /* Leading-edge x aft of the root LE apex — the moment arm that makes a swept
     wing's trim different from a straight wing's (S1-D). Same two-panel law as
     planform.js. */
  const tanI = Math.tan(rad(p.sweep_inner_deg));
  const tanO = Math.tan(rad(p.sweep_outer_deg));

  const ys = grid(p);
  const chords = ys.map(y => panelInterp(y, b1, b2, cRoot, cMid, cTip));
  const twists = ys.map(y => panelInterp(y, b1, b2, tRoot, tMid, tTip));
  const xLes = ys.map(y => (y <= b1 ? y * tanI : b1 * tanI + (y - b1) * tanO));

  /* PASS 1 — section properties at each station's own chord Reynolds. Must come
     before the zero-lift reference, because that reference integrates over them. */
  const reLocals = chords.map(c => (mac > 0 ? reynoldsMac * (c / mac) : reynoldsMac));
  const sampled = ys.map((y, i) => {
    const r = curveAt(curves, params.root_airfoil, reLocals[i]);
    const m = curveAt(curves, params.mid_airfoil, reLocals[i]);
    const t = curveAt(curves, params.tip_airfoil, reLocals[i]);
    return [0, 1, 2, 3].map(k => panelInterp(y, b1, b2, r[k], m[k], t[k]));
  });                                  // [clmax, clAlpha, cm0, alphaL0]

  /* AERODYNAMIC twist, not geometric: TM-948 p.2 reckons twist from the ZERO LIFT
     direction, and each station has its own when the section changes along the span.
     Measured on the reference wing, the cambered s5010 tip (alpha_L0 = -1.35 deg) on
     a symmetric e474 root cancels 34% of the geometric washout. */
  const aeroTwists = twists.map((tw, i) => tw - sampled[i][3]);

  /* Chord-weighted mean = the wing's overall zero-lift direction (TM-948 p.2). */
  const den = trapz(chords, ys);
  const twistBar = den > 0
    ? trapz(aeroTwists.map((t, i) => t * chords[i]), ys) / den : 0;

  /* PASS 2 — the loading. */
  const out = [];
  for (let i = 0; i < ys.length; i++) {
    const y = ys[i], c = chords[i], tw = twists[i], s = sampled[i];
    const ratio = Math.min(1, Math.max(0, half > 0 ? y / half : 0));
    const cEll = (4 * sRef / (Math.PI * p.span_m)) * Math.sqrt(Math.max(0, 1 - ratio * ratio));
    const clAdd = c > 0 ? (c + cEll) / (2 * c) : 0;

    /* HALF the local twist loading: the twist component takes the ZERO LINE, not the
       ellipse, as its ideal distribution (TM-948 p.2). Using the ellipse here doubles
       the effect of washout — the lever that decides inboard vs outboard stall. */
    const clBasic = 0.5 * s[1] * (aeroTwists[i] - twistBar);

    out.push({ y, eta: half > 0 ? y / half : 0, chord: c, twistDeg: tw,
               reynolds: reLocals[i], clAdd, clBasic, clMax: s[0],
               cm0: s[2], xC4: xLes[i] + 0.25 * c });
  }
  return out;
}

/** Wing CLmax and where stall begins. Mirrors spanwise.solve_stall:
 *    CL_max_wing = min over y of [ (cl_max(y) - cl_basic(y)) / cl_add(y) ] */
export function solveStall(params, reynoldsMac, curves) {
  const sts = stations(params, reynoldsMac, curves);
  let bestCl = Infinity, best = sts[0];
  for (const st of sts) {
    if (st.clAdd <= 0) continue;
    const clStall = (st.clMax - st.clBasic) / st.clAdd;
    if (clStall < bestCl) { bestCl = clStall; best = st; }
  }
  if (!Number.isFinite(bestCl) || bestCl <= 0) {
    throw new Error(`no station can carry positive lift before stalling (${bestCl})`);
  }
  const root = sts[0];
  return {
    clMaxWing: bestCl,
    stallEta: best.eta,
    stallIsOutboard: best.eta >= OUTBOARD_STALL_FRAC,
    marginAtRoot: root.clMax - (bestCl * root.clAdd + root.clBasic),
    stations: sts,
  };
}
