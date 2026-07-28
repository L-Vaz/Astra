/* Schrenk in the browser vs Schrenk in Python (S1-C2).
 *
 * Every expected value comes from tests/js/spanwise_fixture.json, which is generated
 * by verification/generate_spanwise_fixture.py from domain_packs/bwb/spanwise.py. The
 * JS is never consulted when producing it. That is deliberate: the parity test this
 * one is modelled on had its expectation copied from the JS's own output, so it
 * compared the mirror against itself and passed for months while two real defects
 * (skin friction 9.5% low, LE sweep averaged in angle space) sat underneath.
 *
 * Before this file existed the browser ran the deleted CL_MAX_2D * 0.90 constants and
 * was measured 1.1208 against the server's 1.0897 — 2.86% optimistic on the number
 * that decides whether the aircraft can be hand-launched.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import * as SPANWISE from '../../src/cockpit/static/spanwise.js';
import { clMax3d, estimate, stallSolution } from '../../src/cockpit/static/aero_mirror.js';
import { resolveAtmosphere } from '../../src/cockpit/static/atmosphere.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '..', '..');
const fixture = JSON.parse(
  fs.readFileSync(path.join(here, 'spanwise_fixture.json'), 'utf8'));

/* Same atmosphere table the cockpit loads, read straight from the committed CSV. */
const dataDir = path.join(repo, 'domain_packs', 'bwb', 'data');
const lines = fs.readFileSync(
  path.join(dataDir, 'us_standard_atmosphere_1976_0_4000m.csv'), 'utf8')
  .trim().split(/\r?\n/);
const fields = lines.shift().split(',');
const metadata = JSON.parse(fs.readFileSync(
  path.join(dataDir, 'us_standard_atmosphere_1976.metadata.json'), 'utf8'));
const table = {
  source: metadata.source,
  dataset_sha256: metadata.csv_sha256,
  minimum_altitude_m: metadata.minimum_altitude_m,
  maximum_altitude_m: metadata.maximum_altitude_m,
  row_interval_m: metadata.row_interval_m,
  interpolation: metadata.interpolation,
  rows: lines.map(line => Object.fromEntries(
    line.split(',').map((value, i) => [fields[i], Number(value)]))),
};

/* Tolerances. The two sides run the same double-precision arithmetic in the same
   order, so agreement should be at round-off; these are three orders of magnitude
   tighter than any physically meaningful difference, which is the point — this test
   catches a changed FORMULA, not a changed aircraft. */
const TOL_REL = 1e-11;
const TOL_ABS = 1e-12;

function close(a, b, what) {
  assert.equal(typeof a, 'number', `${what}: JS produced ${a}`);
  const tol = Math.max(TOL_ABS, TOL_REL * Math.abs(b));
  assert.ok(Math.abs(a - b) <= tol,
    `${what}: JS ${a} vs Python ${b} (diff ${Math.abs(a - b)}, tol ${tol})`);
}

let stationsChecked = 0;
for (const c of fixture.cases) {
  const curves = c.curves;
  const p = c.params;
  assert.ok(SPANWISE.hasCurves(curves, p.root_airfoil, p.mid_airfoil, p.tip_airfoil),
    `${c.label}: fixture did not carry all three section curves`);

  /* --- the Schrenk algebra alone, at a fixed Reynolds number --- */
  const exp = c.at_fixed_reynolds;
  const sol = SPANWISE.solveStall(p, c.reynolds_mac, curves);

  assert.equal(sol.stations.length, exp.n_stations,
    `${c.label}: station count JS ${sol.stations.length} vs Python ${exp.n_stations} `
    + '— the cosine grid or the panel-break insertion has drifted');
  close(sol.clMaxWing, exp.cl_max_wing, `${c.label} cl_max_wing`);
  close(sol.stallEta, exp.stall_eta, `${c.label} stall_eta`);
  close(sol.marginAtRoot, exp.margin_at_root, `${c.label} margin_at_root`);
  assert.equal(sol.stallIsOutboard, exp.stall_is_outboard,
    `${c.label}: outboard-stall flag disagrees`);

  for (const s of exp.sample_stations) {
    const js = sol.stations[s.i];
    close(js.y, s.y, `${c.label} station ${s.i} y`);
    close(js.eta, s.eta, `${c.label} station ${s.i} eta`);
    close(js.chord, s.chord, `${c.label} station ${s.i} chord`);
    close(js.twistDeg, s.twist_deg, `${c.label} station ${s.i} twist_deg`);
    close(js.reynolds, s.reynolds, `${c.label} station ${s.i} reynolds`);
    close(js.clAdd, s.cl_add, `${c.label} station ${s.i} cl_add`);
    close(js.clBasic, s.cl_basic, `${c.label} station ${s.i} cl_basic`);
    close(js.clMax, s.cl_max, `${c.label} station ${s.i} cl_max`);
    close(js.cm0, s.cm0, `${c.label} station ${s.i} cm0`);
    close(js.xC4, s.x_c4, `${c.label} station ${s.i} x_c4`);
    stationsChecked += 1;
  }

  /* Uniform twist => the chord-weighted zero-lift reference equals the local twist
     everywhere, so the basic loading is identically zero. A mirror that weighted the
     reference by span instead of chord, or dropped it, cannot reproduce this. */
  if (c.label === 'uniform-twist') {
    for (const st of sol.stations) {
      assert.ok(Math.abs(st.clBasic) < 1e-14,
        `uniform twist must give zero basic loading, got ${st.clBasic} at eta ${st.eta}`);
    }
  }

  /* --- the sweep patch, and the self-consistent fixed point --- */
  close(clMax3d(p, c.reynolds_mac, curves), exp.cl_max_3d, `${c.label} clMax3d (sweep patch)`);

  const sc = c.self_consistent;
  const atm = resolveAtmosphere(sc.altitude_m, table);
  const js = stallSolution(p, atm, c.weight_n, curves);
  close(js.vStall, sc.v_stall_ms, `${c.label} v_stall_ms`);
  close(js.clMax3d, sc.cl_max_3d, `${c.label} cl_max_3d`);
  close(js.stall.stallEta, sc.stall_eta, `${c.label} self-consistent stall_eta`);
  assert.equal(js.stall.stallIsOutboard, sc.stall_is_outboard,
    `${c.label}: self-consistent outboard flag disagrees`);
}

/* No curves => NULL, not a number. The whole point of deleting CL_MAX_2D is that the
   browser must never show a stall speed it cannot justify; a panel that silently fell
   back to a constant when the fetch was in flight would reproduce the exact defect
   this item retired, intermittently, which is worse than reproducing it always. */
{
  const c = fixture.cases[0];
  const atm = resolveAtmosphere(c.self_consistent.altitude_m, table);
  const panel = estimate(c.params, { atmosphere: atm, v: 20, w: c.weight_n,
                                     launch: 'hand' });   // no sectionCurves
  assert.equal(panel.clMax3d, null, 'CLmax without curves must be null');
  assert.equal(panel.vStall, null, 'stall speed without curves must be null');
  assert.equal(panel.launchOk, null,
    'the launch verdict must not be decided from a missing stall speed');
  assert.ok(Number.isFinite(panel.ldCruise),
    'the drag half of the panel must still work without section curves');
}

console.log(`spanwise (Schrenk) parity OK: ${fixture.cases.length} geometries, `
  + `${stationsChecked} sampled stations, stall fixed point included`);
