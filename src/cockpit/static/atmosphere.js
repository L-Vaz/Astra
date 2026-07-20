/* Table-driven U.S. Standard Atmosphere 1976 mirror.
 * The server supplies the canonical rows; saved calculations remain
 * server-authoritative. Kinematic viscosity is always derived as nu = mu/rho.
 */

const INTERPOLATED_FIELDS = [
  'geopotential_altitude_m', 'temperature_k', 'pressure_pa',
  'density_kgm3', 'dynamic_viscosity_kgms',
];

const lerp = (lower, upper, fraction) => lower + fraction * (upper - lower);

export function resolveAtmosphere(altitudeM, table) {
  if (altitudeM === null || altitudeM === undefined ||
      (typeof altitudeM === 'string' && altitudeM.trim() === '')) {
    throw new TypeError('geometric altitude must be a finite numeric value');
  }
  const altitude = Number(altitudeM);
  if (!table || !Array.isArray(table.rows) || table.rows.length < 2) {
    throw new Error('standard-atmosphere table is unavailable');
  }
  const minimum = Number(table.minimum_altitude_m);
  const maximum = Number(table.maximum_altitude_m);
  if (!Number.isFinite(altitude)) {
    throw new TypeError('geometric altitude must be a finite numeric value');
  }
  if (altitude < minimum || altitude > maximum) {
    throw new RangeError(`geometric altitude ${altitudeM} outside [${minimum}, ${maximum}] m`);
  }

  const rows = table.rows;
  let lo = 0, hi = rows.length;
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (Number(rows[mid].geometric_altitude_m) < altitude) lo = mid + 1;
    else hi = mid;
  }

  let lower, upper, fraction;
  if (lo < rows.length && Number(rows[lo].geometric_altitude_m) === altitude) {
    lower = upper = rows[lo];
    fraction = 0;
  } else {
    lower = rows[lo - 1];
    upper = rows[lo];
    fraction = (altitude - Number(lower.geometric_altitude_m)) /
      (Number(upper.geometric_altitude_m) - Number(lower.geometric_altitude_m));
  }

  const values = {};
  for (const field of INTERPOLATED_FIELDS) {
    values[field] = lerp(Number(lower[field]), Number(upper[field]), fraction);
  }
  values.kinematic_viscosity_m2s =
    values.dynamic_viscosity_kgms / values.density_kgm3;
  return {
    geometric_altitude_m: altitude,
    ...values,
    temperature_c: values.temperature_k - 273.15,
    source: table.source,
    dataset_sha256: table.dataset_sha256,
    interpolation: {
      lower_table_altitude_m: Number(lower.geometric_altitude_m),
      upper_table_altitude_m: Number(upper.geometric_altitude_m),
      fraction,
      exact_row: lower === upper,
    },
  };
}
