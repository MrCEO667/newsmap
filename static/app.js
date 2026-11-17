/* NewsMap · signal observatory front-end */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const safeUrl = u => /^https?:\/\//i.test(u || '') ? u : '';
const nowS = () => Date.now() / 1000;
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
};

/* ---------- optional effects libraries (same three.js revision the graph bundles) ---------- */
const params = new URLSearchParams(location.search);
const LITE = params.has('lite');           // ?lite → plain spheres, no effects (for very weak / busy GPUs)
let THREE = null, BloomPass = null;
if (!LITE) try {
  THREE = await import('https://esm.sh/three@0.162.0');
  BloomPass = (await import('https://esm.sh/three@0.162.0/examples/jsm/postprocessing/UnrealBloomPass.js')).UnrealBloomPass;
} catch (e) { console.warn('[NewsMap] effects unavailable, using plain spheres', e); }

/* ---------- state ---------- */
let CATS = {}, CAT_ORDER = [], anchors = {};
const nodesById = new Map();
let adj = new Map();
let data = { nodes: [], links: [] };
let hiddenCats = new Set(store.get('nm_hiddenCats', []));
let selectedId = null, hoverNode = null, hoverDay = null, hoverCat = null;
let lastGraphSig = '', firstLoad = true, listAnim = true;
let fxOn = store.get('nm_fx', true);
let brief = null;   // briefing-mode state
const filt = { q: '', days: 31, src: '', fix: '', showLow: false, sort: 'best', day: null };
const lastSeen = store.get('nm_lastSeen', 0);
store.set('nm_lastSeen', Math.floor(nowS()));

/* ---------- small helpers ---------- */
const pad = n => String(n).padStart(2, '0');
const dayKeyD = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const dayKey = ts => dayKeyD(new Date(ts * 1000));
const idOf = x => (typeof x === 'object' ? x.id : x);
const nodeOf = x => (typeof x === 'object' ? x : nodesById.get(x));
const colorOf = n => (n && n.low ? '#4b5568' : (CATS[n?.c] || {}).color || '#94a3b8');
const isNew = n => lastSeen > 0 && n.a > lastSeen;
function ago(ts) {
  if (!ts) return '—';
  const s = nowS() - ts;
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
function fmtDur(s) {
  if (s == null) return '—';
  s = Math.round(s);
  if (s < 60) return '<1m';
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h}h ${pad(m)}m` : `${m}m`;
}
const clock = ts => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—';
const easeOutBack = t => { const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2); };
function animateNum(el, to) {
  const from = el._v ?? 0; el._v = to;
  if (from === to || reduceMotion) { el.textContent = to; return; }
  const t0 = performance.now();
  const step = t => { const k = Math.min(1, (t - t0) / 700); el.textContent = Math.round(from + (to - from) * (1 - Math.pow(1 - k, 3))); if (k < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}
function toast(html, ms = 3800) {
  const t = document.createElement('div'); t.className = 'toast'; t.innerHTML = html; $('#toasts').appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 450); }, ms);
}

/* ---------- filters ---------- */
function matchesBase(n) {           // everything except time filters
  if (!n) return false;
  if (n.low && !filt.showLow) return false;
  if (hiddenCats.has(n.c)) return false;
  if (filt.src && !n.s.includes(filt.src)) return false;
  if (filt.fix) {
    if (!n.p || n.p === 'n/a') return false;
    if (filt.fix === 'open' && n.p === 'fixed') return false;
    if (filt.fix === 'fixed' && n.p !== 'fixed') return false;
    if (filt.fix === 'exploited' && !n.x) return false;
  }
  if (filt.q) {
    const hay = n._hay || (n._hay = (n.t + ' ' + n.g.join(' ') + ' ' + n.c + ' ' + n.s.join(' ') + ' ' + (FIX[n.p]?.[1] || '') + (n.x ? ' exploited' : '')).toLowerCase());
    if (!filt.q.split(/\s+/).every(w => hay.includes(w))) return false;
  }
  return true;
}
function visible(n) {
  if (!matchesBase(n)) return false;
  if (filt.day) return dayKey(n.d) === filt.day;
  return n.d >= nowS() - filt.days * 86400;
}
function sortedVisible() {
  const vis = data.nodes.filter(visible);
  return vis.sort(filt.sort === 'new' ? (a, b) => b.d - a.d : (a, b) => (b.v - a.v) || (b.e - a.e) || (b.d - a.d));
}

/* =====================================================================
   3D GALAXY
   ===================================================================== */
function computeAnchors() {
  const N = CAT_ORDER.length, R = 240;
  CAT_ORDER.forEach((c, i) => {              // Fibonacci sphere → evenly spread constellations
    const y = 1 - (i / Math.max(N - 1, 1)) * 2, r = Math.sqrt(1 - y * y), th = i * 2.399963;
    anchors[c] = { x: Math.cos(th) * r * R, y: y * R * 0.7, z: Math.sin(th) * r * R };
  });
}
function clusterForce() {
  let nodes = [];
  const f = alpha => {
    for (const n of nodes) {
      const a = anchors[n.c]; if (!a) continue;
      const k = 0.035 * alpha;
      n.vx += (a.x - n.x) * k; n.vy += (a.y - n.y) * k; n.vz += (a.z - n.z) * k;
    }
  };
  f.initialize = ns => { nodes = ns; };
  return f;
}

/* glow sprites */
const texCache = {};
function glowTex(color, kind) {
  const key = color + kind;
  if (texCache[key]) return texCache[key];
  const S = 128, cv = document.createElement('canvas'); cv.width = cv.height = S;
  const g = cv.getContext('2d'), gr = g.createRadialGradient(S / 2, S / 2, 0, S / 2, S / 2, S / 2);
  if (kind === 'core') {
    gr.addColorStop(0, '#ffffff'); gr.addColorStop(0.16, '#ffffff'); gr.addColorStop(0.28, color);
    gr.addColorStop(0.55, color + '40'); gr.addColorStop(1, color + '00');
  } else {
    gr.addColorStop(0, color + '90'); gr.addColorStop(0.3, color + '30'); gr.addColorStop(1, color + '00');
  }
  g.fillStyle = gr; g.fillRect(0, 0, S, S);
  const t = new THREE.CanvasTexture(cv); t.colorSpace = THREE.SRGBColorSpace;
  return (texCache[key] = t);
}
const starSize = n => n.low ? 3.2 : 4 + Math.max(0, (n.v || 5) - 5) * 1.9;
function makeNodeObj(n) {
  const col = colorOf(n);
  const mk = (kind, op) => new THREE.Sprite(new THREE.SpriteMaterial({
    map: glowTex(col, kind), transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: op }));
  const g = new THREE.Group(), halo = mk('halo', 0.5), core = mk('core', 1);
  g.add(halo); g.add(core);
  n.__o = { core, halo, s: starSize(n), born: n.__bornAt ?? performance.now(), op: 1, ph: Math.random() * 6.28, col };
  return g;
}

const Graph = ForceGraph3D({ controlType: 'orbit' })(document.getElementById('graph'))
  .backgroundColor('#03050a')
  .showNavInfo(false)
  .nodeId('id')
  .nodeRelSize(4)
  .nodeVal(n => 0.5 + Math.pow(Math.max(n.v || 1, 1), 2) / 9)
  .nodeColor(n => colorOf(n))
  .nodeOpacity(0.95)
  .nodeLabel(n => `<div class="tip" style="--c:${colorOf(n)}">${esc(n.t)}<div class="tm"><b>${n.v}/10</b> · ${esc(n.c)} · ${ago(n.d)}</div></div>`)
  .linkOpacity(0.32)
  .linkDirectionalParticleSpeed(0.0055)
  .linkDirectionalParticleWidth(1.9)
  .onNodeClick(n => { stopBrief(); openItem(n.id, true); })
  .onNodeHover(n => { hoverNode = n || null; $('#graph').style.cursor = n ? 'pointer' : ''; refreshLinks(); })
  .onBackgroundClick(() => { stopBrief(); closeDetail(); })
  .cooldownTime(15000);
if (THREE) Graph.nodeThreeObject(makeNodeObj);
Graph.d3Force('charge').strength(-30);
Graph.d3Force('link').distance(l => 18 + (1 - (l.s || 0.6)) * 150).strength(l => 0.2 + (l.s || 0.6) * 0.5);
Graph.d3Force('cluster', clusterForce());
Graph.cameraPosition({ x: 0, y: 260, z: 2600 });
try { Graph.renderer().setPixelRatio(Math.min(devicePixelRatio || 1, 1.25)); } catch (e) {}
window.addEventListener('resize', () => Graph.width(innerWidth).height(innerHeight));
document.addEventListener('visibilitychange', () => document.hidden ? Graph.pauseAnimation() : Graph.resumeAnimation());

/* starfield + bloom */
let stars = null, bloom = null;
if (THREE) {
  const N = 2600, pos = new Float32Array(N * 3);
  for (let i = 0; i < N; i++) {
    const r = 2200 + Math.random() * 2800, u = Math.random() * 2 - 1, th = Math.random() * 6.283, s = Math.sqrt(1 - u * u);
    pos.set([r * s * Math.cos(th), r * u, r * s * Math.sin(th)], i * 3);
  }
  const geo = new THREE.BufferGeometry(); geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  stars = new THREE.Points(geo, new THREE.PointsMaterial({ color: 0x9fb4ff, size: 1.3, sizeAttenuation: false, transparent: true, opacity: 0.55, depthWrite: false }));
  Graph.scene().add(stars);
}
if (BloomPass) {
  try {
    bloom = new BloomPass();
    bloom.strength = 1.05; bloom.radius = 0.55; bloom.threshold = 0.06;
    Graph.postProcessingComposer().addPass(bloom);
  } catch (e) { bloom = null; console.warn('[NewsMap] bloom unavailable', e); }
}

/* camera auto-rotate when idle */
const controls = Graph.controls();
controls.enableDamping = true; controls.dampingFactor = 0.08; controls.autoRotateSpeed = 0.28;
let idleTimer;
const canRotate = () => fxOn && !reduceMotion && !selectedId && !brief;
controls.addEventListener('start', () => { controls.autoRotate = false; clearTimeout(idleTimer); if (brief) stopBrief(); });
controls.addEventListener('end', () => { clearTimeout(idleTimer); idleTimer = setTimeout(() => { controls.autoRotate = canRotate(); }, 12000); });

/* Eco mode: while a scan runs, the local AI owns the GPU → no bloom.
   Idle sleep: after 60 s without input, stop rendering entirely. */
let eco = true, sleeping = false, idleSleepTimer;   // eco until the first status says no scan is running
function applyFx() {
  if (bloom) bloom.enabled = fxOn && !eco && !guardTripped;
  if (stars) stars.visible = fxOn;
  controls.autoRotate = canRotate();
  $('#fxBtn').classList.toggle('on', fxOn);
  $('#fxBtn').title = eco && fxOn ? 'Glow is paused while a scan uses the GPU (F toggles effects)' : 'Glow, starfield and auto-rotation (F)';
}
function setEco(on) { if (on !== eco) { eco = on; applyFx(); } }
function wake() {
  clearTimeout(idleSleepTimer);
  if (sleeping) { sleeping = false; Graph.resumeAnimation(); document.body.classList.remove('asleep'); }
  idleSleepTimer = setTimeout(() => {
    if (brief || document.hidden) return;
    sleeping = true; Graph.pauseAnimation(); document.body.classList.add('asleep');
  }, 60000);
}
['pointermove', 'pointerdown', 'wheel', 'keydown'].forEach(ev => addEventListener(ev, wake, { passive: true }));
wake();

/* highlight links touching the hovered / selected star */
function refreshLinks() {
  const focus = hoverNode?.id || selectedId;
  const hl = l => focus && (idOf(l.source) === focus || idOf(l.target) === focus);
  Graph.linkColor(l => hl(l) ? colorOf(nodeOf(focus)) : 'rgba(120,140,195,0.4)')
    .linkWidth(l => hl(l) ? 0.6 : 0)
    .linkDirectionalParticles(l => hl(l) && !reduceMotion ? 3 : 0)
    .linkDirectionalParticleColor(() => colorOf(nodeOf(focus)));
}
function applyVisibility() {
  Graph.nodeVisibility(n => visible(n)).linkVisibility(l => visible(nodeOf(l.source)) && visible(nodeOf(l.target)));
  renderLegend(); renderList(); renderTimeline();
  $('#empty').classList.toggle('hidden', data.nodes.length > 0);
}

/* per-frame star animation: birth, pulse of new stars, focus dimming */
let focusKey = '', focusSetCache = null;
function focusSet() {
  const key = `${hoverNode?.id}|${hoverDay}|${hoverCat}`;
  if (key === focusKey) return focusSetCache;
  focusKey = key;
  if (hoverNode) focusSetCache = new Set([hoverNode.id, ...(adj.get(hoverNode.id) || [])]);
  else if (hoverDay) focusSetCache = new Set(data.nodes.filter(n => dayKey(n.d) === hoverDay).map(n => n.id));
  else if (hoverCat) focusSetCache = new Set(data.nodes.filter(n => n.c === hoverCat).map(n => n.id));
  else focusSetCache = null;
  return focusSetCache;
}
function animateStars(now) {
  const focus = focusSet();
  for (const n of data.nodes) {
    const o = n.__o; if (!o) continue;
    const k = (now - o.born) / 1000, grow = k <= 0 ? 0 : k >= 1 ? 1 : easeOutBack(k);
    let s = o.s * grow, hs = s * 3.3;
    const sel = n.id === selectedId;
    if (sel) { s *= 1.45; hs = s * (3.8 + 0.5 * Math.sin(now / 280)); }
    else if (isNew(n) && !reduceMotion) hs *= 1 + 0.32 * Math.sin(now / 420 + o.ph);
    o.core.scale.set(s, s, 1); o.halo.scale.set(hs, hs, 1);
    const target = focus ? (focus.has(n.id) ? 1 : 0.07) : 1;
    o.op += (target - o.op) * 0.14;
    o.core.material.opacity = o.op;
    o.halo.material.opacity = o.op * (sel ? 0.95 : 0.5);
  }
}

/* constellation labels (HTML overlay that follows cluster centroids) */
const labelEls = {};
function updateLabels() {
  const sums = {};
  for (const n of data.nodes) {
    if (n.x == null || !visible(n)) continue;
    const s = sums[n.c] || (sums[n.c] = { x: 0, y: 0, z: 0, k: 0 });
    s.x += n.x; s.y += n.y; s.z += n.z; s.k++;
  }
  const focus = focusSet();
  for (const c of CAT_ORDER) {
    let el = labelEls[c];
    if (!el) {
      el = labelEls[c] = document.createElement('div');
      el.className = 'clabel'; el.style.setProperty('--c', CATS[c].color);
      $('#labels').appendChild(el);
    }
    const s = sums[c];
    if (!s || s.k < 2) { el.style.opacity = 0; continue; }
    el.innerHTML = `${esc(c)} <em>${s.k}</em>`;
    const p = Graph.graph2ScreenCoords(s.x / s.k, s.y / s.k + 34, s.z / s.k);
    el.style.transform = `translate(${p.x}px,${p.y}px) translate(-50%,-50%)`;
    const dim = focus && hoverCat !== c && !(hoverNode && hoverNode.c === c);
    el.style.opacity = dim ? 0.15 : 0.9;
  }
}

/* frame loop + automatic quality guard (busy GPU → glow off) */
let frame = 0, fpsFrames = 0, fpsT = performance.now(), lowStreak = 0, guardTripped = false;
applyFx();
function loop(now) {
  requestAnimationFrame(loop);
  if (sleeping) { fpsT = now; fpsFrames = 0; return; }
  frame++;
  if (THREE) animateStars(now);
  if (stars && fxOn) stars.rotation.y += 0.00004;
  if (frame % 2 === 0) updateLabels();
  fpsFrames++;
  if (now - fpsT >= 1000) {
    const fps = fpsFrames * 1000 / (now - fpsT); fpsFrames = 0; fpsT = now;
    if (bloom?.enabled && fps < 24 && !document.hidden && !sleeping) {
      if (++lowStreak >= 5 && !guardTripped) {
        guardTripped = true; bloom.enabled = false;
        toast('Glow turned off to keep things smooth (the GPU is busy). Press <b>F</b> twice to turn it back on.', 6000);
      }
    } else lowStreak = 0;
  }
}
requestAnimationFrame(loop);

/* ---------- data loading ---------- */
async function loadGraph() {
  let g;
  try { g = await (await fetch('/api/graph')).json(); } catch (e) { return; }
  if (!CAT_ORDER.length) {
    g.categories.forEach(c => { CATS[c.name] = c; CAT_ORDER.push(c.name); });
    computeAnchors();
  }
  const sig = g.nodes.length + ':' + g.links.length + ':' + (g.nodes.at(-1) || {}).id;
  if (sig === lastGraphSig) return;
  lastGraphSig = sig;
  const t = performance.now();
  let added = 0;
  const nodes = g.nodes.map(n => {
    const old = nodesById.get(n.id);
    if (old) { delete old._hay; return Object.assign(old, n); }
    added++;
    const a = anchors[n.c] || { x: 0, y: 0, z: 0 }, j = () => (Math.random() - 0.5) * 70;
    return Object.assign(n, { x: a.x + j(), y: a.y + j(), z: a.z + j(), __bornAt: t + (firstLoad ? 600 + Math.random() * 1800 : Math.random() * 300) });
  });
  nodesById.clear(); nodes.forEach(n => nodesById.set(n.id, n));
  adj = new Map();
  for (const l of g.links) {
    if (!adj.has(l.source)) adj.set(l.source, new Set());
    if (!adj.has(l.target)) adj.set(l.target, new Set());
    adj.get(l.source).add(l.target); adj.get(l.target).add(l.source);
  }
  data = { nodes, links: g.links.map(l => ({ ...l })) };
  Graph.graphData(data);
  refreshLinks();
  const srcs = [...new Set(nodes.flatMap(n => n.s))].sort();
  const sel = $('#src'), cur = sel.value;
  sel.innerHTML = '<option value="">All sources</option>' + srcs.map(s => `<option>${esc(s)}</option>`).join('');
  sel.value = srcs.includes(cur) ? cur : '';
  if (firstLoad) {
    listAnim = true;
    Graph.cameraPosition({ x: 40, y: 150, z: 760 }, { x: 0, y: 0, z: 0 }, reduceMotion ? 0 : 3200);   // fly-in intro
    const fresh = nodes.filter(isNew).length;
    if (fresh) setTimeout(() => toast(`<b>${fresh}</b> new signal${fresh > 1 ? 's' : ''} since your last visit`), 2200);
  } else if (added) {
    toast(`<b>+${added}</b> new signal${added > 1 ? 's' : ''} on the map`);
  }
  firstLoad = false;
  applyVisibility();
}

/* =====================================================================
   PANELS
   ===================================================================== */
function renderLegend() {
  const counts = {};
  data.nodes.forEach(n => { if (!n.low || filt.showLow) counts[n.c] = (counts[n.c] || 0) + 1; });
  const max = Math.max(1, ...Object.values(counts));
  let html = '', lastGroup = '';
  for (const c of CAT_ORDER) {
    if (!counts[c]) continue;
    const g = CATS[c].group;
    if (g !== lastGroup) { html += `<div class="lg-group">${esc(g)}</div>`; lastGroup = g; }
    html += `<button class="lg ${hiddenCats.has(c) ? 'off' : ''}" data-c="${esc(c)}" style="--c:${CATS[c].color}">
      <i></i><span class="nm">${esc(c)}</span><span class="ct">${counts[c]}</span><span class="bar"><b style="width:${100 * counts[c] / max}%"></b></span></button>`;
  }
  $('#legend').innerHTML = html || '<div class="empty-list">No signals yet</div>';
}
$('#legend').addEventListener('click', e => {
  const el = e.target.closest('.lg'); if (!el) return;
  const c = el.dataset.c;
  hiddenCats.has(c) ? hiddenCats.delete(c) : hiddenCats.add(c);
  store.set('nm_hiddenCats', [...hiddenCats]);
  listAnim = true; applyVisibility();
});
$('#legend').addEventListener('mouseover', e => { const el = e.target.closest('.lg'); hoverCat = el ? el.dataset.c : null; });
$('#legend').addEventListener('mouseleave', () => { hoverCat = null; });

function scoreRing(v, color) {
  const C = 2 * Math.PI * 12;
  return `<div class="ring"><svg class="sring" viewBox="0 0 30 30"><circle class="rt" cx="15" cy="15" r="12"/>
    <circle class="rf" cx="15" cy="15" r="12" stroke="${color}" color="${color}" stroke-dasharray="${(C * v / 10).toFixed(1)} ${C.toFixed(1)}"/></svg><span class="sv">${v}</span></div>`;
}
function renderList() {
  const vis = sortedVisible();
  $('#count').textContent = vis.length;
  const list = $('#list');
  list.classList.toggle('anim', listAnim && !reduceMotion);
  list.innerHTML = vis.length ? vis.slice(0, 250).map((n, i) => `
    <div class="row ${n.id === selectedId ? 'sel' : ''}" data-id="${esc(n.id)}" style="--c:${colorOf(n)};--i:${i}">
      ${scoreRing(n.v, colorOf(n))}
      <div><div class="t">${isNew(n) ? '<span class="new">NEW</span>' : ''}${fixBadge(n.p, n.x)}${esc(n.t)}</div>
      <div class="m">${esc(n.s.join(' · '))} · ${ago(n.d)}</div></div>
    </div>`).join('') : `<div class="empty-list">${data.nodes.length ? 'Nothing matches these filters' : 'Waiting for the first signals…'}</div>`;
  listAnim = false;
}
$('#list').addEventListener('click', e => { const r = e.target.closest('.row'); if (r) { stopBrief(); openItem(r.dataset.id, true); } });
$('#list').addEventListener('mouseover', e => {
  const r = e.target.closest('.row'); const n = r ? nodesById.get(r.dataset.id) : null;
  if (n !== hoverNode) { hoverNode = n; refreshLinks(); }
});
$('#list').addEventListener('mouseleave', () => { hoverNode = null; refreshLinks(); });

/* timeline */
function renderTimeline() {
  const today = new Date(), days = [];
  for (let i = 30; i >= 0; i--) days.push(new Date(today.getFullYear(), today.getMonth(), today.getDate() - i));
  const counts = {};
  data.nodes.forEach(n => { if (matchesBase(n)) { const k = dayKey(n.d); counts[k] = (counts[k] || 0) + 1; } });
  const max = Math.max(1, ...Object.values(counts));
  const cutoff = nowS() - filt.days * 86400;
  $('#tlBars').innerHTML = days.map((d, i) => {
    const k = dayKeyD(d), c = counts[k] || 0, inRange = filt.day ? filt.day === k : d.getTime() / 1000 + 86400 > cutoff;
    return `<button class="tb ${filt.day === k ? 'on' : ''} ${inRange ? '' : 'out'} ${c ? '' : 'zero'}" data-k="${k}" data-c="${c}" style="--i:${i}">
      <i style="height:${c ? 10 + 90 * c / max : 4}%"></i></button>`;
  }).join('');
  const f = d => d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  $('#tlAxis').innerHTML = `<span>${f(days[0])}</span><span>${f(days[15])}</span><span>today</span>`;
  $('#tlDay').innerHTML = filt.day ? `<span class="chip" id="tlClear">${new Date(filt.day + 'T12:00').toLocaleDateString([], { month: 'short', day: 'numeric' })} ✕</span>` : '';
  $('#tlInfo').textContent = '';
}
$('#tlBars').addEventListener('mouseover', e => {
  const b = e.target.closest('.tb'); if (!b) return;
  hoverDay = b.dataset.k;
  $('#tlInfo').textContent = `${new Date(hoverDay + 'T12:00').toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })} · ${b.dataset.c} signal${b.dataset.c === '1' ? '' : 's'}`;
});
$('#tlBars').addEventListener('mouseleave', () => { hoverDay = null; $('#tlInfo').textContent = ''; });
$('#tlBars').addEventListener('click', e => {
  const b = e.target.closest('.tb'); if (!b) return;
  filt.day = filt.day === b.dataset.k ? null : b.dataset.k;
  listAnim = true; applyVisibility();
});
$('#tlDay').addEventListener('click', () => { filt.day = null; listAnim = true; applyVisibility(); });

/* ---------- patch / fix status ---------- */
const FIX = { fixed: ['fixed', 'Fixed', '✓'], partial: ['partial', 'Partly fixed', '◐'], no_fix: ['nofix', 'No fix yet', '✕'],
  unknown: ['unknown', 'Fix unknown', '?'] };
function fixBadge(p, x, big) {
  const f = FIX[p]; if (!f && !x) return '';
  return `${f ? `<span class="fx ${f[0]} ${big ? 'big' : ''}">${f[2]} ${f[1]}</span>` : ''}${x ? `<span class="fx kev ${big ? 'big' : ''}">⚠ Exploited</span>` : ''}`;
}
const linkList = (arr, label) => arr?.length ? `<div class="pt-row"><span class="pt-k">${label}</span><span class="pt-v">${arr.map(u => safeUrl(u)
  ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.replace(/^https?:\/\/(www\.)?/, '').slice(0, 64))} ↗</a>` : '').join('')}</span></div>` : '';
const kv = (k, v) => v ? `<div class="pt-row"><span class="pt-k">${k}</span><span class="pt-v">${v}</span></div>` : '';
function patchHtml(it) {
  const p = it.patch;
  if (!p) return `<div class="pt-empty">Not checked yet. <button class="ibtn sm" data-fixcheck="${esc(it.id)}">Check for a fix now</button></div>`;
  const ids = (p.ids || []).map(x => {
    const ref = x.id.startsWith('CVE-') ? `https://nvd.nist.gov/vuln/detail/${x.id}` : `https://github.com/advisories/${x.id}`;
    const k = x.kev;
    return `<div class="pt-id">
      <div class="pt-head"><a class="mono" href="${ref}" target="_blank" rel="noopener">${esc(x.id)} ↗</a>${fixBadge(x.status)}
        ${x.cvss ? `<span class="pt-cvss mono" title="${esc(x.cvss)}">CVSS ${esc(x.cvss.split('/')[0].replace('CVSS:', 'v'))}</span>` : ''}
        <a class="pt-osv" href="https://osv.dev/vulnerability/${esc(x.id)}" target="_blank" rel="noopener">OSV ↗</a></div>
      ${x.summary ? `<p class="pt-sum">${esc(x.summary)}</p>` : ''}
      ${k ? `<div class="pt-kev">⚠ <b>Exploited in the wild</b> (CISA, added ${esc(k.added || '')})${k.ransomware ? ' · used by ransomware' : ''}<br>
        <span>Required action: ${esc(k.action || '')}${k.due ? ` · deadline ${esc(k.due)}` : ''}</span></div>` : ''}
      ${kv('Fixed in', (x.fixed_in || []).map(v => `<span class="chip">${esc(v)}</span>`).join(''))}
      ${kv('Patched by', (x.distros || []).map(v => `<span class="chip">${esc(v)}</span>`).join(''))}
      ${kv('Still open', (x.pkg_open || []).filter(v => !(x.pkg_fixed || []).includes(v)).map(v => `<span class="chip bad">${esc(v)}</span>`).join(''))}
      ${linkList(x.commits, 'Fix commit')}${linkList(x.fix_links, 'Patch')}${x.status !== 'fixed' ? linkList(x.advisories, 'Advisory') : ''}
      ${!x.found ? '<div class="pt-note">Not in OSV or NVD yet (very new CVEs can take a few days).</div>' : ''}
    </div>`;
  }).join('');
  const ai = p.ai && p.ai.verdict !== 'not_a_vulnerability' ? `<div class="pt-ai"><span class="pt-k">From the article (AI)</span>
    <b>${esc({ fixed: 'Says it is fixed', no_fix: p.ai.raw_verdict === 'workaround_only' ? 'Only a workaround so far' : 'Says there is no fix yet', unclear: 'Doesn’t say' }[p.ai.verdict] || p.ai.verdict)}</b>
    ${p.ai.product ? ` · ${esc(p.ai.product)}` : ''}${p.ai.fixed_in ? ` · fixed in <b>${esc(p.ai.fixed_in)}</b>` : ''}
    ${p.ai.evidence ? `<div class="pt-ev">${esc(p.ai.evidence)}</div>` : ''}</div>` : '';
  const rel = p.related?.length ? `<div class="pt-rel"><span class="pt-k">Possibly related CVEs (NVD search for “${esc(p.ai?.product || '')}”)</span>
    ${p.related.map(r => `<div><a class="mono" href="https://nvd.nist.gov/vuln/detail/${esc(r.id)}" target="_blank" rel="noopener">${esc(r.id)} ↗</a> ${esc(r.desc)}</div>`).join('')}</div>` : '';
  const none = !ids && !ai ? `<div class="pt-note">No CVE number in this story. The AI check runs with the next Refresh.</div>` : '';
  return `<div class="pt-top">${fixBadge(p.status, p.exploited, true)}
      <span class="pt-when">checked ${ago(p.checked_at)}${p.status !== 'fixed' ? ' · rechecks every 12 h' : ''}</span>
      <button class="ibtn sm" data-fixcheck="${esc(it.id)}">Check again</button></div>${ids}${ai}${rel}${none}`;
}
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-fixcheck]'); if (!b) return;
  const id = b.dataset.fixcheck; b.disabled = true; b.textContent = 'Checking…';
  try {
    const r = await fetch('/api/patch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }) });
    const p = await r.json();
    if (!r.ok) throw new Error(p.error || r.status);
    const n = nodesById.get(id); if (n) { n.p = p.status; n.x = p.exploited; n._hay = null; }
    const box = document.getElementById('patchBox');
    if (box && selectedId === id) box.innerHTML = patchHtml({ id, patch: p });
    renderList();
  } catch (err) { b.disabled = false; b.textContent = 'Check again'; toast('Fix check failed: ' + esc(String(err.message || err))); }
});

/* =====================================================================
   DOSSIER
   ===================================================================== */
function flyTo(n) {
  if (!n || n.x == null) return;
  controls.autoRotate = false;
  const c = Graph.cameraPosition();
  const dx = c.x - n.x, dy = c.y - n.y, dz = c.z - n.z, d = Math.hypot(dx, dy, dz) || 1, k = 260 / d;
  Graph.cameraPosition({ x: n.x + dx * k, y: n.y + dy * k, z: n.z + dz * k }, { x: n.x, y: n.y, z: n.z }, reduceMotion ? 0 : 1300);
}
function gauge(v, color) {
  const L = Math.PI * 34;
  return `<div class="gauge-w"><svg class="gauge" viewBox="0 0 84 50"><path class="gt" d="M8 44 A34 34 0 0 1 76 44"/>
    <path class="gf" d="M8 44 A34 34 0 0 1 76 44" stroke="${color}" color="${color}" stroke-dasharray="${L.toFixed(1)}" stroke-dashoffset="${L.toFixed(1)}" data-off="${(L * (1 - v / 10)).toFixed(1)}"/></svg>
    <div class="gv"><b>${v}</b><small>/10</small></div></div>`;
}
const CHECK_LABELS = [['new_information', 'New info'], ['concrete_details', 'Concrete details'], ['technical_depth', 'Technical depth'],
  ['primary_source', 'Primary source'], ['broad_impact', 'Broad impact'], ['actionable', 'Actionable'], ['expert_discussion', 'Expert discussion'],
  ['opinion_or_speculation', 'Opinion / speculation', true], ['promotional', 'Promotional', true]];
let openSeq = 0;
async function openItem(id, fly) {
  const seq = ++openSeq;
  selectedId = id;
  controls.autoRotate = false;
  refreshLinks();
  document.querySelectorAll('#list .row').forEach(r => r.classList.toggle('sel', r.dataset.id === id));
  const node = nodesById.get(id);
  if (fly) flyTo(node);
  let it;
  try { it = await (await fetch('/api/item?id=' + encodeURIComponent(id))).json(); } catch (e) { return; }
  if (seq !== openSeq) return;
  const s = it.summary || {}, color = (CATS[it.category] || {}).color || '#94a3b8', v = s.value_score ?? it.value_score ?? 0;
  const list = arr => (arr || []).map(x => `<li>${esc(x)}</li>`).join('');
  const maxE = Math.max(1, ...(it.sources || []).map(x => x.engagement || 0));
  const srcs = (it.sources || []).map(x => {
    const u = safeUrl(x.discuss_url);
    const bits = [x.engagement ? `${x.engagement} pts` : '', x.comments ? `${x.comments} comments` : '', x.rank ? `top #${x.rank}` : ''].filter(Boolean).join(' · ');
    return `<div class="src">${u ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(x.source)} ↗</a>` : `<span class="n">${esc(x.source)}</span>`}
      <span class="e">${bits}</span>${x.engagement ? `<div class="eb"><b style="width:${100 * x.engagement / maxE}%"></b></div>` : ''}</div>`;
  }).join('');
  const discuss = (it.sources || []).filter(x => safeUrl(x.discuss_url) && x.discuss_url !== it.url);
  const dt = new Date(it.created_at * 1000);
  let i = 0; const rv = () => `class="rv" style="--i:${i++}"`;
  const body = $('#detailBody');
  body.innerHTML = `
    <div ${rv()}><div class="d-eyebrow"><span class="cat">${esc(it.category)}</span><span class="sep">/</span>
      <span>${dt.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
      ${it.status === 'low' ? '<span class="sep">/</span><span>dropped</span>' : ''}${node && isNew(node) ? '<span class="new">NEW</span>' : ''}</div></div>
    <h1 class="d-title" ${rv()}>${esc(s.headline || it.title)}</h1>
    <div class="d-score" ${rv()}>${gauge(v, color)}<div class="d-sc-r"><div class="lbl">Signal strength</div>
      ${s.checks ? `<div class="checks">${CHECK_LABELS.filter(([k, , neg]) => !neg || s.checks[k]).map(([k, label, neg]) =>
        `<span class="ck ${s.checks[k] ? (neg ? 'neg' : 'on') : ''}">${s.checks[k] ? (neg ? '✕' : '✓') : '·'} ${label}</span>`).join('')}</div>` : ''}
      <div class="why">${esc(s.value_reason || '')}</div></div></div>
    ${s.tldr ? `<p class="d-tldr" ${rv()}>${esc(s.tldr)}</p>` : ''}
    ${it.vuln ? `<section class="d-sec" ${rv()}><h4>Fix status <small>OSV · NVD · CISA KEV</small></h4><div id="patchBox" class="patch">${patchHtml(it)}</div></section>` : ''}
    ${s.key_points?.length ? `<section class="d-sec" ${rv()}><h4>Key points</h4><ol class="kp">${list(s.key_points)}</ol></section>` : ''}
    ${s.why_it_matters ? `<section class="d-sec" ${rv()}><h4>Why it matters</h4><p class="d-why">${esc(s.why_it_matters)}</p></section>` : ''}
    ${s.insider_notes?.length ? `<section class="d-sec" ${rv()}><h4>Insider notes <small>from the discussion</small></h4><div class="notes">${s.insider_notes.map(x => `<div class="note">${esc(x)}</div>`).join('')}</div></section>` : ''}
    ${s.glossary?.length ? `<section class="d-sec" ${rv()}><h4>Words explained</h4><dl class="gloss">${s.glossary.map(g => `<dt>${esc(g.term)}</dt><dd>${esc(g.meaning)}</dd>`).join('')}</dl></section>` : ''}
    ${s.entities?.length || s.tags?.length ? `<section class="d-sec" ${rv()}><h4>Entities &amp; tags</h4><div class="chips">${(s.entities || []).map(e => `<span class="chip">${esc(e)}</span>`).join('')}${(s.tags || []).map(t => `<span class="chip tag">#${esc(t)}</span>`).join('')}</div></section>` : ''}
    <section class="d-sec" ${rv()}><h4>Sources</h4><div class="srcs">${srcs}</div></section>
    <div class="d-actions" ${rv()}>
      ${safeUrl(it.url) ? `<a class="main" href="${esc(it.url)}" target="_blank" rel="noopener">Read original ↗</a>` : ''}
      ${discuss.map(x => `<a href="${esc(x.discuss_url)}" target="_blank" rel="noopener">${esc(x.source)} thread ↗</a>`).join('')}
    </div>
    <p class="d-orig" ${rv()}>Original title: ${esc(it.title)}</p>`;
  const d = $('#detail');
  d.style.setProperty('--dc', color);
  body.classList.remove('swap'); void body.offsetWidth; body.classList.add('swap');
  body.scrollTop = 0;
  const order = sortedVisible(), pos = order.findIndex(n => n.id === id);
  $('#dIdx').textContent = pos >= 0 ? `Signal ${pad(pos + 1)} / ${pad(order.length)}` : 'Signal';
  d.classList.add('open'); d.setAttribute('aria-hidden', 'false');
  document.body.classList.add('detail-open');
  requestAnimationFrame(() => requestAnimationFrame(() => body.querySelectorAll('.gf').forEach(p => p.style.strokeDashoffset = p.dataset.off)));
}
function closeDetail() {
  if (!selectedId && !$('#detail').classList.contains('open')) return;
  $('#detail').classList.remove('open'); $('#detail').setAttribute('aria-hidden', 'true');
  document.body.classList.remove('detail-open');
  selectedId = null; refreshLinks();
  document.querySelectorAll('#list .row.sel').forEach(r => r.classList.remove('sel'));
  clearTimeout(idleTimer); idleTimer = setTimeout(() => { controls.autoRotate = canRotate(); }, 6000);
}
function step(delta) {
  const order = sortedVisible(); if (!order.length) return;
  let i = order.findIndex(n => n.id === selectedId);
  i = i < 0 ? 0 : (i + delta + order.length) % order.length;
  openItem(order[i].id, true);
  const row = document.querySelector(`#list .row[data-id="${CSS.escape(order[i].id)}"]`);
  row?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}
$('#closeDetail').onclick = () => { stopBrief(); closeDetail(); };
$('#prevBtn').onclick = () => { stopBrief(); step(-1); };
$('#nextBtn').onclick = () => { stopBrief(); step(1); };

/* briefing mode: auto-fly through the top stories */
const BRIEF_MS = 11000;
function briefSeq() {
  let seq = data.nodes.filter(n => visible(n) && !n.low && n.d > nowS() - 2 * 86400);
  if (seq.length < 5) seq = data.nodes.filter(n => visible(n) && !n.low);
  return seq.sort((a, b) => (b.v - a.v) || (b.e - a.e)).slice(0, 12).map(n => n.id);
}
function startBrief() {
  const seq = briefSeq(); if (!seq.length) { toast('No signals to brief yet'); return; }
  brief = { seq, i: 0 };
  document.body.classList.add('briefing'); $('#briefBtn').classList.add('on');
  document.documentElement.style.setProperty('--bt', BRIEF_MS + 'ms');
  toast(`Briefing: top <b>${seq.length}</b> signals · ← → to skip · Esc to stop`);
  briefStep();
}
function briefStep() {
  if (!brief) return;
  openItem(brief.seq[brief.i], true);
  const bar = $('.brief-bar i'); bar.style.animation = 'none'; void bar.offsetWidth; bar.style.animation = '';
  clearTimeout(brief.t);
  brief.t = setTimeout(() => { if (!brief) return; brief.i++; brief.i >= brief.seq.length ? stopBrief(true) : briefStep(); }, BRIEF_MS);
}
function stopBrief(finished) {
  if (!brief) return;
  clearTimeout(brief.t); brief = null;
  document.body.classList.remove('briefing'); $('#briefBtn').classList.remove('on');
  if (finished) toast('Briefing complete');
}
$('#briefBtn').onclick = () => brief ? stopBrief() : startBrief();

/* =====================================================================
   CONTROLS
   ===================================================================== */
const seg = $('#days');
function moveSegInd() {
  const on = seg.querySelector('button.on'), ind = seg.querySelector('.seg-ind');
  if (!on) return;
  ind.style.width = on.offsetWidth + 'px';
  ind.style.transform = `translateX(${on.offsetLeft}px)`;
}
seg.addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  seg.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
  filt.days = +b.dataset.v; filt.day = null; moveSegInd(); listAnim = true; applyVisibility();
});
document.fonts?.ready.then(moveSegInd); setTimeout(moveSegInd, 300); window.addEventListener('resize', moveSegInd);
$('#sort').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  $('#sort').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
  filt.sort = b.dataset.v; listAnim = true; renderList();
});
let qTimer;
$('#q').addEventListener('input', e => { clearTimeout(qTimer); qTimer = setTimeout(() => { filt.q = e.target.value.trim().toLowerCase(); listAnim = true; applyVisibility(); }, 160); });
$('#src').onchange = e => { filt.src = e.target.value; listAnim = true; applyVisibility(); };
$('#fix').onchange = e => { filt.fix = e.target.value; listAnim = true; applyVisibility(); };
$('#showLow').onchange = e => { filt.showLow = e.target.checked; listAnim = true; applyVisibility(); };
$('#fxBtn').onclick = () => { fxOn = !fxOn; guardTripped = false; lowStreak = 0; store.set('nm_fx', fxOn); applyFx();
  if (eco && fxOn) toast('Glow returns automatically when the scan finishes'); };

document.addEventListener('keydown', e => {
  const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement?.tagName);
  if (e.key === 'Escape') { if (typing) document.activeElement.blur(); else { stopBrief(); closeDetail(); } return; }
  if (typing) return;
  if (e.key === '/') { e.preventDefault(); $('#q').focus(); }
  else if (['ArrowRight', 'ArrowDown', 'j'].includes(e.key)) { e.preventDefault(); if (brief) { brief.i = Math.min(brief.i + 1, brief.seq.length - 1); briefStep(); } else step(1); }
  else if (['ArrowLeft', 'ArrowUp', 'k'].includes(e.key)) { e.preventDefault(); if (brief) { brief.i = Math.max(brief.i - 1, 0); briefStep(); } else step(-1); }
  else if (e.key === 'b') brief ? stopBrief() : startBrief();
  else if (e.key === 'f') $('#fxBtn').click();
});

$('#refreshBtn').onclick = async () => {
  if ($('#refreshBtn').classList.contains('busy')) return;
  const r = await fetch('/api/refresh', { method: 'POST' });
  missionHidden = false;
  const ag = lastStatus?.agent?.enabled;
  toast(r.status === 409 ? 'An update is already running'
    : ag ? 'Asked Haiku to read the latest news. New stars appear as it finishes stories (usually 10–30 min).'
    : 'Scan started: stars will appear as stories are read');
  poll();
};
async function control(action) { await fetch('/api/control', { method: 'POST', body: JSON.stringify({ action }) }); poll(); }
$('#pPause').onclick = () => control(lastStatus?.run?.state === 'paused' || lastStatus?.run?.control === 'pause' ? 'resume' : 'pause');
let stopArmed = false;
$('#pStop').onclick = () => {
  if (!stopArmed) { stopArmed = true; $('#pStop').textContent = 'Confirm stop'; setTimeout(() => { stopArmed = false; $('#pStop').textContent = 'Stop'; }, 4000); return; }
  stopArmed = false; $('#pStop').textContent = 'Stop'; control('stop');
};
let missionHidden = false;
$('#pHide').onclick = e => { e.stopPropagation(); missionHidden = true; $('#mission').classList.add('hidden'); };
$('#mcHead').onclick = () => $('#mission').classList.toggle('collapsed');
$('#statusPill').onclick = () => { missionHidden = false; if (lastStatus?.run) { $('#mission').classList.remove('hidden', 'collapsed'); } };

/* =====================================================================
   STATUS POLLING → MISSION CONTROL
   ===================================================================== */
let lastStatus = null, wasRunning = false, lastKept = -1, lastGraphLoad = 0, pollTimer;
const STATE_TXT = { running: 'Scanning', waiting: 'Waiting for GPU', paused: 'Paused', done: 'Complete', failed: 'Failed', stopped: 'Stopped', interrupted: 'Interrupted' };
const STEP_META = { collect: ['Collect', '⇣'], triage: ['Filter', '⌕'], summarize: ['Read', '✎'], patches: ['Fixes', '⛨'], map: ['Map', '✦'] };

async function poll() {
  clearTimeout(pollTimer);
  let st;
  try { st = await (await fetch('/api/status')).json(); }
  catch (e) { setPill('server offline · run NewsMap.cmd', 'err'); pollTimer = setTimeout(poll, 5000); return; }
  lastStatus = st;
  renderStatus(st);
  const kept = st.run?.stats?.kept ?? 0;
  if ((st.running && kept !== lastKept && Date.now() - lastGraphLoad > 4000) || (wasRunning && !st.running)) {
    lastKept = kept; lastGraphLoad = Date.now(); loadGraph();
  }
  wasRunning = st.running;
  pollTimer = setTimeout(poll, st.running ? 1500 : 15000);
}
function setPill(text, cls) { $('#statusText').textContent = text; $('#statusPill').className = 'status ' + (cls || ''); }

function renderStatus(st) {
  const run = st.run, lib = st.library;
  setEco(!!st.running);
  const rb = $('#refreshBtn');
  const agBusy = !st.running && (st.agent?.running || st.agent?.requested);
  rb.classList.toggle('busy', st.running || agBusy); $('#refreshTxt').textContent = st.running ? 'Scanning' : agBusy ? 'Haiku working' : 'Refresh';
  if (agBusy) {
    setPill(st.agent.running ? `Haiku is reading the news · ${lib.done} signals` : 'starting Haiku…', 'run');
  } else if (st.running) {
    const w = run.state === 'waiting', p = run.state === 'paused';
    setPill(`${w ? 'waiting for GPU' : p ? 'paused' : 'scanning'} · ${run.pct}% · ${fmtDur(run.eta_s)} left`, w || p ? 'wait' : 'run');
  } else {
    setPill(`${lib.done} signals · ${lib.last_success ? 'updated ' + ago(lib.last_success) : 'never updated'} · next ${clock(st.next_scheduled)}`, run?.state === 'failed' ? 'err' : '');
  }
  const m = $('#mission');
  if (!run || missionHidden || (!st.running && !wasRunning && m.classList.contains('hidden'))) { if (!run || missionHidden) m.classList.add('hidden'); return; }
  m.classList.remove('hidden');
  m.className = m.className.replace(/\b(running|waiting|paused|done|failed|stopped|interrupted)\b/g, '').trim() + ' ' + run.state;

  const pct = st.running ? run.pct || 0 : (run.state === 'done' ? 100 : run.pct || 0);
  $('#ringFg').style.strokeDashoffset = (169.65 * (1 - pct / 100)).toFixed(1);
  $('#pPct').textContent = pct + '%';
  $('#pEyebrow').textContent = run.mode === 'backfill' ? 'First scan · last 30 days' : run.scheduled ? 'Scheduled scan' : 'Manual scan';
  $('#pTitle').textContent = st.running ? (STATE_TXT[run.state] || 'Scanning') : 'Last scan';
  $('#pEtaShort').textContent = st.running ? `≈ ${fmtDur(run.eta_s)} left · done ${clock(run.finish_at)}` : `took ${fmtDur(run.elapsed_s)} · ${run.finished_at ? ago(run.finished_at) : ''}`;
  const b = $('#pState'); b.textContent = (STATE_TXT[run.state] || run.state || '').toLowerCase(); b.className = 'badge ' + run.state;
  $('#tEl').textContent = fmtDur(run.elapsed_s);
  $('#tRem').textContent = st.running ? fmtDur(run.eta_s) : '—';
  $('#tEnd').textContent = st.running ? clock(run.finish_at) : clock(run.finished_at);
  $('#pWait').classList.toggle('hidden', !run.waiting); $('#pWait').textContent = run.waiting || '';

  const keys = Object.keys(run.stages || {});
  $('#pSteps').innerHTML = keys.map((k, i) => {
    const s = run.stages[k], [label, ic] = STEP_META[k] || [k, '•'];
    const conn = i ? `<div class="conn ${run.stages[keys[i - 1]].state === 'done' ? 'done' : ''} ${s.state === 'active' ? 'flow' : ''}"></div>` : '';
    const icon = s.state === 'done' ? '✓' : s.state === 'failed' || s.state === 'stopped' ? '✕' : ic;
    return `${conn}<div class="st ${s.state}"><div class="bub">${icon}</div><div class="sl">${label}</div><div class="sc">${s.total ? `${s.done}/${s.total}` : '&nbsp;'}</div></div>`;
  }).join('');
  const active = keys.map(k => run.stages[k]).find(s => s.state === 'active');
  $('#pStepDetail').innerHTML = active
    ? `${esc(active.label)}${active.note ? ' · ' + esc(active.note) : ''}<div class="sbar"><b style="width:${active.total ? 100 * active.done / active.total : 0}%"></b></div>`
    : esc(keys.map(k => run.stages[k].note).filter(Boolean).slice(-1)[0] || '');
  $('#pNow').textContent = run.current || (st.running ? '…' : 'idle');
  const x = run.stats || {};
  animateNum($('#sSeen'), x.candidates ?? 0); animateNum($('#sPass'), x.relevant ?? 0);
  animateNum($('#sKept'), x.kept ?? 0); animateNum($('#sLow'), x.low ?? 0);
  const warns = run.warnings || [];
  $('#pWarnWrap').classList.toggle('hidden', !warns.length);
  const feedW = warns.filter(w => /feed not found|offline|timed out|blocked|rate-limited|Timeout|ConnectionError|HTTPError/.test(w)).length;
  $('#pWarnSum').textContent = feedW === warns.length
    ? `${warns.length} source${warns.length === 1 ? '' : 's'} unreachable (harmless, skipped)`
    : `${warns.length} warning${warns.length === 1 ? '' : 's'}`;
  $('#pWarn').innerHTML = warns.slice(-50).map(w => `<li>${esc(w)}</li>`).join('');
  const logEl = $('#pLog'), atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 4;
  logEl.innerHTML = esc((run.log || []).join('\n')) + (st.running ? '\n<span class="cur"></span>' : '');
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;
  $('#pPause').textContent = run.state === 'paused' || run.control === 'pause' ? 'Resume' : 'Pause';
  $('#pPause').disabled = $('#pStop').disabled = !st.running;
}

loadGraph().then(poll);
