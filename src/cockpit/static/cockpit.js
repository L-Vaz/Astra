/* Astra Aero Design Cockpit — frontend.
 *
 * The three.js model is a parametric lofted APPROXIMATION built from the same
 * planform definitions as the OpenVSP builder; it morphs live while sliders
 * move. The authoritative STL replaces it only after a successful OpenVSP
 * rebuild (Apply Revision). All instant numbers are deterministic equation
 * mirrors of domain_packs/bwb — the server re-validates on Apply.
 */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import * as AERO from './aero_mirror.js?v=c1';

const MU_AIR = 1.81e-5;
const PAYLOAD_FRACTION = 0.30;
const TIP_CHORD_MIN = 0.04;
const AR_RANGE = [2.5, 10.0];

/* ------------------------------------------------------------------ state */
const S = {
  sid: null,
  schema: null,          // BWBParams json-schema properties
  mission: null,         // working mission
  savedMission: null,    // last saved mission
  params: null,          // working geometry params
  applied: null,         // params of latest valid built geometry (or plan baseline)
  original: null,        // params at cockpit open
  appliedIsBuilt: false, // whether `applied` has an OpenVSP STL behind it
  locks: {},
  geomVersion: 0,
  stabilityPref: 'normal',
  sizing: null,
  assumptions: [],
  fast: null,
  fastForVersion: null,
  versions: {},
  ghostOn: false,
  halfOn: false,
  candidates: [],
  candGenerated: null,
  browse: false,          // Designs tab: viewport carousel mode
  candIdx: 0,
  preBrowseParams: null,  // params to restore when leaving browse without adopting
  hudOpen: false,         // design-details dropdown expanded?
};

const PARAM_META = {
  span_m:               { label: 'Wingspan', unit: 'm', dp: 3, info: 'Tip-to-tip span. Drives aspect ratio and induced drag.',
                          aero: 'wider ⇒ induced drag ↓ (L/D ↑) in exchange for root bending ↑' },
  root_chord_m:         { label: 'Root chord', unit: 'm', dp: 3, info: 'Centre-body root chord. Sets Reynolds number and internal volume.',
                          aero: 'bigger ⇒ wing loading ↓ ⇒ stall ↓, but wetted area ↑ (L/D ↓ at cruise)' },
  centre_body_fraction: { label: 'Centre-body fraction', unit: 'of span', dp: 3, info: 'Centre-body width / span. Bigger = more payload volume, less efficient span.',
                          aero: 'wider body ⇒ volume ↑ in exchange for span efficiency ↓' },
  mid_chord_frac:       { label: 'Mid-chord fraction', unit: 'of root', dp: 3, info: 'Chord at the body/wing blend as a fraction of root chord.',
                          aero: 'bigger ⇒ inner wing carries more lift; blends body↔wing loading' },
  taper_outer:          { label: 'Outer taper ratio', unit: 'tip/blend', dp: 3, info: 'Tip chord / blend chord. Too low makes an unmanufacturable, stall-prone tip.',
                          aero: 'lower taper ⇒ lighter tip, but tip Re ↓ (separation risk) and sharper stall' },
  sweep_inner_deg:      { label: 'Inner LE sweep', unit: 'deg', dp: 1, info: 'Centre-body leading-edge sweep. Moves the aerodynamic centre aft.',
                          aero: 'more sweep ⇒ neutral point aft (stability ↑) in exchange for CLmax ↓ (stall speed ↑)' },
  sweep_outer_deg:      { label: 'Outer LE sweep', unit: 'deg', dp: 1, info: 'Outer panel sweep. With washout, provides pitch stability for a tailless BWB.',
                          aero: 'more sweep ⇒ neutral point aft (stability ↑) in exchange for CLmax ↓ (stall speed ↑)' },
  root_thickness_pct:   { label: 'Root thickness', unit: '% t/c', dp: 1, info: 'Root airfoil thickness ratio. Structure/volume vs drag.',
                          aero: 'thicker ⇒ CD0 ↑ (L/D ↓) in exchange for spar depth & volume ↑' },
  tip_thickness_pct:    { label: 'Tip thickness', unit: '% t/c', dp: 1, info: 'Tip airfoil thickness ratio. Must not exceed root thickness.',
                          aero: 'thicker ⇒ CD0 ↑ slightly; stiffer tip' },
  root_twist_deg:       { label: 'Root incidence', unit: 'deg', dp: 1, info: 'Root setting angle relative to the body datum.',
                          aero: 'sets cruise trim attitude; more incidence ⇒ body flies flatter' },
  tip_twist_deg:        { label: 'Tip twist (washout)', unit: 'deg', dp: 1, info: 'Negative = washout. A tailless BWB needs washout, never wash-in.',
                          aero: 'more washout ⇒ docile stall + tailless trim, in exchange for twist drag at cruise' },
  dihedral_deg:         { label: 'Dihedral', unit: 'deg', dp: 1, info: 'Spanwise upward angle. Adds roll stability.',
                          aero: 'more dihedral ⇒ roll stability ↑; negligible drag cost at these angles' },
};
const PRIMARY_REAL = ['span_m', 'sweep_outer_deg', 'root_thickness_pct'];
const ADVANCED = ['root_chord_m', 'centre_body_fraction', 'mid_chord_frac', 'taper_outer',
  'sweep_inner_deg', 'tip_thickness_pct', 'root_twist_deg', 'tip_twist_deg', 'dihedral_deg'];

/* ------------------------------------------------- deterministic math mirror */
function planform(p) {
  const cr = +p.root_chord_m, span = +p.span_m;
  const cMid = cr * p.mid_chord_frac, cTip = cMid * p.taper_outer;
  const b1 = p.centre_body_fraction * span / 2, b2 = span / 2 - b1;
  const s = 2 * (b1 * (cr + cMid) / 2 + b2 * (cMid + cTip) / 2);
  const trapMac = (a, b) => (2 / 3) * (a + b - a * b / (a + b));
  const a1 = b1 * (cr + cMid) / 2, a2 = b2 * (cMid + cTip) / 2;
  const mac = (a1 * trapMac(cr, cMid) + a2 * trapMac(cMid, cTip)) / (a1 + a2);
  return { s, mac, ar: span * span / s, cMid, cTip, b1, b2 };
}
const isaDensity = (alt) => 1.225 * Math.pow(1 - 2.25577e-5 * alt, 4.2559);

function derived(mission, p) {
  const pf = planform(p);
  const rho = isaDensity(+mission.cruise_altitude_m);
  const v = +mission.cruise_speed_ms;
  const mtowGiven = mission.mtow_kg != null && mission.mtow_kg !== '';
  const mtow = mtowGiven ? +mission.mtow_kg : +mission.payload_kg / PAYLOAD_FRACTION;
  const q = 0.5 * rho * v * v, w = mtow * 9.81;
  return { ...pf, rho, q, mtow, mtowGiven, w,
    cl: w / (q * pf.s), re: rho * v * pf.mac / MU_AIR, wl: mtow / pf.s };
}

function bounds(key) {
  const prop = S.schema[key] || {};
  return [prop.minimum ?? prop.exclusiveMinimum ?? 0,
          prop.maximum ?? prop.exclusiveMaximum ?? 1e9];
}

/* Mirrors BWBParams.cross_checks — server re-validates on Apply. */
function crossChecks(p) {
  const issues = [];
  const pf = planform(p);
  if (pf.cTip < TIP_CHORD_MIN) issues.push({
    params: ['taper_outer', 'mid_chord_frac', 'root_chord_m'],
    message: `tip chord ${(pf.cTip * 1000).toFixed(0)} mm is below the ${TIP_CHORD_MIN * 1000} mm manufacturable minimum` });
  if (+p.tip_thickness_pct > +p.root_thickness_pct) issues.push({
    params: ['tip_thickness_pct', 'root_thickness_pct'],
    message: 'tip airfoil is thicker than the root airfoil' });
  if (pf.ar < AR_RANGE[0] || pf.ar > AR_RANGE[1]) issues.push({
    params: ['span_m', 'root_chord_m'],
    message: `aspect ratio ${pf.ar.toFixed(2)} outside sane BWB range [${AR_RANGE[0]}, ${AR_RANGE[1]}]` });
  if (+p.tip_twist_deg > +p.root_twist_deg) issues.push({
    params: ['tip_twist_deg', 'root_twist_deg'],
    message: 'tip twist exceeds root twist (wash-in) — destabilizing for a tailless BWB' });
  for (const k of Object.keys(PARAM_META)) {
    const [lo, hi] = bounds(k);
    if (+p[k] < lo - 1e-9 || +p[k] > hi + 1e-9) issues.push({
      params: [k], message: `${PARAM_META[k].label} ${(+p[k]).toFixed(3)} outside permitted range [${lo}, ${hi}] ${PARAM_META[k].unit}` });
  }
  return issues;
}

/* -------------------------------------------------------- rebalancing rules */
const locked = (k) => !!S.locks[k];

function setSref(target) {
  if (locked('root_chord_m')) {
    toast('err', 'Reference-area rule scales the root chord, which is locked. Unlock root chord or edit chords directly.');
    return false;
  }
  const factor = target / planform(S.params).s;
  S.params.root_chord_m *= factor;
  return clampWithNotice('root_chord_m');
}
function setAR(target) {
  if (locked('span_m') || locked('root_chord_m')) {
    const who = locked('span_m') ? 'wingspan' : 'root chord';
    toast('err', `Aspect-ratio rule adjusts span and root chord at fixed area, but ${who} is locked.`);
    return false;
  }
  const s0 = planform(S.params).s;
  S.params.span_m = Math.sqrt(target * s0);
  clampWithNotice('span_m');
  const sNow = planform(S.params).s;           // span change altered S
  S.params.root_chord_m *= s0 / sNow;          // scale chords to restore S
  return clampWithNotice('root_chord_m');
}
function setCentreWidth(widthM) {
  if (locked('centre_body_fraction')) {
    toast('err', 'Centre-body width edits centre-body fraction, which is locked.');
    return false;
  }
  S.params.centre_body_fraction = widthM / S.params.span_m;
  return clampWithNotice('centre_body_fraction');
}
function clampWithNotice(k) {
  const [lo, hi] = bounds(k);
  const v = +S.params[k];
  if (v < lo || v > hi) {
    S.params[k] = Math.min(hi, Math.max(lo, v));
    toast('warn', `${PARAM_META[k].label} clamped to its permitted range [${lo}–${hi} ${PARAM_META[k].unit}] — the rule hit a limit.`);
  }
  return true;
}

/* -------------------------------------------------------------------- three */
let renderer, scene, cameraP, cameraO, camera, controls, modelGroup;
let previewMesh, stlMesh, ghostMesh, symPlane;
const clipPlane = new THREE.Plane(new THREE.Vector3(0, 0, -1), 0);

function initThree() {
  const canvas = document.getElementById('canvas3d');
  renderer = new THREE.WebGLRenderer({ canvas, antialias: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.localClippingEnabled = false;
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0e1217);

  cameraP = new THREE.PerspectiveCamera(45, 1, 0.01, 100);
  const oScale = 1.2;
  cameraO = new THREE.OrthographicCamera(-oScale, oScale, oScale, -oScale, 0.01, 100);
  camera = cameraP;

  controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;

  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const key = new THREE.DirectionalLight(0xffffff, 1.1); key.position.set(2, 3, 2);
  const fill = new THREE.DirectionalLight(0x88aaff, 0.4); fill.position.set(-2, 1, -2);
  scene.add(key, fill);

  const grid = new THREE.GridHelper(4, 40, 0x2a3442, 0x1c242f);
  grid.position.y = -0.4;
  scene.add(grid);
  scene.add(new THREE.AxesHelper(0.3));

  // model space is VSP convention (x aft, y span, z up) -> rotate into three's y-up
  modelGroup = new THREE.Group();
  modelGroup.rotation.x = -Math.PI / 2;
  scene.add(modelGroup);

  symPlane = new THREE.Mesh(
    new THREE.PlaneGeometry(2.5, 1.2),
    new THREE.MeshBasicMaterial({ color: 0x4da3ff, transparent: true, opacity: 0.08,
      side: THREE.DoubleSide, depthWrite: false }));
  symPlane.visible = false;
  scene.add(symPlane);

  setView('iso');
  const resize = () => {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    renderer.setSize(w, h, false);
    cameraP.aspect = w / h; cameraP.updateProjectionMatrix();
    const a = w / h;
    cameraO.left = -oScale * a; cameraO.right = oScale * a; cameraO.updateProjectionMatrix();
  };
  new ResizeObserver(() => { resize(); requestRender(); }).observe(canvas);
  resize();
  controls.addEventListener('change', requestRender);
  // render on demand — a continuous 60 fps loop starves software WebGL
  (function loop() {
    requestAnimationFrame(loop);
    const moved = controls.update();
    if (moved || needsRender) { renderer.render(scene, camera); needsRender = false; }
  })();
}
let needsRender = true;
function requestRender() { needsRender = true; }

/* NACA 4-series symmetric section, closed trailing edge; returns a closed loop
 * (TE -> upper -> LE -> lower -> TE), 2*NX-1 points. */
const NX = 14;
function sectionLoop(chord, tc, twistDeg, xle, y, z) {
  const pts = [];
  const t = tc, th = twistDeg * Math.PI / 180;
  const yt = (x) => 5 * t * (0.2969 * Math.sqrt(x) - 0.1260 * x - 0.3516 * x * x
    + 0.2843 * x ** 3 - 0.1036 * x ** 4);
  const xs = [];
  for (let i = 0; i < NX; i++) xs.push(0.5 * (1 - Math.cos(Math.PI * i / (NX - 1)))); // 0..1
  for (let i = NX - 1; i >= 0; i--) pts.push([xs[i], yt(xs[i])]);       // TE -> LE upper
  for (let i = 1; i < NX; i++) pts.push([xs[i], -yt(xs[i])]);           // LE -> TE lower
  const out = [];
  for (const [xc, zc] of pts) {
    // scale, rotate about quarter chord (positive twist = leading edge up)
    const dx = (xc - 0.25) * chord, dz = zc * chord;
    const xr = 0.25 * chord + dx * Math.cos(th) + dz * Math.sin(th);
    const zr = -dx * Math.sin(th) + dz * Math.cos(th);
    out.push(xle + xr, y, z + zr);
  }
  return out; // length (2*NX-1)*3
}

function stations(p) {
  const pf = planform(p);
  const tanI = Math.tan(p.sweep_inner_deg * Math.PI / 180);
  const tanO = Math.tan(p.sweep_outer_deg * Math.PI / 180);
  const tanD = Math.tan(p.dihedral_deg * Math.PI / 180);
  const midThick = (+p.root_thickness_pct + +p.tip_thickness_pct) / 2;
  const midTwist = +p.root_twist_deg + (+p.tip_twist_deg - +p.root_twist_deg) * p.centre_body_fraction;
  const st = [];
  const N1 = 6, N2 = 12;
  for (let i = 0; i <= N1; i++) {         // centre body: 0 .. b1
    const f = i / N1, y = pf.b1 * f;
    st.push({ y, chord: p.root_chord_m + (pf.cMid - p.root_chord_m) * f,
      xle: y * tanI, z: y * tanD,
      tc: (+p.root_thickness_pct + (midThick - p.root_thickness_pct) * f) / 100,
      tw: +p.root_twist_deg + (midTwist - p.root_twist_deg) * f });
  }
  for (let i = 1; i <= N2; i++) {         // outer wing: b1 .. b/2
    const f = i / N2, y = pf.b1 + pf.b2 * f;
    st.push({ y, chord: pf.cMid + (pf.cTip - pf.cMid) * f,
      xle: pf.b1 * tanI + (y - pf.b1) * tanO, z: y * tanD,
      tc: (midThick + (+p.tip_thickness_pct - midThick) * f) / 100,
      tw: midTwist + (+p.tip_twist_deg - midTwist) * f });
  }
  return st;
}

function buildPreviewGeometry(p) {
  const half = stations(p);
  const full = [...half.slice(1).reverse().map(s => ({ ...s, y: -s.y })), ...half];
  const ring = 2 * NX - 1;
  const pos = [];
  for (const s of full) pos.push(...sectionLoop(s.chord, s.tc, s.tw, s.xle, s.y, s.z));
  const idx = [];
  for (let s = 0; s < full.length - 1; s++) {
    for (let i = 0; i < ring - 1; i++) {
      const a = s * ring + i, b = a + 1, c = a + ring, d = c + 1;
      idx.push(a, c, b, b, c, d);
    }
  }
  for (const [s, flip] of [[0, false], [full.length - 1, true]]) {  // tip caps
    const base = s * ring;
    for (let i = 1; i < ring - 1; i++) {
      if (flip) idx.push(base, base + i, base + i + 1);
      else idx.push(base, base + i + 1, base + i);
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  return g;
}

function matFor(mode, color, opacity = 1) {
  if (mode === 'wireframe') return new THREE.MeshBasicMaterial({ color, wireframe: true });
  return new THREE.MeshStandardMaterial({
    color, metalness: 0.15, roughness: 0.6, side: THREE.DoubleSide,
    transparent: opacity < 1, opacity, clippingPlanes: [] });
}

function refreshPreviewMesh() {
  const g = buildPreviewGeometry(S.params);
  if (!previewMesh) {
    previewMesh = new THREE.Mesh(g, matFor('solid', 0x4da3ff));
    modelGroup.add(previewMesh);
  } else {
    previewMesh.geometry.dispose();
    previewMesh.geometry = g;
  }
  applyDisplayModes();
}

function loadStl(version, target = 'main') {
  return new Promise((resolve) => {
    new STLLoader().load(`/api/stl/${S.sid}/${version}`, (geom) => {
      geom.computeVertexNormals();
      if (target === 'ghost') {
        if (ghostMesh) { modelGroup.remove(ghostMesh); ghostMesh.geometry.dispose(); }
        ghostMesh = new THREE.Mesh(geom, matFor('solid', 0x8899aa, 0.25));
        modelGroup.add(ghostMesh);
      } else {
        if (stlMesh) { modelGroup.remove(stlMesh); stlMesh.geometry.dispose(); }
        stlMesh = new THREE.Mesh(geom, matFor('solid', 0x3ecf8e));
        modelGroup.add(stlMesh);
      }
      applyDisplayModes();
      resolve(true);
    }, undefined, () => resolve(false));
  });
}

function applyDisplayModes() {
  const mode = document.getElementById('render-mode').value;
  const dirty = isDirty();
  const showStl = !!stlMesh && !dirty && S.appliedIsBuilt;
  if (previewMesh) {
    previewMesh.visible = !showStl;
    swapMat(previewMesh, mode, 0x4da3ff, mode === 'transparent' ? 0.45 : 1);
  }
  if (stlMesh) {
    stlMesh.visible = showStl;
    swapMat(stlMesh, mode, 0x3ecf8e, mode === 'transparent' ? 0.45 : 1);
  }
  if (ghostMesh) ghostMesh.visible = S.ghostOn;

  renderer.localClippingEnabled = S.halfOn;
  for (const m of [previewMesh, stlMesh, ghostMesh]) {
    if (m) m.material.clippingPlanes = S.halfOn ? [clipPlane] : [];
  }
  symPlane.visible = S.halfOn;

  requestRender();
  const chip = document.getElementById('model-status');
  if (showStl) {
    chip.textContent = `authoritative OpenVSP model — geometry v${S.geomVersion}`;
    chip.className = 'overlay-chip authoritative';
  } else {
    chip.textContent = dirty ? 'parametric preview — unapplied changes (approximation)'
                             : 'parametric preview (approximation — no OpenVSP build yet)';
    chip.className = 'overlay-chip preview';
  }
}
function swapMat(mesh, mode, color, opacity) {
  mesh.material.dispose();
  mesh.material = matFor(mode, color, opacity);
}

function setView(name) {
  const d = 2.2;
  const posn = { iso: [d * 0.75, d * 0.6, d * 0.75], top: [0, d, 0.001],
                 front: [-d, 0.15, 0], side: [0, 0.15, -d] }[name];
  camera.position.set(...posn);
  controls.target.set(0, 0, 0);
  controls.update();
  document.querySelectorAll('.vb[data-view]').forEach(b =>
    b.classList.toggle('active', b.dataset.view === name));
}

/* ----------------------------------------------------------------- UI build */
function fmt(v, dp = 3) { return (+v).toFixed(dp); }
function pct(a, b) { return b ? `${a >= b ? '+' : ''}${(100 * (a - b) / b).toFixed(0)}%` : '—'; }

function controlRow(key, opts = {}) {
  const meta = opts.meta || PARAM_META[key];
  const [lo, hi] = opts.range || bounds(key);
  const isDerived = !!opts.derived;
  const div = document.createElement('div');
  div.className = 'ctl' + (isDerived ? ' derived-ctl' : '');
  div.id = `ctl-${key}`;
  div.innerHTML = `
    <div class="ctl-head">
      <label>${meta.label} ${isDerived ? '<span class="derived-tag">derived · rule: ' + opts.rule + '</span>' : ''}</label>
      <span class="unit">${meta.unit}</span>
      ${isDerived ? '' : `<button class="lockbtn" title="Lock this requirement">🔓</button>`}
    </div>
    <div class="ctl-row">
      <input type="range" min="${lo}" max="${hi}" step="${(hi - lo) / 400}">
      <input type="number" min="${lo}" max="${hi}" step="any">
    </div>
    <div class="ctl-sub"><span class="cur"></span><span class="orig"></span></div>
    <div class="ctl-msg hidden"></div>
    <div class="ctl-info">${meta.info}</div>
    ${meta.aero ? `<div class="ctl-aero">≈ ${meta.aero}</div>` : ''}`;
  const range = div.querySelector('input[type=range]');
  const num = div.querySelector('input[type=number]');

  const commit = (raw, fromSlider) => {
    // number inputs sanitize garbage ("abc") to '' — treat as invalid, revert
    if (String(raw).trim() === '') {
      toast('warn', `${meta.label}: that is not a number — reverted to the current value.`);
      syncAll(); return;
    }
    let v = +raw;
    if (Number.isNaN(v)) { toast('warn', `${meta.label}: "${raw}" is not a number — reverted.`); syncAll(); return; }
    if (v < lo || v > hi) {
      const c = Math.min(hi, Math.max(lo, v));
      toast('warn', `${meta.label} ${v} ${meta.unit} is outside the permitted range ${lo}–${hi} ${meta.unit}; clamped to ${fmt(c, meta.dp)}.`);
      v = c;
    }
    if (isDerived) { if (!opts.set(v)) { syncAll(); return; } }
    else {
      if (locked(key) && !fromSlider) { toast('err', `${meta.label} is locked.`); syncAll(); return; }
      S.params[key] = v;
    }
    syncAll();
  };
  range.addEventListener('input', () => commit(range.value, true));
  num.addEventListener('change', () => commit(num.value, false));

  const lockBtn = div.querySelector('.lockbtn');
  if (lockBtn) lockBtn.addEventListener('click', () => {
    S.locks[key] = !S.locks[key];
    fetch('/api/locks', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: S.sid, locks: S.locks }) });
    toast('ok', `${meta.label} ${S.locks[key] ? 'locked — Astra and rebalancing rules will not change it' : 'unlocked'}.`);
    syncAll();
  });

  div._sync = () => {
    const v = isDerived ? opts.get() : +S.params[key];
    range.value = v; num.value = fmt(v, meta.dp);
    div.querySelector('.cur').textContent = `current ${fmt(v, meta.dp)}`;
    const ov = isDerived ? opts.getOriginal() : +S.original[key];
    div.querySelector('.orig').textContent = `original ${fmt(ov, meta.dp)}`;
    if (!isDerived) {
      const isLocked = locked(key);
      div.classList.toggle('locked', isLocked);
      range.disabled = isLocked; num.disabled = isLocked;
      if (lockBtn) { lockBtn.textContent = isLocked ? '🔒' : '🔓'; lockBtn.classList.toggle('locked', isLocked); }
    }
  };
  return div;
}

function buildGeometryTab() {
  const pane = document.getElementById('tab-geometry');
  pane.innerHTML = '<div class="group-title">Primary controls</div>';

  pane.appendChild(controlRow('span_m'));
  pane.appendChild(controlRow('sref', {
    derived: true, rule: 'scales root chord, span fixed',
    meta: { label: 'Reference area', unit: 'm²', dp: 4, info: 'Planform area from the trapezoid equations. Dragging it uniformly scales the chords.' },
    range: [0.08, 2.0],
    get: () => planform(S.params).s,
    getOriginal: () => planform(S.original).s,
    set: setSref }));
  pane.appendChild(controlRow('ar', {
    derived: true, rule: 'adjusts span + chords at fixed area',
    meta: { label: 'Aspect ratio', unit: 'b²/S', dp: 2, info: 'Higher AR lowers induced drag but grows span. Hard-gated to [2.5, 10].' },
    range: AR_RANGE,
    get: () => planform(S.params).ar,
    getOriginal: () => planform(S.original).ar,
    set: setAR }));
  pane.appendChild(controlRow('cbw', {
    derived: true, rule: 'edits centre-body fraction',
    meta: { label: 'Centre-body width', unit: 'm', dp: 3, info: 'Physical width of the centre body (fraction × span).' },
    range: [0.08, 1.4],
    get: () => S.params.centre_body_fraction * S.params.span_m,
    getOriginal: () => S.original.centre_body_fraction * S.original.span_m,
    set: setCentreWidth }));
  pane.appendChild(controlRow('sweep_outer_deg'));
  pane.appendChild(controlRow('root_thickness_pct'));

  const stab = document.createElement('div');
  stab.className = 'ctl';
  stab.innerHTML = `
    <div class="ctl-head"><label>Stability preference</label></div>
    <div class="ctl-row">
      <select id="stab-pref" style="flex:1">
        <option value="relaxed">Relaxed (SM target ~4% MAC)</option>
        <option value="normal" selected>Normal (SM target ~8% MAC)</option>
        <option value="stable">Stable (SM target ~12% MAC)</option>
      </select>
    </div>
    <div class="ctl-info">Sets the static-margin band future rebalancing/optimization biases sweep and washout toward. Changing it does not modify geometry now; static margin itself is a fast-validated quantity (VSPAERO + assumed mass model).</div>`;
  stab.querySelector('select').addEventListener('change', (e) => {
    S.stabilityPref = e.target.value;
    toast('ok', `Stability preference set to "${e.target.value}". Recorded with the next revision — no geometry change by itself.`);
  });
  pane.appendChild(stab);

  const det = document.createElement('details');
  det.className = 'advanced';
  det.innerHTML = '<summary>Advanced controls</summary>';
  for (const k of ADVANCED) det.appendChild(controlRow(k));
  pane.appendChild(det);
}

function buildMissionTab() {
  const pane = document.getElementById('tab-mission');
  pane.innerHTML = '<div class="group-title">Mission inputs</div>';
  const fields = [
    ['payload_kg', 'Payload mass', 'kg', 0.1, 10, false],
    ['cruise_speed_ms', 'Cruise speed', 'm/s', 8, 60, false],
    ['cruise_altitude_m', 'Cruise altitude', 'm AMSL', 0, 4000, false],
    ['mtow_kg', 'MTOW (optional)', 'kg', 0.5, 25, true],
    ['range_km', 'Intended range', 'km', 1, 500, true],
    ['endurance_min', 'Intended endurance', 'min', 1, 600, true],
  ];
  for (const [key, label, unit, lo, hi, optional] of fields) {
    const goal = key === 'range_km' || key === 'endurance_min';
    const div = document.createElement('div');
    div.className = 'ctl';
    div.innerHTML = `
      <div class="ctl-head"><label>${label}</label><span class="unit">${unit}</span></div>
      <div class="ctl-row"><input type="number" step="any" min="${lo}" max="${hi}"
        placeholder="${optional ? 'not set' : ''}" data-mkey="${key}" style="flex:1"></div>
      ${goal ? '<div class="goalnote" style="margin:4px 0 0">Recorded goal only — NOT evaluated. Propulsion and energy modelling are outside the current aero-only scope; this value never influences sizing.</div>' : ''}`;
    const inp = div.querySelector('input');
    inp.addEventListener('change', () => {
      const raw = inp.value.trim();
      if (raw === '') {
        if (optional) { S.mission[key] = null; syncAll(); return; }
        toast('warn', `${label} is required; keeping ${S.mission[key]}.`); inp.value = S.mission[key]; return;
      }
      let v = +raw;
      if (Number.isNaN(v)) { toast('warn', `${label}: "${raw}" is not a number — ignored.`); inp.value = S.mission[key] ?? ''; return; }
      if (v < lo || v > hi) {
        const c = Math.min(hi, Math.max(lo, v));
        toast('warn', `${label} ${v} ${unit} is outside the supported envelope ${lo}–${hi} ${unit}; clamped to ${c}. (Schema limit for small-UAV scope.)`);
        v = c;
      }
      S.mission[key] = v;
      syncAll();
    });
    pane.appendChild(div);
  }
  const launch = document.createElement('div');
  launch.className = 'ctl';
  launch.innerHTML = `
    <div class="ctl-head"><label>Launch method</label></div>
    <div class="ctl-row"><select id="launch-sel" style="flex:1">
      <option value="hand">Hand launch (wing loading ~7 kg/m²)</option>
      <option value="catapult">Catapult (~10 kg/m²)</option>
      <option value="runway">Runway (~13 kg/m²)</option>
    </select></div>
    <div class="ctl-info">Sets the initial wing-loading assumption used for sizing targets.</div>`;
  launch.querySelector('select').addEventListener('change', (e) => { S.mission.launch = e.target.value; syncAll(); });
  pane.appendChild(launch);

  const save = document.createElement('button');
  save.className = 'primary'; save.id = 'btn-save-mission';
  save.textContent = 'Save mission (new plan version)';
  save.style.width = '100%';
  save.addEventListener('click', saveMission);
  pane.appendChild(save);

  const targets = document.createElement('div');
  targets.id = 'sizing-box'; targets.style.marginTop = '14px';
  pane.appendChild(targets);
  const assum = document.createElement('div');
  assum.id = 'assumptions-box'; assum.style.marginTop = '14px';
  pane.appendChild(assum);
}

function renderSizing() {
  const t = S.sizing;
  const box = document.getElementById('sizing-box');
  box.innerHTML = '<div class="group-title">Sizing targets (from saved plan)</div>' + (t ? `
    <div class="kv">
      <span class="k">Target reference area</span><span class="v">${fmt(t.s_ref_target_m2, 4)} m²</span>
      <span class="k">Required cruise CL (target sizing)</span><span class="v">${fmt(t.required_cl, 3)}</span>
      <span class="k">Static margin target</span><span class="v">${fmt(100 * t.static_margin_target, 0)}% MAC</span>
    </div>` : '<div class="dim">no saved plan yet</div>');
  const a = document.getElementById('assumptions-box');
  a.innerHTML = '<div class="group-title">Logged assumptions</div>' +
    (S.assumptions.length
      ? '<ul style="margin:0;padding-left:17px;font-size:11px;color:var(--dim)">'
        + S.assumptions.map(x => `<li>${x}</li>`).join('') + '</ul>'
      : '<div class="dim">none</div>');
}

function buildVersionsTab() {
  const pane = document.getElementById('tab-versions');
  pane.innerHTML = '<div class="group-title">Geometry versions</div>';
  const geo = (S.versions.geometry || []).filter(v => v.recorded).reverse();
  if (!geo.length) pane.innerHTML += '<div class="dim">No built geometry yet — press Apply Revision to create v1.</div>';
  for (const v of geo) {
    const div = document.createElement('div');
    div.className = 'ver' + (v.is_latest_valid ? ' current' : '');
    const ov = v.overrides && Object.keys(v.overrides).length
      ? Object.entries(v.overrides).map(([k, x]) => `${k}=${(+x).toFixed(3)}`).join(', ')
      : 'baseline';
    div.innerHTML = `
      <div class="ver-head"><span>Geometry v${v.version}${v.is_latest_valid ? ' — current' : ''}</span>
        <span class="when">${v.created || ''}</span></div>
      <div class="ver-meta">${v.note ? v.note + ' · ' : ''}${ov} · gates ${v.passed ? 'passed' : 'FAILED'}</div>
      <div class="ver-actions">
        <button data-act="load">Load params</button>
        <button data-act="ghost">Ghost compare</button>
        <button data-act="restore">Restore as new</button>
      </div>`;
    div.querySelector('[data-act=load]').addEventListener('click', () => loadVersionParams(v.version, false));
    div.querySelector('[data-act=restore]').addEventListener('click', () => loadVersionParams(v.version, true));
    div.querySelector('[data-act=ghost]').addEventListener('click', async () => {
      const ok = await loadStl(v.version, 'ghost');
      if (ok) { S.ghostOn = true; document.getElementById('btn-ghost').classList.add('active'); applyDisplayModes();
        toast('ok', `Ghost overlay: geometry v${v.version} (grey, translucent).`); }
      else toast('err', `No STL stored for geometry v${v.version}.`);
    });
    pane.appendChild(div);
  }
  const plans = (S.versions.plan || []).filter(v => v.recorded);
  pane.innerHTML2 = '';
  const pl = document.createElement('div');
  pl.innerHTML = `<div class="group-title">Plan versions</div>
    <div class="dim">${plans.length} saved (latest v${plans.length ? plans[plans.length - 1].version : 0}). Earlier files stay on disk; restoring never deletes later work.</div>`;
  pane.appendChild(pl);
}

async function loadVersionParams(version, asRestore) {
  const r = await fetch(`/api/params/${S.sid}/${version}`);
  if (!r.ok) { toast('err', `Could not load params of geometry v${version}.`); return; }
  const { params } = await r.json();
  S.params = { ...params };
  syncAll();
  toast('ok', asRestore
    ? `Geometry v${version} staged as a NEW revision — press Apply Revision to build it as v${latestGeoVersion() + 1}. Later versions are preserved.`
    : `Parameters of geometry v${version} loaded into the editor.`);
}
function latestGeoVersion() {
  const geo = (S.versions.geometry || []).filter(v => v.recorded);
  return geo.length ? Math.max(...geo.map(v => v.version)) : 0;
}

/* ------------------------------------------------------- design candidates
 * Browsing happens in the CENTER: the viewport becomes a carousel (‹ › arrows
 * or arrow keys), the 3D preview morphs to each design, and a HUD overlay
 * shows that design's metric table, trade-offs, and actions. The sidebar
 * collapses while browsing — no card list. */
function buildDesignsTab() {
  const pane = document.getElementById('tab-designs');
  pane.innerHTML = `
    <div class="group-title">Design candidates</div>
    <div class="ctl-info">Browsing happens in the 3D viewport: flip through the
    designs with the ‹ › arrows (or ←/→ keys). Metrics, trade-offs, and
    screen/promote actions sit on the panel under the model.</div>`;
}

function setBrowseMode(on) {
  if (on === S.browse) { if (on) showCandidate(S.candIdx); return; }
  S.browse = on;
  const main = document.getElementById('main');
  if (on) {
    S.preBrowseParams = { ...S.params };
    main.classList.add('left-collapsed');
    if (!S.candidates.length) fetchCandidates().then(() => showCandidate(S.candIdx));
    else showCandidate(S.candIdx);
  } else {
    main.classList.remove('left-collapsed');
    for (const id of ['cand-prev', 'cand-next', 'cand-hud'])
      document.getElementById(id).classList.add('hidden');
    if (S.preBrowseParams) {
      S.params = { ...S.preBrowseParams };
      S.preBrowseParams = null;
      syncAll();
    }
  }
}

function showCandidate(i) {
  if (!S.candidates.length) { renderCandHud(); return; }
  S.candIdx = ((i % S.candidates.length) + S.candidates.length) % S.candidates.length;
  S.params = { ...S.candidates[S.candIdx].params };
  syncAll();
  renderCandHud();
}

function evidenceBadges(c) {
  let html = '<span class="badge instant">equation</span>';
  const geo = c.evidence.geometry;
  if (geo) html += ` <span class="badge ${geo.passed ? 'fast' : 'failedb'}">geometry ${geo.passed ? 'gated' : 'REJECTED'}</span>`;
  const fa = c.evidence.fast_aero;
  if (fa) html += ` <span class="badge ${fa.passed ? 'fast' : 'failedb'}">fast ${fa.passed ? 'screened' : 'REJECTED'}</span>`;
  if (c.status === 'promoted') html += ` <span class="badge cfd">on main line v${c.promoted_geometry_version}</span>`;
  return html;
}

function candMetrics(c) {
  const p = c.panel;
  const rows = [
    ['L/D cruise', fmt(p.ld_cruise, 1)],
    ['best L/D (at CL*)', `${fmt(p.ld_max, 1)}`],
    ['stall', `${fmt(p.v_stall_ms, 1)} m/s${p.launch_ok === false ? ' ⚠' : ''}`],
    ['span', `${fmt(p.span_m, 2)} m`],
    ['AR', fmt(p.aspect_ratio, 2)],
    ['wing loading', `${fmt(p.wing_loading_kgm2, 1)} kg/m²`],
    ['cruise CL', `${fmt(p.cl_cruise, 3)} (CL* ${fmt(p.cl_star, 2)})`],
    ['neutral pt x', `${fmt(p.x_np_m, 3)} m`],
  ];
  return rows.map(([k, v]) => `<span class="k">${k}</span><span class="v">${v}</span>`).join('');
}

function screenBlock(c) {
  const fa = c.evidence.fast_aero;
  if (!fa || !fa.passed) return '';
  const h = fa.hybrid || {};
  const p = c.panel;
  const dLd = h.ld_hybrid != null ? h.ld_hybrid - p.ld_cruise : null;
  const disagree = dLd != null && Math.abs(dLd) / p.ld_cruise > 0.15;
  return `
    <div class="cand-screen">
      <div class="group-title" style="margin-top:8px">VSPAERO vs equations</div>
      <div class="kv">
        <span class="k">L/D hybrid (CD0 eq + CDi VLM)</span>
        <span class="v">${fmt(h.ld_hybrid, 1)} <span class="dim">(predicted ${fmt(p.ld_cruise, 1)})</span></span>
        <span class="k">Oswald e measured</span>
        <span class="v">${fmt(h.oswald_e_measured, 3)} <span class="dim">(assumed ${fmt(p.oswald_e, 3)})</span></span>
        <span class="k">Neutral point x</span>
        <span class="v">${fmt(fa.x_np_m, 3)} m <span class="dim">(planform est ${fmt(p.x_np_m, 3)})</span></span>
        <span class="k">Trim α</span><span class="v">${fmt(fa.alpha_trim_deg, 2)}°</span>
      </div>
      ${disagree ? '<div class="issue" style="margin-top:6px">equations and VSPAERO disagree by >15% on L/D — an Oswald/CD0 assumption is off for this shape; trust the screened number</div>' : ''}
    </div>`;
}

function renderCandidates() {
  if (S.browse) renderCandHud();
}

function renderCandHud() {
  const hud = document.getElementById('cand-hud');
  const prev = document.getElementById('cand-prev');
  const next = document.getElementById('cand-next');
  if (!S.browse) return;
  hud.classList.remove('hidden');

  if (!S.candidates.length) {
    prev.classList.add('hidden'); next.classList.add('hidden');
    hud.innerHTML = `<div class="cand-hud-inner">
      <div class="cand-hud-title">No design candidates yet for the saved plan</div>
      <div class="cand-obj">Each candidate solves the sizing equations under a
      different objective (range, launch, span efficiency, control margin).</div>
      <button id="hud-generate" class="primary" style="margin-top:8px">Generate candidates</button>
    </div>`;
    document.getElementById('hud-generate').addEventListener('click', () => generateCandidates(false));
    return;
  }

  prev.classList.remove('hidden'); next.classList.remove('hidden');
  const c = S.candidates[S.candIdx];
  hud.innerHTML = `<div class="cand-hud-inner ${c.status}">
    <details class="cand-dd" ${S.hudOpen ? 'open' : ''}>
      <summary>
        <span class="cand-hud-title">Design ${S.candIdx + 1} of ${S.candidates.length} — ${c.title}</span>
        <span class="cand-sum-metric">L/D ${fmt(c.panel.ld_cruise, 1)}</span>
        <span class="cand-status ${c.status}">${c.status}</span>
      </summary>
      <div class="cand-badges">${evidenceBadges(c)}</div>
      <div class="cand-obj">${c.objective}</div>
      <div class="kv cand-kv">${candMetrics(c)}</div>
      ${c.constraint_notes.map(n => `<div class="cand-note">◈ ${n}</div>`).join('')}
      <ul class="cand-trades">${c.tradeoffs.map(t => `<li>${t}</li>`).join('')}</ul>
      ${screenBlock(c)}
      <details class="cand-why"><summary>Why these numbers (derivation)</summary>
        <ul>${c.provenance.map(x => `<li>${x}</li>`).join('')}</ul>
        <div class="dim" style="margin-top:4px">Assumptions: ${c.panel.assumptions.join(' · ')}</div>
      </details>
      <div class="ver-actions">
        <button data-act="screen" ${c.status === 'discarded' ? 'disabled' : ''}>Screen (VSPAERO)</button>
        <button data-act="promote" ${c.status === 'discarded' ? 'disabled' : ''}>Promote to main</button>
        <button data-act="edit">Edit this design</button>
        <button data-act="discard" ${c.status === 'discarded' || c.status === 'promoted' ? 'disabled' : ''}>Discard</button>
        <button data-act="regen" title="Replace the whole batch from the saved plan">Regenerate</button>
      </div>
    </details>
  </div>`;
  hud.querySelector('.cand-dd').addEventListener('toggle', (e) => {
    S.hudOpen = e.target.open;   // remember across design flips
  });
  hud.querySelector('[data-act=screen]').addEventListener('click', () => screenCandidate(c.key));
  hud.querySelector('[data-act=promote]').addEventListener('click', () => promoteCandidate(c.key));
  hud.querySelector('[data-act=discard]').addEventListener('click', () => discardCandidate(c.key));
  hud.querySelector('[data-act=regen]').addEventListener('click', () => generateCandidates(true));
  hud.querySelector('[data-act=edit]').addEventListener('click', () => {
    S.preBrowseParams = null;           // keep this design in the editor
    switchTab('geometry');
    toast('ok', `"${c.title}" kept in the editor — tweak it in Geometry, then Apply Revision to build it.`);
  });
}

function switchTab(name) {
  const btn = document.querySelector(`.tab[data-tab=${name}]`);
  if (btn) btn.click();
}

async function fetchCandidates() {
  const r = await fetch(`/api/candidates/${S.sid}`);
  if (!r.ok) return;
  const d = await r.json();
  S.candidates = d.candidates || [];
  S.candGenerated = d.generated;
  renderCandidates();
}

async function generateCandidates(regen) {
  busy(true, 'Solving sizing equations per objective…');
  try {
    const r = await fetch('/api/candidates/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: S.sid, regenerate: !!regen }) });
    const d = await r.json();
    if (!r.ok) { toast('err', `Candidate generation failed: ${d.detail || r.status}`); return; }
    S.candidates = d.candidates; S.candGenerated = d.generated;
    if (S.browse) showCandidate(0); else renderCandidates();
    toast('ok', `${d.candidates.length} candidates generated from the saved plan. Equation-level numbers only — screen with VSPAERO before believing any ranking.`);
  } finally { busy(false); }
}

async function screenCandidate(key) {
  busy(true, 'Building candidate in OpenVSP + VSPAERO sweep (up to ~2 min)…');
  try {
    const r = await fetch('/api/candidates/screen', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: S.sid, key }) });
    const d = await r.json();
    if (!r.ok) { toast('err', `Screening failed: ${d.detail || r.status}`); await fetchCandidates(); return; }
    S.candidates = S.candidates.map(c => (c.key === key ? d : c));
    renderCandidates();
    const h = (d.evidence.fast_aero || {}).hybrid || {};
    toast('ok', `"${d.title}" screened: hybrid L/D ${fmt(h.ld_hybrid ?? 0, 1)} (equations predicted ${fmt(d.panel.ld_cruise, 1)}).`);
  } finally { busy(false); }
}

async function promoteCandidate(key) {
  const c = S.candidates.find(x => x.key === key);
  if (!c) return;
  busy(true, `Promoting "${c.title}" — authoritative OpenVSP build on the main line…`);
  try {
    const r = await fetch('/api/candidates/promote', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: S.sid, key }) });
    const d = await r.json();
    if (!r.ok) { toast('err', `Promotion failed: ${d.detail || r.status}`); return; }
    const rep = d.report;
    S.params = { ...rep.params }; S.applied = { ...rep.params };
    if (S.browse) S.preBrowseParams = { ...rep.params };  // leaving browse keeps it
    S.appliedIsBuilt = true; S.geomVersion = d.geometry_version;
    S.fast = null; S.fastForVersion = null;
    await loadStl(d.geometry_version);
    await refreshVersions();
    await fetchCandidates();
    toast('ok', `"${c.title}" is now main geometry v${d.geometry_version} (watertight=${rep.watertight}). Downstream fast-aero/mesh/CFD invalidated — rerun them on the new design.`);
    syncAll(true);
  } finally { busy(false); }
}

async function discardCandidate(key) {
  const r = await fetch('/api/candidates/discard', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: S.sid, key }) });
  if (r.ok) {
    const d = await r.json();
    S.candidates = S.candidates.map(c => (c.key === key ? d : c));
    renderCandidates();
    toast('ok', `Candidate discarded — it stays on disk for the design history.`);
  }
}

/* -------------------------------------------------------------- right panel */
function isDirty() {
  if (!S.applied) return true;
  return Object.keys(PARAM_META).some(k =>
    Math.abs(+S.params[k] - +S.applied[k]) > 1e-9);
}
function missionDirty() {
  return JSON.stringify(S.mission) !== JSON.stringify(S.savedMission);
}

function renderDerived() {
  const d = derived(S.mission, S.params);
  const a = S.applied ? derived(S.savedMission || S.mission, S.applied) : null;
  const rows = [
    ['Air density ρ', `${fmt(d.rho, 4)} kg/m³`],
    ['Dynamic pressure q', `${fmt(d.q, 1)} Pa`],
    ['MTOW' + (d.mtowGiven ? '' : ' (assumed 30% payload fraction)'), `${fmt(d.mtow, 2)} kg`],
    ['Weight', `${fmt(d.w, 1)} N`],
    ['Reference area S', `${fmt(d.s, 4)} m²`, a && Math.abs(d.s - a.s) > 1e-6],
    ['Aspect ratio', `${fmt(d.ar, 2)}`, a && Math.abs(d.ar - a.ar) > 1e-3],
    ['Mean aero chord', `${fmt(d.mac, 4)} m`, a && Math.abs(d.mac - a.mac) > 1e-6],
    ['Tip chord', `${(d.cTip * 1000).toFixed(0)} mm`],
    ['Wing loading', `${fmt(d.wl, 2)} kg/m²`],
    ['Required cruise CL', `${fmt(d.cl, 3)}`, a && Math.abs(d.cl - a.cl) > 1e-4],
    ['Reynolds (MAC)', `${d.re.toExponential(2)}`, a && Math.abs(d.re - a.re) / a.re > 1e-3],
  ];
  document.getElementById('derived-list').innerHTML = rows.map(([k, v, ch]) =>
    `<span class="k">${k}</span><span class="v${ch ? ' changed' : ''}">${v}</span>`).join('');

  const dims = `span   ${fmt(S.params.span_m, 3)} m\nroot c ${fmt(S.params.root_chord_m, 3)} m\nMAC    ${fmt(d.mac, 3)} m\nS      ${fmt(d.s, 3)} m²\nAR     ${fmt(d.ar, 2)}${S.halfOn ? '\n— half view (display only) —' : ''}`;
  document.getElementById('dim-overlay').textContent = dims;
}

function aeroCtx(mission) {
  const rho = isaDensity(+mission.cruise_altitude_m);
  const mtowGiven = mission.mtow_kg != null && mission.mtow_kg !== '';
  const mtow = mtowGiven ? +mission.mtow_kg : +mission.payload_kg / PAYLOAD_FRACTION;
  return { rho, v: +mission.cruise_speed_ms, w: mtow * 9.81, launch: mission.launch || 'hand' };
}

function renderAeroEst() {
  const el = document.getElementById('aeroest-list');
  if (!el) return;
  let est;
  try { est = AERO.estimate(S.params, aeroCtx(S.mission)); }
  catch { el.innerHTML = '<span class="dim">geometry invalid — no estimate</span>'; return; }
  let base = null;
  if (S.applied) {
    try { base = AERO.estimate(S.applied, aeroCtx(S.savedMission || S.mission)); } catch {}
  }
  const row = (label, val, baseVal, dp, unit = '', warn = '') => {
    let delta = '';
    if (baseVal != null && Math.abs(val - baseVal) > Math.abs(baseVal) * 1e-4 + 1e-9) {
      const d = val - baseVal;
      delta = ` <span class="aer-delta">(${d > 0 ? '+' : ''}${d.toFixed(dp)})</span>`;
    }
    return `<span class="k">${label}</span><span class="v${delta ? ' changed' : ''}">` +
      `${val.toFixed(dp)}${unit}${warn}${delta}</span>`;
  };
  el.innerHTML =
    row('L/D at cruise CL', est.ldCruise, base?.ldCruise, 1) +
    row(`best L/D (at CL* ${est.clStar.toFixed(2)})`, est.ldMax, base?.ldMax, 1) +
    row('Stall speed', est.vStall, base?.vStall, 1, ' m/s',
        est.launchOk === false ? ' ⚠' : '') +
    row('Neutral point x', est.xNp, base?.xNp, 3, ' m') +
    row('CG for 8% SM', est.xCgTarget, base?.xCgTarget, 3, ' m') +
    row('Parasite CD0', est.cd0, base?.cd0, 4) +
    row('Oswald e (est)', est.e, base?.e, 3) +
    row('Root bending ~', est.bending, base?.bending, 1, ' N·m') +
    `<span class="k">Tip Reynolds</span><span class="v">${est.tipRe.toExponential(2)}` +
    `${est.tipRe < 1.5e5 ? ' ⚠ low' : ''}</span>` +
    (est.launchOk === false
      ? `<span class="k" style="color:var(--warn)">launch</span>` +
        `<span class="v" style="color:var(--warn)">stall > ${est.launchLimit} m/s ${S.mission.launch} limit</span>`
      : '');
}

function renderConsequences() {
  const el = document.getElementById('consequence-list');
  const gDirty = isDirty(), mDirty = missionDirty();
  if (!gDirty && !mDirty) { el.textContent = 'No pending changes — displayed model is the applied design.'; return; }
  const base = derived(S.savedMission || S.mission, S.applied || S.params);
  const now = derived(S.mission, S.params);
  const line = (name, a, b, f = 3, u = '') =>
    Math.abs(a - b) > Math.abs(b) * 1e-6 + 1e-12
      ? `${name}: ${fmt(b, f)} → ${fmt(a, f)}${u}  (${pct(a, b)})\n` : '';
  let txt = '';
  if (mDirty) txt += line('Dynamic pressure', now.q, base.q, 1, ' Pa');
  txt += line('Reference area S', now.s, base.s, 4, ' m²');
  txt += line('Aspect ratio', now.ar, base.ar, 2);
  txt += line('MAC', now.mac, base.mac, 4, ' m');
  txt += line('Required cruise CL', now.cl, base.cl, 3);
  txt += line('Reynolds (MAC)', now.re, base.re, 0);
  txt += `Geometry: ${gDirty ? 'CHANGED — press Apply Revision to rebuild the authoritative model' : 'unchanged'}\n`;
  txt += 'Drag & efficiency: awaiting fast-aero validation (never inferred from equations)';
  el.textContent = txt;
}

function renderIssues() {
  const issues = crossChecks(S.params);
  const el = document.getElementById('issues-list');
  document.querySelectorAll('.ctl').forEach(c => { c.classList.remove('err');
    const m = c.querySelector('.ctl-msg'); if (m) { m.classList.add('hidden'); m.textContent = ''; } });
  if (!issues.length) {
    el.innerHTML = '<div class="gate"><span class="ok">✓</span> all parameter ranges and cross-checks pass (instant check — OpenVSP gates run on Apply)</div>';
  } else {
    el.innerHTML = issues.map(i => `<div class="issue">
      <div class="who">affects: ${i.params.map(p => PARAM_META[p]?.label || p).join(', ') || 'model'}</div>
      ${i.message}</div>`).join('');
    for (const i of issues) for (const p of i.params) {
      const c = document.getElementById(`ctl-${p}`);
      if (c) { c.classList.add('err');
        const m = c.querySelector('.ctl-msg'); if (m) { m.textContent = i.message; m.classList.remove('hidden'); m.classList.add('err'); } }
    }
  }
  const apply = document.getElementById('btn-apply');
  apply.disabled = !isDirty() || issues.length > 0;
  apply.title = issues.length ? 'Fix validation issues first: ' + issues.map(i => i.message).join(' | ')
    : (isDirty() ? 'Rebuild the authoritative OpenVSP geometry with these parameters' : 'No changes to apply');
  return issues;
}

function renderFast() {
  const el = document.getElementById('fast-list');
  if (!S.fast) { el.innerHTML = '<span class="dim">not run for this geometry</span>'; return; }
  const stale = S.fastForVersion !== S.geomVersion || isDirty();
  const f = S.fast;
  el.innerHTML = `
    <span class="k">Trim α (mission CL)</span><span class="v">${fmt(f.alpha_trim_deg, 2)}°</span>
    <span class="k">L/D at trim</span><span class="v">${fmt(f.ld_at_trim, 1)}</span>
    <span class="k">CL_α</span><span class="v">${fmt(f.cl_alpha_per_deg, 4)}/deg</span>
    <span class="k">Neutral point x</span><span class="v">${fmt(f.x_np_m, 3)} m</span>` +
    (stale ? '<span class="k" style="color:var(--warn)">STALE</span><span class="v" style="color:var(--warn)">geometry changed since this run</span>' : '');
}

/* --------------------------------------------------------------- actions */
function busy(on, msg) {
  document.getElementById('busy').classList.toggle('hidden', !on);
  if (msg) document.getElementById('busy-msg').textContent = msg;
}

async function applyRevision() {
  const issues = renderIssues();
  if (issues.length) { toast('err', 'Cannot apply: ' + issues[0].message); return; }
  busy(true, 'OpenVSP is rebuilding the authoritative geometry…');
  try {
    const r = await fetch('/api/revision', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: S.sid, params: S.params,
        note: `cockpit revision (stability pref: ${S.stabilityPref})` }) });
    const data = await r.json();
    if (!r.ok) {
      const det = Array.isArray(data.detail) ? data.detail : [{ message: JSON.stringify(data.detail) }];
      for (const d of det) toast('err', `Geometry rejected by validation gates: ${d.message}`);
      return;
    }
    S.applied = { ...data.params };
    S.params = { ...data.params };
    S.savedMission = { ...S.mission };
    S.appliedIsBuilt = true;
    S.geomVersion = data.version;
    await loadStl(data.version);
    await refreshVersions();
    toast('ok', `Geometry v${data.version} built and validated: S=${fmt(data.summary.s_ref_m2, 3)} m², AR=${fmt(data.summary.aspect_ratio, 2)}, watertight=${data.summary.watertight}. Explanation queued for chat.`);
    if (!data.passed) toast('warn', 'Some OpenVSP gates FAILED — see gate list in Validation.');
    document.getElementById('issues-list').innerHTML +=
      data.gates.map(g => `<div class="gate"><span class="${g.passed ? 'ok' : 'bad'}">${g.passed ? '✓' : '✗'}</span> ${g.gate}${g.reason ? ' — ' + g.reason : ''} <span class="badge fast">OpenVSP gate</span></div>`).join('');
    syncAll(true);
  } catch (e) {
    toast('err', `Apply failed: ${e.message}`);
  } finally { busy(false); }
}

async function saveMission() {
  busy(true, 'Recomputing mission physics…');
  try {
    const r = await fetch('/api/mission', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: S.sid, mission: S.mission }) });
    const data = await r.json();
    if (!r.ok) {
      (Array.isArray(data.detail) ? data.detail : [{ message: String(data.detail) }])
        .forEach(d => toast('err', `Mission rejected: ${d.message} (${(d.params || []).join(', ')})`));
      return;
    }
    S.savedMission = { ...S.mission };
    S.sizing = data.sizing_targets;
    S.assumptions = data.assumptions;
    renderSizing();
    await refreshVersions();
    toast('ok', `Mission saved as plan v${data.plan_version}. Geometry parameters were NOT changed — review sizing targets and adjust geometry if needed.`);
    syncAll();
  } finally { busy(false); }
}

async function runFastAero() {
  if (isDirty()) { toast('warn', 'Apply the pending geometry revision first — fast aero runs on the authoritative OpenVSP model, not the preview.'); return; }
  if (!S.appliedIsBuilt) { toast('warn', 'No built geometry yet — press Apply Revision first.'); return; }
  busy(true, 'VSPAERO sweep running (seconds to a minute)…');
  try {
    const r = await fetch('/api/fast_aero', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: S.sid }) });
    const data = await r.json();
    if (!r.ok) {
      (Array.isArray(data.detail) ? data.detail : [{ message: String(data.detail) }])
        .forEach(d => toast('err', `Fast aero rejected: ${d.message}`));
      return;
    }
    S.fast = data; S.fastForVersion = S.geomVersion;
    renderFast();
    await refreshVersions();
    toast('ok', `VSPAERO done: trim α ${fmt(data.alpha_trim_deg, 2)}°, L/D ${fmt(data.ld_at_trim, 1)}. These are fast-validated values.`);
  } catch (e) { toast('err', `Fast aero failed: ${e.message}`);
  } finally { busy(false); }
}

async function refreshVersions() {
  const r = await fetch(`/api/bootstrap?session=${S.sid}`);
  if (!r.ok) return;
  const b = await r.json();
  S.versions = b.versions;
  document.getElementById('session-chip').textContent =
    `session: ${S.sid} · ${b.pending_explanations} change(s) queued for chat`;
  buildVersionsTab();
}

/* ----------------------------------------------------------------- helpers */
function toast(kind, msg) {
  const t = document.createElement('div');
  t.className = `toast ${kind}`;
  t.textContent = msg;
  document.getElementById('toast-stack').appendChild(t);
  setTimeout(() => t.remove(), kind === 'err' ? 9000 : 6000);
}

let syncPending = false;
function syncAll(skipMesh) {
  if (syncPending) return;
  syncPending = true;
  // setTimeout, not requestAnimationFrame: rAF is suspended in hidden tabs,
  // which would freeze the readout panels while the cockpit is backgrounded
  setTimeout(() => {
    syncPending = false;
    document.querySelectorAll('.ctl').forEach(c => c._sync && c._sync());
    if (!skipMesh) refreshPreviewMesh();
    renderDerived();
    renderAeroEst();
    renderConsequences();
    renderIssues();
    renderFast();
    applyDisplayModes();
  }, 16);
}

/* -------------------------------------------------------------------- boot */
async function boot() {
  const q = new URLSearchParams(location.search);
  const r = await fetch('/api/bootstrap' + (q.get('session') ? `?session=${q.get('session')}` : ''));
  if (!r.ok) { document.body.innerHTML = `<pre style="color:#ff6b6b;padding:30px">cockpit backend error: ${await r.text()}</pre>`; return; }
  const b = await r.json();
  S.sid = b.session_id;
  S.schema = b.param_schema.properties;
  S.mission = { ...b.mission };
  S.savedMission = { ...b.mission };
  S.params = { ...b.params };
  S.applied = { ...b.params };
  S.original = { ...b.params };
  S.locks = b.locks || {};
  S.sizing = b.sizing_targets;
  S.assumptions = b.plan_assumptions;
  S.versions = b.versions;
  S.geomVersion = b.geometry.valid ? b.geometry.version : 0;
  S.appliedIsBuilt = !!(b.geometry.valid && b.geometry.report);
  S.fast = b.fast_aero; S.fastForVersion = S.geomVersion;

  document.getElementById('session-chip').textContent =
    `session: ${S.sid} · ${b.pending_explanations} change(s) queued for chat`;

  buildMissionTab();
  buildDesignsTab();
  buildGeometryTab();
  buildVersionsTab();
  renderSizing();
  fetchCandidates();

  // mission inputs initial values
  document.querySelectorAll('[data-mkey]').forEach(inp => {
    const v = S.mission[inp.dataset.mkey];
    if (v != null) inp.value = v;
  });
  const ls = document.getElementById('launch-sel');
  if (ls) ls.value = S.mission.launch || 'hand';

  initThree();
  refreshPreviewMesh();
  if (S.appliedIsBuilt) await loadStl(S.geomVersion);
  syncAll();

  // top bar wiring
  document.querySelectorAll('.vb[data-view]').forEach(btn =>
    btn.addEventListener('click', () => setView(btn.dataset.view)));
  document.getElementById('btn-proj').addEventListener('click', () => {
    const toOrtho = camera === cameraP;
    const pos = camera.position.clone();
    camera = toOrtho ? cameraO : cameraP;
    camera.position.copy(pos);
    controls.object = camera;
    controls.update();
    document.getElementById('btn-proj').textContent = toOrtho ? 'Ortho' : 'Persp';
  });
  document.getElementById('render-mode').addEventListener('change', applyDisplayModes);
  document.getElementById('btn-half').addEventListener('click', (e) => {
    S.halfOn = !S.halfOn;
    e.target.classList.toggle('active', S.halfOn);
    if (S.halfOn) toast('ok', 'Half view is DISPLAY ONLY. The CFD half export is a geometrically exact centre-plane cut done at the mesh stage, with the centre boundary as a symmetry plane.');
    syncAll(true);
  });
  document.getElementById('btn-ghost').addEventListener('click', async (e) => {
    S.ghostOn = !S.ghostOn;
    e.target.classList.toggle('active', S.ghostOn);
    if (S.ghostOn && !ghostMesh) {
      const prev = S.geomVersion > 1 ? S.geomVersion - 1 : 0;
      if (!prev) { toast('warn', 'No previous geometry version to ghost yet.'); S.ghostOn = false; e.target.classList.remove('active'); return; }
      if (!(await loadStl(prev, 'ghost'))) { toast('err', `No STL stored for geometry v${prev}.`); S.ghostOn = false; e.target.classList.remove('active'); return; }
      toast('ok', `Ghost overlay: geometry v${prev}.`);
    }
    applyDisplayModes();
  });
  document.getElementById('btn-apply').addEventListener('click', applyRevision);
  document.getElementById('btn-left').addEventListener('click', () =>
    document.getElementById('main').classList.toggle('left-collapsed'));
  document.getElementById('btn-right').addEventListener('click', () =>
    document.getElementById('main').classList.toggle('right-collapsed'));
  document.getElementById('btn-fastaero').addEventListener('click', runFastAero);
  document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.tabpane').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('tab-' + t.dataset.tab).classList.add('active');
    setBrowseMode(t.dataset.tab === 'designs');
  }));
  document.getElementById('cand-prev').addEventListener('click', () => showCandidate(S.candIdx - 1));
  document.getElementById('cand-next').addEventListener('click', () => showCandidate(S.candIdx + 1));
  window.addEventListener('keydown', (e) => {
    if (!S.browse || /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
    if (e.key === 'ArrowLeft') showCandidate(S.candIdx - 1);
    if (e.key === 'ArrowRight') showCandidate(S.candIdx + 1);
  });
}

/* Debug/verification hook: lets tooling snapshot the WebGL canvas and inspect
 * model state without relying on compositor screenshots. */
window.__astra = {
  state: S,
  bbox() {
    const m = (stlMesh && stlMesh.visible) ? stlMesh : previewMesh;
    if (!m) return null;
    m.geometry.computeBoundingBox();
    const b = m.geometry.boundingBox;
    return { which: m === stlMesh ? 'stl' : 'preview',
      min: b.min.toArray(), max: b.max.toArray(),
      span: b.max.y - b.min.y, chordExtent: b.max.x - b.min.x };
  },
  async save(name, scale = 0.75, quality = 0.7) {
    const r = await fetch('/api/debug/snapshot', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, data_url: this.snapshot(scale, quality) }) });
    return (await r.json()).path;
  },
  snapshot(scale = 0.5, quality = 0.55) {
    renderer.render(scene, camera);
    const src = renderer.domElement;
    const c = document.createElement('canvas');
    c.width = Math.round(src.width * scale); c.height = Math.round(src.height * scale);
    c.getContext('2d').drawImage(src, 0, 0, c.width, c.height);
    return c.toDataURL('image/jpeg', quality);
  },
};

boot().catch(e => {
  document.body.innerHTML = `<pre style="color:#ff6b6b;padding:30px">cockpit failed to start: ${e.stack}</pre>`;
});
