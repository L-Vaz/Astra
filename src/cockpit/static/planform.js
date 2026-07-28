/* The browser's mirror of domain_packs/bwb/schema.py's planform algebra — ONE copy.
 *
 * It existed five times (schema.py, server.py:_planform, aero_mirror.js, cockpit.js,
 * stage1.js) and the station algebra twice, with stage1.js a literal hand copy of
 * cockpit.js. stage1.js even documented the duplication — "if the BWBParams schema
 * changes, update both" — while nothing enforced it (S7-I1).
 *
 * The copies were faithful when measured (bit-identical station tables across all 6
 * fields x 19 stations, and planform area/MAC/AR agreeing with schema.py to 0.0), so
 * this is drift PREVENTION, not a correction. The one boundary that cannot be
 * deduplicated is Python <-> JS, and that pair is parity-tested permanently by
 * tests/js/planform_parity.mjs and tests/js/station_parity.mjs.
 *
 * Every input is numerically coerced AT THIS BOUNDARY. cockpit.js coerced some fields
 * and not others: `p.root_chord_m` was un-coerced in the station builder while its
 * neighbours were, so string params produced chords like "0.55-0.03666666666666667"
 * and silently deformed the preview while the numbers still read fine (S7-I4).
 */

const N1 = 6;   // centre-body stations
const N2 = 12;  // outer-panel stations

/** Coerce a BWBParams-shaped object to numbers once, so nothing downstream can
 *  concatenate strings by accident. */
export function numeric(p) {
  const out = {};
  for (const [k, v] of Object.entries(p)) out[k] = typeof v === 'number' ? v : +v;
  return out;
}

export function chords(params) {
  const p = numeric(params);
  const cRoot = p.root_chord_m;
  const cMid = cRoot * p.mid_chord_frac;
  return { cRoot, cMid, cTip: cMid * p.taper_outer };
}

export function halfWidths(params) {
  const p = numeric(params);
  const b1 = p.centre_body_fraction * p.span_m / 2;
  return { b1, b2: p.span_m / 2 - b1 };
}

/** Trapezoid mean aerodynamic chord — the same closed form as schema.py. */
export function trapMac(a, b) {
  return (2 / 3) * (a + b - (a * b) / (a + b));
}

export function planform(params) {
  const p = numeric(params);
  const { cRoot, cMid, cTip } = chords(p);
  const { b1, b2 } = halfWidths(p);
  const a1 = (b1 * (cRoot + cMid)) / 2;
  const a2 = (b2 * (cMid + cTip)) / 2;
  const s = 2 * (a1 + a2);
  const mac = (a1 * trapMac(cRoot, cMid) + a2 * trapMac(cMid, cTip)) / (a1 + a2);
  return { s, mac, ar: (p.span_m * p.span_m) / s, cMid, cTip, b1, b2 };
}

export function planformArea(params) { return planform(params).s; }
export function meanAeroChord(params) { return planform(params).mac; }
export function aspectRatio(params) { return planform(params).ar; }

/** Half-span station table: 19 stations, linear between them by design (the
 *  discretization deliberately differs from OpenVSP's U-spline and AeroSandbox's
 *  3 xsecs; the difference is bounded at <= 0.31% projected area — see S7-I6). */
export function stations(params) {
  const p = numeric(params);
  const pf = planform(p);
  const tanI = Math.tan((p.sweep_inner_deg * Math.PI) / 180);
  const tanO = Math.tan((p.sweep_outer_deg * Math.PI) / 180);
  const tanD = Math.tan((p.dihedral_deg * Math.PI) / 180);
  /* S1-C: the blend station has its own aerofoil, so its thickness is READ, not
     assumed to be the root/tip mean. These three arrive from the server in the
     params payload because BWBParams exposes them as computed fields. */
  const midThick = p.mid_thickness_pct;
  const midTwist =
    p.root_twist_deg + (p.tip_twist_deg - p.root_twist_deg) * p.centre_body_fraction;

  const st = [];
  for (let i = 0; i <= N1; i++) {            // centre body: 0 .. b1
    const f = i / N1;
    const y = pf.b1 * f;
    st.push({
      y,
      chord: p.root_chord_m + (pf.cMid - p.root_chord_m) * f,
      xle: y * tanI,
      z: y * tanD,
      tc: (p.root_thickness_pct + (midThick - p.root_thickness_pct) * f) / 100,
      tw: p.root_twist_deg + (midTwist - p.root_twist_deg) * f,
    });
  }
  for (let i = 1; i <= N2; i++) {            // outer wing: b1 .. b/2
    const f = i / N2;
    const y = pf.b1 + pf.b2 * f;
    st.push({
      y,
      chord: pf.cMid + (pf.cTip - pf.cMid) * f,
      xle: pf.b1 * tanI + (y - pf.b1) * tanO,
      z: y * tanD,
      tc: (midThick + (p.tip_thickness_pct - midThick) * f) / 100,
      tw: midTwist + (p.tip_twist_deg - midTwist) * f,
    });
  }
  return st;
}
