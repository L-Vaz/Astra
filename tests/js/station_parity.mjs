/* The station table: JS planform.js vs the Python fixture (S7-I1).
 *
 * The JS interpolates linearly between 19 stations; OpenVSP lofts a spline in U and
 * AeroSandbox uses 3 xsecs. Those discretizations differ BY DESIGN and are bounded at
 * <= 0.31% projected area (S7-I6) — this file checks the JS against the same station
 * definition in Python, not against the other two discretizations.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as PLANFORM from '../../src/cockpit/static/planform.js';

const fixture = JSON.parse(
  readFileSync(new URL('./planform_fixture.json', import.meta.url), 'utf8'));

const TOL = 1e-12;
const FIELDS = ['y', 'chord', 'xle', 'z', 'tc', 'tw'];

let stationsChecked = 0;
for (const c of fixture.cases) {
  const js = PLANFORM.stations(c.params);
  assert.equal(js.length, c.stations.length,
    `station count: JS ${js.length} vs Python ${c.stations.length}`);
  for (let i = 0; i < js.length; i++) {
    for (const f of FIELDS) {
      const a = js[i][f];
      const b = c.stations[i][f];
      const scale = Math.max(1, Math.abs(b));
      assert.ok(Math.abs(a - b) <= TOL * scale,
        `station ${i} ${f}: JS ${a} vs Python ${b}`);
    }
    stationsChecked += 1;
  }
}

console.log(`station parity OK: ${stationsChecked} stations across `
  + `${fixture.cases.length} geometries`);
