/* Astra MDO cockpit module — simple setup panel + differential-evolution search
 * gallery, over the AeroSandbox fast tier. Self-contained (no imports); shares the
 * session via window.__astra.state and talks to /api/mdo/*. Winners flow into the
 * existing candidate navigator.
 *
 * Design intent: show only the options that change the answer. Three plain-language
 * priorities stand in for nine penalty weights; everything else is defaulted and
 * tucked under "Advanced".
 */
(function () {
  "use strict";
  var D2R = Math.PI / 180;
  var state = { eff: 0.5, sta: 0.5, bld: 0.5, spread: 0.15, freeze: [],
                preset: "balanced", presets: [], running: false, sid: null };
  var scoreTimer = null;

  function astra() { return (window.__astra && window.__astra.state) || {}; }
  function sid() { return astra().sid || state.sid; }

  async function api(path, body) {
    var r = await fetch(path, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error((await r.text()) || r.status);
    return r.json();
  }

  /* ---- parametric wing render (canvas, pseudo-3D; no OpenVSP) ---- */
  function drawWing(cv, p, winner) {
    var dpr = window.devicePixelRatio || 1, W = cv.clientWidth || 150, H = cv.clientHeight || 92;
    cv.width = W * dpr; cv.height = H * dpr;
    var ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
    var half = p.span_m / 2, b1 = p.centre_body_fraction * half, b2 = half - b1;
    var cR = p.root_chord_m, cM = cR * p.mid_chord_frac, cT = cM * p.taper_outer;
    var tI = Math.tan(p.sweep_inner_deg * D2R), tO = Math.tan(p.sweep_outer_deg * D2R),
        tD = Math.tan(p.dihedral_deg * D2R);
    var midTw = p.root_twist_deg + (p.tip_twist_deg - p.root_twist_deg) * p.centre_body_fraction;
    var St = [
      { y: 0, xle: 0, z: 0, c: cR, tw: p.root_twist_deg },
      { y: b1, xle: b1 * tI, z: b1 * tD, c: cM, tw: midTw },
      { y: half, xle: b1 * tI + b2 * tO, z: b1 * tD + b2 * tD, c: cT, tw: p.tip_twist_deg }
    ];
    function le(s) { return { x: s.xle, y: s.y, z: s.z }; }
    function te(s) { return { x: s.xle + s.c * Math.cos(s.tw * D2R), y: s.y, z: s.z - s.c * Math.sin(s.tw * D2R) }; }
    function proj(q) { return { x: q.y * 1.0 + q.x * 0.42, y: q.x * 0.52 - q.z * 1.05 }; }
    var surf = [], pts = [];
    [1, -1].forEach(function (sg) {
      var LE = St.map(function (s) { return proj({ x: le(s).x, y: le(s).y * sg, z: le(s).z }); });
      var TE = St.map(function (s) { return proj({ x: te(s).x, y: te(s).y * sg, z: te(s).z }); });
      surf.push({ LE: LE, TE: TE }); pts = pts.concat(LE, TE);
    });
    var xs = pts.map(function (q) { return q.x; }), ys = pts.map(function (q) { return q.y; });
    var mnX = Math.min.apply(0, xs), mxX = Math.max.apply(0, xs),
        mnY = Math.min.apply(0, ys), mxY = Math.max.apply(0, ys);
    var m = 9, sc = Math.min((W - 2 * m) / ((mxX - mnX) || 1), (H - 2 * m) / ((mxY - mnY) || 1));
    var ox = (W - (mxX + mnX) * sc) / 2, oy = (H - (mxY + mnY) * sc) / 2;
    function T(q) { return [q.x * sc + ox, q.y * sc + oy]; }
    surf.forEach(function (hf) {
      ctx.beginPath();
      hf.LE.concat(hf.TE.slice().reverse()).forEach(function (q, i) {
        var c = T(q); i ? ctx.lineTo(c[0], c[1]) : ctx.moveTo(c[0], c[1]); });
      ctx.closePath();
      var g = ctx.createLinearGradient(0, 0, 0, H);
      g.addColorStop(0, winner ? "#ffffff" : "#e6eef7");
      g.addColorStop(1, winner ? "#c3d2e2" : "#9db0c5");
      ctx.fillStyle = g; ctx.fill();
      ctx.strokeStyle = "rgba(38,58,86,.35)"; ctx.lineWidth = 0.6;
      hf.LE.forEach(function (q, i) { var a = T(q), b = T(hf.TE[i]);
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); });
      ctx.beginPath();
      hf.LE.forEach(function (q, i) { var c = T(q); i ? ctx.lineTo(c[0], c[1]) : ctx.moveTo(c[0], c[1]); });
      ctx.strokeStyle = winner ? "#f5d21e" : "#7fd3ff"; ctx.lineWidth = 1.7; ctx.stroke();
    });
  }

  /* ---- setup panel ---- */
  var el = {};
  function buildPanel(root) {
    var p = document.createElement("div"); p.id = "mdo-panel"; p.innerHTML =
      '<h3>Optimize this wing</h3>' +
      '<p class="hint">AI searches thousands of shapes and keeps the best. Pick what matters — the rest is set for you.</p>' +
      '<div class="grp"><div class="lbl">Quick pick</div><div class="mdo-chips" id="mdo-chips"></div></div>' +
      '<div class="grp"><div class="lbl">What matters most</div>' +
        sliderRow("eff", "Efficiency") + sliderRow("sta", "Stability") + sliderRow("bld", "Buildable") +
      '</div>' +
      '<div class="grp"><div class="lbl">Search</div><div class="mdo-explore">' +
        '<span>stay close</span><input id="mdo-spread" type="range" min="5" max="45" value="15">' +
        '<span>explore</span></div></div>' +
      '<details id="mdo-adv"><summary>Advanced</summary>' +
        '<div class="freeze" id="mdo-freeze"></div>' +
        '<div class="row"><label>seed <input id="mdo-seed" type="number" value="0"></label>' +
        '<label>pop <input id="mdo-pop" type="number" value="12"></label>' +
        '<label>gens <input id="mdo-gen" type="number" value="24"></label></div></details>' +
      '<div class="mdo-score"><div><div class="cap">this wing scores</div>' +
        '<span class="n" id="mdo-scoreN">—</span></div>' +
        '<div class="meta" id="mdo-scoreMeta"></div></div>' +
      '<button class="mdo-run" id="mdo-run">Search designs →</button>';
    root.appendChild(p);
    el.panel = p;
    el.chips = p.querySelector("#mdo-chips");
    ["eff", "sta", "bld"].forEach(function (k) {
      var s = p.querySelector("#mdo-" + k);
      s.addEventListener("input", function () { state[k] = s.value / 100; markCustom(); queueScore(); });
    });
    p.querySelector("#mdo-spread").addEventListener("input", function (e) { state.spread = e.target.value / 100; });
    var freeze = p.querySelector("#mdo-freeze");
    ["span_m", "root_chord_m", "sweep_inner_deg", "sweep_outer_deg", "dihedral_deg"].forEach(function (f) {
      var lab = document.createElement("label");
      lab.innerHTML = '<input type="checkbox" value="' + f + '">' + f.replace(/_m$|_deg$/, "").replace(/_/g, " ");
      lab.querySelector("input").addEventListener("change", collectFreeze);
      freeze.appendChild(lab);
    });
    p.querySelector("#mdo-run").addEventListener("click", runSearch);
  }
  function sliderRow(k, label) {
    return '<div class="mdo-slider"><span>' + label + '</span>' +
      '<input id="mdo-' + k + '" type="range" min="0" max="100" value="50"></div>';
  }
  function collectFreeze() {
    state.freeze = Array.prototype.slice.call(
      el.panel.querySelectorAll("#mdo-freeze input:checked")).map(function (c) { return c.value; });
  }
  function markCustom() {
    state.preset = null;
    Array.prototype.forEach.call(el.chips.children, function (c) { c.classList.remove("active"); });
  }
  function applyPreset(pr) {
    state.eff = pr.priorities.efficiency; state.sta = pr.priorities.stability;
    state.bld = pr.priorities.buildable; state.preset = pr.key;
    el.panel.querySelector("#mdo-eff").value = state.eff * 100;
    el.panel.querySelector("#mdo-sta").value = state.sta * 100;
    el.panel.querySelector("#mdo-bld").value = state.bld * 100;
    Array.prototype.forEach.call(el.chips.children, function (c) {
      c.classList.toggle("active", c.dataset.key === pr.key); });
    queueScore();
  }

  async function loadProposal() {
    try {
      var prop = await api("/api/mdo/propose", { session_id: sid() });
      state.presets = prop.presets; state.spread = prop.spread;
      el.chips.innerHTML = "";
      prop.presets.forEach(function (pr) {
        var c = document.createElement("div"); c.className = "mdo-chip"; c.textContent = pr.label;
        c.dataset.key = pr.key; c.title = pr.blurb;
        c.addEventListener("click", function () { applyPreset(pr); });
        el.chips.appendChild(c);
      });
      var def = prop.presets.filter(function (p) { return p.key === prop.default_preset; })[0];
      if (def) applyPreset(def);
    } catch (e) { /* panel still usable with defaults */ queueScore(); }
  }

  function queueScore() {
    clearTimeout(scoreTimer);
    scoreTimer = setTimeout(updateScore, 260);
  }
  async function updateScore() {
    var n = el.panel.querySelector("#mdo-scoreN"), meta = el.panel.querySelector("#mdo-scoreMeta");
    n.textContent = "…"; n.classList.remove("bad");
    try {
      var r = await api("/api/mdo/score", { session_id: sid(),
        efficiency: state.eff, stability: state.sta, buildable: state.bld });
      if (r.score == null || !r.feasible) {
        n.textContent = r.score == null ? "—" : r.score.toFixed(2);
        n.classList.add("bad"); meta.textContent = r.reject_reason || "infeasible"; return;
      }
      n.textContent = r.score.toFixed(2);
      meta.innerHTML = "L/D " + r.l_over_d.toFixed(1) + " · SM " + r.static_margin.toFixed(2);
    } catch (e) { n.textContent = "—"; meta.textContent = "score unavailable"; }
  }

  /* ---- gallery + streaming search ---- */
  var gal = {};
  function buildGallery(root) {
    var g = document.createElement("div"); g.id = "mdo-gallery"; g.innerHTML =
      '<div class="g-hdr"><div class="g-step">STEP 3 — SEARCH FOR THE WINNER <b>(DIFFERENTIAL EVOLUTION)</b></div>' +
      '<div class="g-stat" id="mdo-gstat">starting…</div>' +
      '<button class="g-close" id="mdo-gclose">Close</button></div>' +
      '<div id="mdo-grid"></div>' +
      '<div class="g-foot"><span id="mdo-gfoot">Evaluating candidates…</span>' +
      '<button class="go" id="mdo-ggo" style="display:none">View in candidates</button></div>';
    root.appendChild(g); gal.root = g;
    gal.grid = g.querySelector("#mdo-grid"); gal.stat = g.querySelector("#mdo-gstat");
    gal.foot = g.querySelector("#mdo-gfoot"); gal.go = g.querySelector("#mdo-ggo");
    g.querySelector("#mdo-gclose").addEventListener("click", function () { g.classList.remove("open"); });
    gal.go.addEventListener("click", function () {
      g.classList.remove("open");
      var b = document.getElementById("btn-suggest"); if (b) b.click();  // open candidate navigator
    });
  }
  function renderTiles(tiles) {
    var best = tiles.length ? Math.max.apply(0, tiles.map(function (t) { return t.score; })) : null;
    gal.grid.innerHTML = "";
    tiles.forEach(function (t, i) {
      var win = t.score === best;
      var d = document.createElement("div"); d.className = "g-tile" + (win ? " winner" : "");
      d.innerHTML = '<div class="rk">#' + (t.rank || i + 1) + '</div><canvas></canvas>' +
        '<div class="sc">' + t.score.toFixed(2) + '</div><div class="ld">L/D ' + t.l_over_d.toFixed(1) + '</div>';
      gal.grid.appendChild(d);
      // draw synchronously (accessing clientWidth forces layout) — rAF is throttled
      // in hidden tabs, which would leave the canvases blank.
      drawWing(d.querySelector("canvas"), t.params, win);
    });
  }

  async function streamOptimize(body, onEvent) {
    var res = await fetch("/api/mdo/optimize", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok || !res.body) throw new Error((res.status) + " " + (await res.text().catch(function () { return ""; })));
    var reader = res.body.getReader(), dec = new TextDecoder(), buf = "";
    while (true) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buf += dec.decode(chunk.value, { stream: true });
      var idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        var block = buf.slice(0, idx); buf = buf.slice(idx + 2);
        var line = block.split("\n").filter(function (l) { return l.indexOf("data:") === 0; })[0];
        if (line) { try { onEvent(JSON.parse(line.slice(5).trim())); } catch (e) {} }
      }
    }
  }

  async function runSearch() {
    if (state.running) return;
    collectFreeze();
    state.running = true;
    var runBtn = el.panel.querySelector("#mdo-run");
    runBtn.disabled = true; runBtn.textContent = "Searching…";
    gal.grid.innerHTML = ""; gal.foot.textContent = "Evaluating candidates…";
    gal.go.style.display = "none"; gal.stat.textContent = "starting…";
    gal.root.classList.add("open");
    var body = { session_id: sid(), efficiency: state.eff, stability: state.sta,
      buildable: state.bld, spread: state.spread, freeze: state.freeze,
      seed: +el.panel.querySelector("#mdo-seed").value || 0,
      popsize: +el.panel.querySelector("#mdo-pop").value || 12,
      maxiter: +el.panel.querySelector("#mdo-gen").value || 24 };
    try {
      await streamOptimize(body, function (ev) {
        if (ev.type === "generation") {
          gal.stat.textContent = "generation " + String(ev.gen).padStart(2, "0") +
            " · " + ev.n_evals + " evals · best " + (ev.best || 0).toFixed(2);
          if (ev.tiles && ev.tiles.length) renderTiles(ev.tiles);
        } else if (ev.type === "result") {
          if (ev.top && ev.top.length) renderTiles(ev.top);
          gal.stat.textContent = "converged · " + ev.n_evals + " evals · " +
            ev.n_feasible + " feasible";
          var base = ev.reference ? ev.reference.base_score : null;
          var win = ev.winner ? ev.winner.score : null;
          gal.foot.innerHTML = '<span class="done">✓ ' + (ev.candidate_keys || []).length +
            ' winners added to candidates</span>' +
            (base != null && win != null ? " · score " + base.toFixed(1) + " → " + win.toFixed(1) : "");
          gal.go.style.display = "inline-block";
        } else if (ev.type === "error") {
          gal.foot.textContent = "Search failed: " + ev.message;
        }
      });
    } catch (e) {
      gal.foot.textContent = "Search failed: " + e.message;
    } finally {
      state.running = false; runBtn.disabled = false; runBtn.textContent = "Search designs →";
    }
  }

  /* ---- boot ---- */
  function init() {
    var root = document.getElementById("mdo-root");
    if (!root) { root = document.createElement("div"); root.id = "mdo-root"; document.body.appendChild(root); }
    buildPanel(root); buildGallery(root);
    var btn = document.getElementById("btn-mdo");
    if (btn) btn.addEventListener("click", function () {
      var open = el.panel.classList.toggle("open");
      if (open && !state.presets.length) loadProposal();
      else if (open) queueScore();
    });
    // expose for the standalone preview harness / tests
    window.__mdo = { drawWing: drawWing, renderTiles: renderTiles, applyPreset: applyPreset,
      openPanel: function () { el.panel.classList.add("open"); loadProposal(); },
      openGallery: function () { gal.root.classList.add("open"); }, state: state };
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
