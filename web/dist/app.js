import { mountExperiments } from './experiments.js';
// Shell: run picker, hash router, shared ctx for view modules (overview / explore / trust).
const VIEWS = ['overview', 'explore', 'trust'];
const state = { run: localStorage.getItem('observatory.run') || null, mod: null, view: null, pendingRun: null, routeSeq: 0 };

export const fmt = {
  n: (x) => (x == null || Number.isNaN(x) ? '—' : Number(x).toLocaleString('zh-CN')),
  bytes(b) {
    if (b == null) return '—';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = b, i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
  },
  pct: (x) => (x == null ? '—' : `${(x * 100).toFixed(1)}%`),
  time: (s) => (s == null ? '—' : new Date(s * 1000).toLocaleTimeString('zh-CN', { hour12: false })),
  ago(s) {
    if (s == null) return '—';
    const d = Date.now() / 1000 - s;
    if (d < 60) return `${Math.max(0, Math.round(d))} 秒前`;
    if (d < 3600) return `${Math.round(d / 60)} 分钟前`;
    return `${Math.round(d / 3600)} 小时前`;
  },
};

export function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v == null || v === '') continue;
    if (Array.isArray(v)) v.forEach((x) => u.append(k, x));
    else u.append(k, v);
  }
  return u.toString();
}

async function get(path, params = {}) {
  const p = { ...params };
  const global = path.startsWith('/api/system') || path.startsWith('/api/runs');
  if (state.run && p.run === undefined && !global) p.run = state.run;
  const q = qs(p);
  const res = await fetch(path + (q ? (path.includes('?') ? '&' : '?') + q : ''));
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json();
}

async function post(path, body) {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
  return data;
}

export const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function ctx() {
  return { run: state.run, get, post, fmt, qs, escapeHtml, openTab: (view, params) => openTab(view, params) };
}

function openTab(view, params) {
  if (params) sessionStorage.setItem('observatory.open', JSON.stringify({ view, params }));
  location.hash = `#/${view}`;
}

async function loadRuns() {
  const select = document.getElementById('run');
  try {
    const data = await get('/api/runs');
    const runs = (data?.runs ?? []).filter((r) => r.id !== '_unassigned');
    if (state.pendingRun && !runs.some((r) => r.id === state.pendingRun)) runs.unshift({id: state.pendingRun, objects: 0});
    if (runs.some((r) => r.id === state.pendingRun && r.objects > 0)) state.pendingRun = null;
    if (!runs.length) { select.innerHTML = '<option>（还没有数据）</option>'; return; }
    if (!runs.some((r) => r.id === state.run)) state.run = runs[0].id;
    select.innerHTML = runs.map((r) => `<option value="${escapeHtml(r.id)}">${escapeHtml(r.id)} · ${fmt.n(r.objects)} 个对象</option>`).join('');
    select.value = state.run;
  } catch {
    select.innerHTML = '<option>（后端不可达）</option>';
  }
}

async function route() {
  const seq = ++state.routeSeq;
  const view = VIEWS.find((v) => location.hash === `#/${v}`) ?? 'overview';
  document.querySelectorAll('#tabs a').forEach((a) => a.classList.toggle('active', a.dataset.view === view));
  if (state.mod?.dispose) state.mod.dispose();
  const root = document.getElementById('view');
  root.className = `view view-${view}`;
  root.innerHTML = '<div class="loading">加载中…</div>';
  state.view = view;
  try {
    const mod = await import(`./views/${view}.js`);
    if (seq !== state.routeSeq) return;
    state.mod = mod;
    root.innerHTML = '';
    await state.mod.render(root, ctx());
  } catch (err) {
    if (seq !== state.routeSeq) return;
    root.innerHTML = `<div class="card error">页面加载失败：${escapeHtml(err.message)}</div>`;
  }
}

async function pollLive() {
  const live = document.getElementById('live');
  try {
    const o = await get('/api/overview');
    const active = !!o?.active;
    live.classList.toggle('on', active);
    live.querySelector('b').textContent = active ? `接收中 · ${fmt.n(Math.round(o.rate))} 对象/秒` : '空闲';
  } catch {
    live.classList.remove('on');
    live.querySelector('b').textContent = '后端不可达';
  }
}

document.getElementById('run').addEventListener('change', (e) => {
  state.run = e.target.value;
  localStorage.setItem('observatory.run', state.run);
  route();
  pollLive();
});
window.addEventListener('hashchange', route);

await loadRuns();
route();
pollLive();
setInterval(pollLive, 3000);
setInterval(loadRuns, 15000);

function selectExperiment(exp, target) {
  if (!exp?.test_id) return;
  state.run = exp.test_id; state.pendingRun = exp.test_id;
  localStorage.setItem('observatory.run', state.run);
  loadRuns();
  if (target && location.hash !== `#/${target}`) location.hash = `#/${target}`;
  else route();
  pollLive();
}
mountExperiments(document.getElementById('experiment'), {
  fmt,
  onStart: (exp, navigate) => selectExperiment(exp, navigate ? 'overview' : null),
  onTrust: (exp) => selectExperiment(exp, 'trust'),
});
