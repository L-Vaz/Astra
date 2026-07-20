import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { resolveAtmosphere } from '../../src/cockpit/static/atmosphere.js';
import { estimate } from '../../src/cockpit/static/aero_mirror.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '..', '..');
const csvPath = path.join(
  repo, 'domain_packs', 'bwb', 'data',
  'us_standard_atmosphere_1976_0_4000m.csv');
const metadataPath = path.join(
  repo, 'domain_packs', 'bwb', 'data',
  'us_standard_atmosphere_1976.metadata.json');

const lines = fs.readFileSync(csvPath, 'utf8').trim().split(/\r?\n/);
const fields = lines.shift().split(',');
const rows = lines.map(line => Object.fromEntries(
  line.split(',').map((value, i) => [fields[i], Number(value)])));
const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));
const table = {
  source: metadata.source,
  dataset_sha256: metadata.csv_sha256,
  minimum_altitude_m: metadata.minimum_altitude_m,
  maximum_altitude_m: metadata.maximum_altitude_m,
  row_interval_m: metadata.row_interval_m,
  interpolation: metadata.interpolation,
  rows,
};

const checkpoints = [
  [0, 288.15, 1.224999155887712, 1.460719600888937e-5],
  [1000, 281.6510223716947, 1.111658985055827, 1.581285719089361e-5],
  [1275, 279.8641638372115, 1.081960014904785, 1.616622035168617e-5],
  [4000, 262.1663502320318, 0.819346308654904, 2.027457770975855e-5],
];

for (const [altitude, temperature, density, nu] of checkpoints) {
  const state = resolveAtmosphere(altitude, table);
  assert.ok(Math.abs(state.temperature_k - temperature) < 1e-9);
  assert.ok(Math.abs(state.density_kgm3 - density) < 1e-11);
  assert.ok(Math.abs(state.kinematic_viscosity_m2s - nu) < 1e-15);
  assert.ok(Math.abs(state.kinematic_viscosity_m2s -
    state.dynamic_viscosity_kgms / state.density_kgm3) < 1e-18);
}

const interpolated = resolveAtmosphere(1275, table);
assert.equal(interpolated.interpolation.lower_table_altitude_m, 1200);
assert.equal(interpolated.interpolation.upper_table_altitude_m, 1300);
assert.equal(interpolated.interpolation.fraction, 0.75);
assert.equal(interpolated.interpolation.exact_row, false);

assert.throws(() => resolveAtmosphere(-1, table), /outside/);
assert.throws(() => resolveAtmosphere(4001, table), /outside/);
assert.throws(() => resolveAtmosphere(null, table), /finite numeric/);
assert.throws(() => resolveAtmosphere('', table), /finite numeric/);

const params = {
  span_m: 1.9820624179302297,
  root_chord_m: 0.7043732911375002,
  centre_body_fraction: 0.3,
  mid_chord_frac: 0.55,
  taper_outer: 0.45,
  sweep_inner_deg: 38,
  sweep_outer_deg: 22,
  root_thickness_pct: 14,
  tip_thickness_pct: 9,
  root_twist_deg: 2,
  tip_twist_deg: -3,
  dihedral_deg: 2,
};
const panel = estimate(params, {
  atmosphere: resolveAtmosphere(1275, table),
  v: 25,
  w: 49.05,
  launch: 'hand',
});
assert.ok(Math.abs(panel.reMac - 642731.9151803288) < 1e-6);
assert.ok(Math.abs(panel.tipRe - 269593.61211841356) < 1e-7);
assert.ok(Math.abs(panel.ldCruise - 14.691544825265458) < 1e-12);

console.log('JavaScript atmosphere parity: PASS');
