/* Python schema.py <-> JS planform.js, the one boundary that cannot be deduplicated.
 *
 * The five copies of this algebra are now one per language (S7-I1), which leaves
 * exactly one cross-language pair. It is parity-tested permanently, from BOTH sides:
 * this file asserts the JS against a fixture generated from Python, and
 * tests/test_js_parity.py::test_planform_fixture_matches_python regenerates that
 * fixture and asserts it is byte-identical to the committed one — so a Python change
 * that is not mirrored fails on the Python side too.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as PLANFORM from '../../src/cockpit/static/planform.js';

const fixture = JSON.parse(
  readFileSync(new URL('./planform_fixture.json', import.meta.url), 'utf8'));

const TOL = 1e-12;

function close(actual, expected, what) {
  const scale = Math.max(1, Math.abs(expected));
  assert.ok(Math.abs(actual - expected) <= TOL * scale,
    `${what}: JS ${actual} vs Python ${expected}`);
}

let checked = 0;
for (const c of fixture.cases) {
  const pf = PLANFORM.planform(c.params);
  close(pf.s, c.planform_area_m2, 'planform area');
  close(pf.mac, c.mean_aero_chord_m, 'MAC');
  close(pf.ar, c.aspect_ratio, 'aspect ratio');
  close(pf.cMid, c.chords[1], 'mid chord');
  close(pf.cTip, c.chords[2], 'tip chord');
  close(pf.b1, c.half_widths[0], 'b1');
  close(pf.b2, c.half_widths[1], 'b2');
  checked += 1;
}

/* S7-I4: the station builder must be type-agnostic at its boundary. `root_chord_m`
   was the one un-coerced field, so string params produced chords like
   "0.55-0.03666666666666667" and deformed the preview while the numbers still read
   fine. Unreachable through the server today; one value-passthrough from live. */
const numericParams = fixture.cases[0].params;
const stringParams = Object.fromEntries(
  Object.entries(numericParams).map(([k, v]) => [k, String(v)]));
assert.deepEqual(PLANFORM.stations(stringParams), PLANFORM.stations(numericParams),
  'station table differs when params arrive as strings');
assert.deepEqual(PLANFORM.planform(stringParams), PLANFORM.planform(numericParams),
  'planform differs when params arrive as strings');

console.log(`planform parity OK: ${checked} geometries, plus string-param coercion`);
