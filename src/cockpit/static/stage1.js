/* Stage 1 — Plan: requirements intake + concept gallery.
 *
 * Deliberately self-contained (no imports from cockpit.js): the classic
 * cockpit stays untouched while the staged pipeline grows. The parametric
 * preview functions (planform/stations/sectionLoop/buildPreviewGeometry)
 * mirror cockpit.js — if the BWBParams schema changes, update both.
 */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const S = {
  sid: null,
  intake: null,
  intakeVersion: 0,
  planVersion: null,
  candidates: [],
  candIdx: 0,
  galleryIntakeVersion: null,   // which intake version the designs came from
  openInfo: new Set(),
  warnings: [],                 // structured disclosures from the last response (S3-D2)
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function toast(msg, ms = 2600) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add('hidden'), ms);
}

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = `${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch { /* keep status */ }
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return res.json();
}

/* ======================= requirements screen ======================= */

const TIER_EL = { mission: 'tier-mission', vehicle: 'tier-vehicle', derived: 'tier-derived' };

function badgeFor(row) {
  if (row.origin === 'you_said') return '<span class="badge you">you said</span>';
  if (row.origin === 'edited') return '<span class="badge you">you set</span>';
  if (row.origin === 'derived')
    return `<span class="badge derived">derived · ${esc(row.source)}</span>`;
  return `<span class="badge assumed">assumed · ${esc(row.source)}</span>`;
}

/* S3-D2: a row whose typed value is not the value in force must say so, next to the
   value itself. Ten probed inputs diverged silently — span 100 m was used as 3.0 m
   with the verdict still reading "feasible". */
function substitutionFor(row) {
  if (!row.used_value) return '';
  return `<span class="badge warn" title="the solver used a different value">
            using ${esc(row.used_value)}</span>`;
}

/* S3-E2/E3: six of the twelve editable rows and every custom row change no number at
   all. They were presented identically to the six that do. */
function recordedOnlyFor(row) {
  if (row.status === 'derived' || row.drives_computation) return '';
  return '<span class="badge muted" title="recorded on this list; not evaluated at '
    + 'this tier">recorded only</span>';
}

function renderIntake() {
  for (const el of Object.values(TIER_EL)) $(el).innerHTML = '';
  for (const row of S.intake.rows) {
    const holder = $(TIER_EL[row.tier]);
    if (!holder) continue;
    const derived = row.status === 'derived';
    const div = document.createElement('div');
    div.className = 'req-row';
    div.dataset.rowId = row.id;
    div.innerHTML = `
      <div class="req-main">
        <button class="icon-btn ${derived ? 'derived-hidden' : ''} ${row.status === 'fixed' ? 'locked' : ''}"
                data-act="lock" title="${row.status === 'fixed' ? 'Lock (display only) — tap to free' : 'Lock (display only) — tap to fix'}">
          ${row.status === 'fixed' ? '🔒' : '🔓'}</button>
        <button class="icon-btn" data-act="info" title="What is this?">ⓘ</button>
        <span class="label">${esc(row.label)}</span>
        ${derived
          ? `<span class="req-value readonly">${esc(row.value)}</span>`
          : `<input class="req-value" value="${esc(row.value)}">`}
        ${badgeFor(row)}
        ${substitutionFor(row)}
        ${recordedOnlyFor(row)}
      </div>`;
    const info = document.createElement('div');
    info.className = 'req-info' + (S.openInfo.has(row.id) ? '' : ' hidden');
    let html = esc(row.info || 'Custom requirement — recorded on this list only. It is '
      + 'not evaluated at this tier and does not constrain the solver.');
    if (!derived && !row.drives_computation) {
      html += '<br><b>Recorded only.</b> Editing this row does not change any number '
        + 'on this screen — it is kept on the record for a later stage.';
    }
    if (row.status !== 'derived') {
      html += '<br><b>Lock is display only.</b> It records your intent; the solver '
        + 'produces identical values whether this row is locked or free.';
    }
    if (row.used_value) {
      html += `<br><b>In force:</b> ${esc(row.used_value)} — not the value typed above.`;
    }
    if (row.drivers && row.drivers.length) {
      html += '<br><b>To change it, change these:</b><div class="driver-chips">'
        + row.drivers.map((d) =>
          `<button class="driver-chip" data-target="${esc(d.row_id)}">↗ ${esc(d.effect)}</button>`).join('')
        + '</div>';
    }
    info.innerHTML = html;
    div.appendChild(info);
    holder.appendChild(div);

    div.querySelector('[data-act=info]').addEventListener('click', () => {
      info.classList.toggle('hidden');
      if (info.classList.contains('hidden')) S.openInfo.delete(row.id);
      else S.openInfo.add(row.id);
    });
    if (!derived) {
      div.querySelector('[data-act=lock]').addEventListener('click', () =>
        saveEdits([{ id: row.id, status: row.status === 'fixed' ? 'variable' : 'fixed' }]));
      div.querySelector('input.req-value').addEventListener('change', (e) =>
        saveEdits([{ id: row.id, value: e.target.value }]));
    }
    for (const chip of info.querySelectorAll('.driver-chip')) {
      chip.addEventListener('click', () => flashRow(chip.dataset.target));
    }
  }
  $('intake-version').textContent = `v${S.intakeVersion}`;
  renderFeasibility();
}

function flashRow(rowId) {
  const el = document.querySelector(`.req-row[data-row-id="${rowId}"]`);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 1200);
}

function renderFeasibility() {
  const f = S.intake.feasibility;
  const card = $('feas-card');
  card.classList.remove('ok', 'warn');
  if (!f) { $('feas-headline').textContent = '—'; return; }
  /* "feasible" is reserved for a solve that used every number the user typed.
     A substitution downgrades the card, because a green tick over a replaced
     requirement is the exact dishonesty S3-D2 exists to remove. */
  const clean = f.verdict === 'feasible';
  card.classList.add(clean ? 'ok' : 'warn');
  $('feas-icon').textContent = clean ? '✔' : '▲';
  $('feas-headline').textContent = f.headline
    + (f.substitutions ? ` · ${f.substitutions} value`
       + `${f.substitutions === 1 ? '' : 's'} replaced` : '');
  $('feas-detail').textContent = f.detail || '';
  $('feas-implied').textContent = Object.entries(f.implied || {})
    .map(([k, v]) => `${k} ${v}`).join(' · ');
  renderWarnings();
  $('btn-generate').disabled = f.verdict === 'error';
  if (f.binding_row_ids && f.binding_row_ids.length) {
    $('feas-detail').textContent += ' (tap a derived ⓘ for the levers)';
  }
}

/* Warnings arrive on every intake response (S3-D2). They are structured, so the list
   can show what was typed, what is in force, and which bound forced it. */
function renderWarnings() {
  const el = $('feas-warnings');
  if (!el) return;
  const items = S.warnings || [];
  el.classList.toggle('hidden', items.length === 0);
  el.innerHTML = items.map((w) => {
    const detail = w.typed && w.used
      ? `<span class="mono dim"> (you typed ${esc(w.typed)} · using ${esc(w.used)}`
        + `${w.limit ? ` · limit ${esc(w.limit)}` : ''})</span>`
      : '';
    return `<li data-kind="${esc(w.kind)}">${esc(w.message)}${detail}</li>`;
  }).join('');
}

async function saveEdits(edits) {
  try {
    const out = await api('/api/stage1/intake', { session_id: S.sid, edits });
    S.intake = out.intake;
    S.intakeVersion = out.version;
    S.warnings = out.warnings || [];
    renderIntake();
    updateStaleChip();
  } catch (e) { toast(`save failed: ${e.message}`); }
}

function updateStaleChip() {
  const stale = S.candidates.length > 0
    && S.galleryIntakeVersion !== null
    && S.intakeVersion !== S.galleryIntakeVersion;
  const chip = $('stale-chip');
  chip.classList.toggle('hidden', !stale);
  if (stale) {
    $('stale-text').textContent =
      `designs from intake v${S.galleryIntakeVersion} — list is now v${S.intakeVersion}`;
  }
  $('btn-gallery').classList.toggle('hidden', S.candidates.length === 0);
  $('btn-gallery').textContent = stale ? 'View designs (stale)' : 'View designs';
}

let addCount = 0;
function addRow() {
  addCount += 1;
  const holder = $('tier-vehicle');
  const div = document.createElement('div');
  div.className = 'req-row';
  div.innerHTML = `
    <div class="req-main">
      <span class="icon-btn locked">🔒</span>
      <input class="req-value" style="flex:1; text-align:left" placeholder="Name — e.g. Max chord" data-k="label">
      <input class="req-value" placeholder="value — e.g. ≤ 0.35 m" data-k="value">
      <span class="badge you">you set</span>
    </div>`;
  holder.appendChild(div);
  const commit = () => {
    const label = div.querySelector('[data-k=label]').value.trim();
    const value = div.querySelector('[data-k=value]').value.trim();
    if (!label || !value) return;
    const id = 'custom_' + label.toLowerCase().replace(/[^a-z0-9]+/g, '_') + '_' + addCount;
    div.remove();
    saveEdits([{ id, label, value, status: 'fixed' }]);
  };
  for (const inp of div.querySelectorAll('input')) {
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') commit(); });
  }
  div.querySelector('[data-k=value]').addEventListener('blur', commit);
  div.querySelector('[data-k=label]').focus();
}

/* ======================= gallery screen ======================= */

let renderer, scene, camera, cameraP, cameraO, controls, modelGroup, mesh;
let needsRender = true;
const requestRender = () => { needsRender = true; };

function initThree() {
  const canvas = $('canvas3d');
  renderer = new THREE.WebGLRenderer({ canvas, antialias: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
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
  modelGroup = new THREE.Group();
  modelGroup.rotation.x = -Math.PI / 2;   // VSP x-aft/y-span/z-up -> three y-up
  scene.add(modelGroup);
  setView('iso');
  const resize = () => {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    cameraP.aspect = w / h; cameraP.updateProjectionMatrix();
    const a = w / h;
    cameraO.left = -oScale * a; cameraO.right = oScale * a;
    cameraO.updateProjectionMatrix();
  };
  new ResizeObserver(() => { resize(); requestRender(); }).observe(canvas);
  resize();
  controls.addEventListener('change', requestRender);
  (function loop() {
    requestAnimationFrame(loop);
    const moved = controls.update();
    if (moved || needsRender) { renderer.render(scene, camera); needsRender = false; }
  })();
}

const VIEWS = {
  iso: [1.6, 1.2, 1.6], top: [0, 2.4, 0.0001], front: [0, 0, 2.4], side: [2.4, 0, 0],
};
function setView(name) {
  const [x, y, z] = VIEWS[name] || VIEWS.iso;
  camera.position.set(x, y, z);
  controls.target.set(0, 0, 0);
  controls.update();
  requestRender();
  document.querySelectorAll('.vb[data-view]').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === name));
}

/* --- parametric preview (mirrors cockpit.js) --- */
const NX = 14;
function planform(p) {
  const cMid = p.root_chord_m * p.mid_chord_frac;
  const cTip = cMid * p.taper_outer;
  const b1 = p.centre_body_fraction * p.span_m / 2.0;
  const b2 = p.span_m / 2.0 - b1;
  return { cMid, cTip, b1, b2 };
}
function sectionLoop(chord, tc, twistDeg, xle, y, z) {
  const pts = [];
  const t = tc, th = twistDeg * Math.PI / 180;
  const yt = (x) => 5 * t * (0.2969 * Math.sqrt(x) - 0.1260 * x - 0.3516 * x * x
    + 0.2843 * x ** 3 - 0.1036 * x ** 4);
  const xs = [];
  for (let i = 0; i < NX; i++) xs.push(0.5 * (1 - Math.cos(Math.PI * i / (NX - 1))));
  for (let i = NX - 1; i >= 0; i--) pts.push([xs[i], yt(xs[i])]);
  for (let i = 1; i < NX; i++) pts.push([xs[i], -yt(xs[i])]);
  const out = [];
  for (const [xc, zc] of pts) {
    const dx = (xc - 0.25) * chord, dz = zc * chord;
    const xr = 0.25 * chord + dx * Math.cos(th) + dz * Math.sin(th);
    const zr = -dx * Math.sin(th) + dz * Math.cos(th);
    out.push(xle + xr, y, z + zr);
  }
  return out;
}
function stations(p) {
  const pf = planform(p);
  const tanI = Math.tan(p.sweep_inner_deg * Math.PI / 180);
  const tanO = Math.tan(p.sweep_outer_deg * Math.PI / 180);
  const tanD = Math.tan(p.dihedral_deg * Math.PI / 180);
  const midThick = (+p.root_thickness_pct + +p.tip_thickness_pct) / 2;
  const midTwist = +p.root_twist_deg
    + (+p.tip_twist_deg - +p.root_twist_deg) * p.centre_body_fraction;
  const st = [];
  const N1 = 6, N2 = 12;
  for (let i = 0; i <= N1; i++) {
    const f = i / N1, y = pf.b1 * f;
    st.push({ y, chord: p.root_chord_m + (pf.cMid - p.root_chord_m) * f,
      xle: y * tanI, z: y * tanD,
      tc: (+p.root_thickness_pct + (midThick - p.root_thickness_pct) * f) / 100,
      tw: +p.root_twist_deg + (midTwist - p.root_twist_deg) * f });
  }
  for (let i = 1; i <= N2; i++) {
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
  const full = [...half.slice(1).reverse().map((s) => ({ ...s, y: -s.y })), ...half];
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
  for (const [s, flip] of [[0, false], [full.length - 1, true]]) {
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

function showCandidate(i) {
  if (!S.candidates.length) return;
  S.candIdx = ((i % S.candidates.length) + S.candidates.length) % S.candidates.length;
  const c = S.candidates[S.candIdx];
  const g = buildPreviewGeometry(c.params);
  if (!mesh) {
    mesh = new THREE.Mesh(g, new THREE.MeshStandardMaterial({
      color: 0x35b587, metalness: 0.15, roughness: 0.6, side: THREE.DoubleSide }));
    modelGroup.add(mesh);
  } else {
    mesh.geometry.dispose();
    mesh.geometry = g;
  }
  requestRender();

  $('design-name').textContent = c.title;
  $('design-sub').textContent =
    `from plan v${S.planVersion ?? '—'} · ${c.is_baseline ? 'baseline · ' : ''}equation evaluated`;
  const p = c.panel;
  const rows = [
    ['Span', `${p.span_m.toFixed(2)} m`],
    ['Wing area', `${p.s_ref_m2.toFixed(3)} m²`],
    ['Aspect ratio', p.aspect_ratio.toFixed(2)],
    ['Wing loading', `${p.wing_loading_kgm2.toFixed(1)} kg/m²`],
    ['Cruise CL', p.cl_cruise.toFixed(3)],
    ['L/D (equation)', p.ld_cruise.toFixed(1)],
    ['Stall speed', `${p.v_stall_ms.toFixed(1)} m/s`],
  ];
  $('design-metrics').innerHTML = rows.map(([k, v]) =>
    `<tr><td>${k}</td><td>${v}</td></tr>`).join('');
  /* S4-C4: a concept that breaks one of the user's own hard caps says so on the card
     and cannot be promoted without an explicit override. It used to report
     `passed: true` and offer the same button as any other. */
  const violations = c.violations || [];
  const notes = [c.objective || '', ...(c.constraint_notes || [])].filter(Boolean);
  $('design-notes').innerHTML =
    (violations.length
      ? `<div class="violation">✖ Does not meet your requirements<ul>`
        + violations.map((v) => `<li>${esc(v.message)}</li>`).join('')
        + `</ul></div>`
      : '')
    + notes.map((n) => `<p>${esc(n)}</p>`).join('');
  $('btn-stage2').classList.toggle('blocked', violations.length > 0);
  $('counter').textContent = `${S.candIdx + 1} / ${S.candidates.length}`;
  const dots = $('dots');
  dots.innerHTML = S.candidates.map((_, k) =>
    `<span class="${k === S.candIdx ? 'on' : ''}"></span>`).join('');
}

function busy(on, msg = 'working…') {
  $('busy-msg').textContent = msg;
  $('busy').classList.toggle('hidden', !on);
}

function showScreen(name) {
  $('screen-req').classList.toggle('hidden', name !== 'req');
  $('screen-gallery').classList.toggle('hidden', name !== 'gallery');
  if (name === 'gallery') requestRender();
}

async function generateConcepts(regenerate) {
  busy(true, 'sizing concepts from your plan…');
  showScreen('gallery');
  try {
    const out = await api('/api/stage1/generate', { session_id: S.sid });
    S.planVersion = out.plan_version;
    const live = out.candidates.filter((c) => c.status !== 'discarded');
    /* S6-G5: a duplicate is the same wing as an earlier card — same span and area to
       within 0.1 mm / 1%. Offering it as a separate choice offers a choice that does
       not exist, so it is dropped and the collapse is reported instead. */
    S.candidates = live.filter((c) => !c.duplicate_of);
    const collapsed = live.length - S.candidates.length;
    S.galleryIntakeVersion = out.intake_version;
    showCandidate(0);
    updateStaleChip();
    toast(collapsed
      ? `${S.candidates.length} distinct concepts (${collapsed} were the same wing — `
        + 'the caps are doing the designing)'
      : `${S.candidates.length} concepts from intake v${out.intake_version}`,
    collapsed ? 5200 : undefined);
  } catch (e) {
    toast(`generate failed: ${e.message}`, 4200);
    showScreen('req');
  } finally { busy(false); }
}

async function takeToStage2() {
  if (!S.candidates.length) return;
  const c = S.candidates[S.candIdx];
  const violations = c.violations || [];
  let note = 'stage-1 gallery selection';
  if (violations.length) {
    /* An override is a decision, so it is taken explicitly and recorded with a
       reason on the geometry version (S4-C4). */
    const reason = window.prompt(
      'This concept does not meet your requirements:\n\n'
      + violations.map((v) => `• ${v.message}`).join('\n')
      + '\n\nPromote it anyway? Type why (this is recorded), or cancel.');
    if (!reason || !reason.trim()) { toast('promotion cancelled'); return; }
    note = `override: ${reason.trim()}`;
  }
  busy(true, `promoting “${c.title}” to the main line…`);
  try {
    await api('/api/candidates/promote',
      { session_id: S.sid, key: c.key, note,
        override_violations: violations.length > 0 });
    toast('Promoted. Opening Stage 2 (classic cockpit MDO)…', 3200);
    setTimeout(() => { window.location.href = `/?session=${S.sid}`; }, 1200);
  } catch (e) {
    toast(`promote failed: ${e.message}`, 4200);
  } finally { busy(false); }
}

/* ======================= boot + wiring ======================= */

async function boot() {
  initThree();
  try {
    const out = await api('/api/stage1/intake');
    S.sid = out.session_id;
    S.intake = out.intake;
    S.intakeVersion = out.version;
    S.warnings = out.warnings || [];
    $('session-chip').textContent = `session: ${S.sid}`;
    renderIntake();
  } catch (e) { toast(`load failed: ${e.message}`, 5000); }

  $('btn-parse').addEventListener('click', async () => {
    const brief = $('brief').value.trim();
    if (!brief) { toast('type a brief first'); return; }
    $('parse-note').textContent = 'parsing…';
    try {
      const out = await api('/api/stage1/parse', { session_id: S.sid, brief });
      S.intake = out.intake;
      S.intakeVersion = out.version;
      S.warnings = out.warnings || [];
      const said = S.intake.rows.filter((r) => r.origin === 'you_said').length;
      $('parse-note').textContent =
        `${said} requirement${said === 1 ? '' : 's'} from your words — the rest are cited defaults`;
      renderIntake();
      updateStaleChip();
    } catch (e) { toast(`parse failed: ${e.message}`); $('parse-note').textContent = ''; }
  });
  $('btn-add').addEventListener('click', addRow);
  $('btn-generate').addEventListener('click', () => generateConcepts(true));
  $('btn-gallery').addEventListener('click', () => showScreen('gallery'));
  $('btn-back-req').addEventListener('click', () => showScreen('req'));
  $('btn-regen').addEventListener('click', () => generateConcepts(true));
  $('btn-stage2').addEventListener('click', takeToStage2);
  $('cand-prev').addEventListener('click', () => showCandidate(S.candIdx - 1));
  $('cand-next').addEventListener('click', () => showCandidate(S.candIdx + 1));
  $('stale-chip').addEventListener('click', () => generateConcepts(true));
  document.addEventListener('keydown', (e) => {
    if ($('screen-gallery').classList.contains('hidden')) return;
    if (e.key === 'ArrowLeft') showCandidate(S.candIdx - 1);
    if (e.key === 'ArrowRight') showCandidate(S.candIdx + 1);
  });
  $('design-title').addEventListener('click', () =>
    $('design-details').classList.toggle('hidden'));
  document.querySelectorAll('.vb[data-view]').forEach((b) =>
    b.addEventListener('click', () => setView(b.dataset.view)));
  $('btn-proj').addEventListener('click', () => {
    const persp = camera === cameraP;
    const pos = camera.position.clone();
    camera = persp ? cameraO : cameraP;
    camera.position.copy(pos);
    controls.object = camera;
    controls.update();
    $('btn-proj').classList.toggle('active', !persp);
    requestRender();
  });
  document.querySelectorAll('.stage-pill').forEach((b) =>
    b.addEventListener('click', () => {
      if (b.dataset.stage === '1') return;
      toast(`Stage ${b.dataset.stage} lives in the classic cockpit for now — opening it…`);
      setTimeout(() => { window.location.href = `/?session=${S.sid || ''}`; }, 900);
    }));
}

boot();
