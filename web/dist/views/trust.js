// 数据可信与系统：回执对账、隔离区、系统资源
let timer = null;
let alive = false;

const STATUS = {
  pass: { icon: '✓', label: '通过', cls: 'tr-pass' },
  fail: { icon: '✗', label: '未通过', cls: 'tr-fail' },
  warn: { icon: '!', label: '注意', cls: 'tr-warn' },
  unknown: { icon: '?', label: '无法验证', cls: 'tr-unknown' },
};

const BATCH_STATES = [
  { ch: 'A', icon: '✓', label: '已确认且已入库', color: 'var(--good)', css: '--good', fb: '#0ca30c' },
  { ch: 'M', icon: '✗', label: '已确认但缺失', color: 'var(--critical)', css: '--critical', fb: '#d03b3b' },
  { ch: 'U', icon: '!', label: '已入库但发生器未确认', color: 'var(--warning)', css: '--warning', fb: '#fab219' },
  { ch: 'X', icon: '⚠', label: '已发送但未确认、未入库', color: 'var(--serious)', css: '--serious', fb: '#ec835a' },
  { ch: '-', icon: '·', label: '未发送', color: 'var(--grid)', css: '--grid', fb: '#2c2c2a' },
];

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (x) => (x == null || Number.isNaN(Number(x)) ? '—' : Number(x).toLocaleString('zh-CN'));
const val = (x) => (x == null ? '—' : typeof x === 'number' ? num(x) : esc(x));
const pct = (r) => (r == null || !Number.isFinite(r) ? '—' : `${(r * 100).toFixed(1)}%`);
const level = (r) => (r == null ? null : r >= 0.9 ? 'critical' : r >= 0.75 ? 'warning' : 'normal');

function bytes(b) {
  if (b == null || Number.isNaN(Number(b))) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = Number(b);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${i === 0 || v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

function clock(sec) {
  if (sec == null) return '—';
  return new Date(sec * 1000).toLocaleString('zh-CN', { hour12: false });
}

function duration(sec) {
  if (sec == null) return '—';
  const t = Math.floor(sec);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  return h ? `${h} 小时 ${m} 分` : m ? `${m} 分 ${s} 秒` : `${s} 秒`;
}

export async function render(root, ctx) {
  dispose();
  alive = true;
  root.innerHTML = `
    <div class="tr-page">
      <section class="card tr-reconcile">
        <div class="tr-head">
          <h2 class="card-title">回执对账</h2>
          <label class="tr-upload"><input type="file" accept="application/json,.json" hidden><span>上传回执 JSON</span></label>
        </div>
        <div class="tr-reconcile-body"><p class="muted">加载中…</p></div>
      </section>
      <div class="tr-cols">
        <section class="card tr-quarantine"><h2 class="card-title">隔离区</h2><div class="tr-q-body"><p class="muted">加载中…</p></div></section>
        <section class="card tr-system"><h2 class="card-title">系统资源</h2><div class="tr-s-body"><p class="muted">加载中…</p></div></section>
      </div>
    </div>`;

  const recBody = root.querySelector('.tr-reconcile-body');
  const uploadLabel = root.querySelector('.tr-upload span');
  const input = root.querySelector('.tr-upload input');
  input.addEventListener('change', async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    recBody.innerHTML = `<p class="muted">正在对账：${esc(file.name)}…</p>`;
    try {
      const text = await file.text();
      const result = await ctx.post('/api/reconcile', text);
      if (!alive) return;
      renderReconcile(recBody, result);
      uploadLabel.textContent = '重新上传回执';
    } catch (err) {
      if (alive) recBody.innerHTML = `<p class="tr-error">✗ 对账失败：${esc(err && err.message ? err.message : err)}</p>`;
    } finally {
      input.value = '';
    }
  });

  const sysBody = root.querySelector('.tr-s-body');
  await Promise.allSettled([
    loadReconcile(recBody, uploadLabel, ctx),
    loadQuality(root.querySelector('.tr-q-body'), ctx),
    loadSystem(sysBody, ctx),
  ]);
  if (alive) timer = setInterval(() => loadSystem(sysBody, ctx), 5000);
}

export function dispose() {
  alive = false;
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

async function loadReconcile(body, label, ctx) {
  let last = null;
  let error = null;
  try { last = await ctx.get('/api/reconcile'); } catch (err) { error = err; }
  if (!alive) return;
  if (!last || last.available === false) {
    body.innerHTML = `<div class="tr-empty">
      <p>尚无对账结果。</p>
      <p class="muted">从发生器取回回执（<code>GET :9001/receipt</code>）后，用右上角按钮上传；或者运行 <code>scripts/reconcile.py</code>。${error ? `（读取上次结果失败：${esc(error.message || error)}）` : ''}</p>
    </div>`;
    label.textContent = '上传回执 JSON';
    return;
  }
  renderReconcile(body, last);
  label.textContent = '重新上传回执';
}

function renderReconcile(body, r) {
  const checks = Array.isArray(r.checks) ? r.checks : [];
  const counts = { pass: 0, fail: 0, warn: 0, unknown: 0 };
  checks.forEach((c) => { counts[STATUS[c.status] ? c.status : 'unknown']++; });
  const ok = r.ok
    ? '<span class="tr-badge tr-pass">✓ 对账通过</span>'
    : '<span class="tr-badge tr-fail">✗ 存在未通过项</span>';
  const complete = r.complete
    ? '<span class="tr-badge tr-pass">✓ 全部可验证</span>'
    : '<span class="tr-badge tr-unknown">? 部分项无法验证</span>';
  const meta = [
    r.run ? `轮次 <b>${esc(r.run)}</b>` : '',
    r.checked_at ? `对账时间 ${clock(r.checked_at)}` : '',
    r.confirmed_batches != null ? `回执确认批次 ${num(r.confirmed_batches)}` : '',
    r.stored_main_batches != null ? `本地主批次 ${num(r.stored_main_batches)}` : '',
  ].filter(Boolean).join(' · ');
  const limits = Array.isArray(r.limits) && r.limits.length
    ? `<div class="tr-limits"><h3>说明与局限</h3><ul>${r.limits.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div>`
    : '';

  body.innerHTML = `
    <div class="tr-summary">${ok}${complete}
      <span class="tr-counts">✓ ${counts.pass} 通过 · ✗ ${counts.fail} 未通过 · ! ${counts.warn} 注意 · ? ${counts.unknown} 无法验证</span>
    </div>
    ${meta ? `<p class="muted tr-meta">${meta}</p>` : ''}
    <ol class="tr-checks">${checks.map(checkRow).join('')}</ol>
    <div class="tr-batches"></div>
    ${idList('已确认但缺失的批次', r.missing_batch_ids)}
    ${idList('已入库但发生器未确认的批次', r.unconfirmed_batch_ids)}
    ${limits}`;

  const box = body.querySelector('.tr-batches');
  if (r.batches && typeof r.batches.states === 'string') drawBatches(box, r.batches.states);
  else box.innerHTML = '<p class="muted tr-note">后端尚未提供批次状态（契约 R4），暂不绘制批次像素条。</p>';
}

function checkRow(c) {
  const st = STATUS[c.status] || STATUS.unknown;
  const hasVals = c.expected !== undefined || c.actual !== undefined;
  const note = c.note || c.detail;
  return `<li class="tr-check ${st.cls}">
    <span class="tr-icon" aria-hidden="true">${st.icon}</span>
    <span class="tr-st">${st.label}</span>
    <span class="tr-name">${esc(c.name ?? c.label ?? c.id ?? '')}</span>
    <span class="tr-vals tnum">${hasVals ? `${val(c.expected)} <span class="muted">→</span> ${val(c.actual)}` : ''}</span>
    ${note ? `<span class="tr-cnote muted">${esc(note)}</span>` : ''}
  </li>`;
}

function idList(title, ids) {
  if (!Array.isArray(ids) || !ids.length) return '';
  const shown = ids.slice(0, 200).map((x) => esc(x)).join(', ');
  return `<div class="tr-ids"><h3>${title}（${num(ids.length)}）</h3><p class="tnum">${shown}${ids.length > 200 ? ' …' : ''}</p></div>`;
}

function drawBatches(box, states) {
  const counts = {};
  for (const ch of states) counts[ch] = (counts[ch] || 0) + 1;
  let last = states.length - 1;
  while (last >= 0 && states[last] === '-') last--;
  const shown = last + 1;
  const omitted = states.length - shown;

  box.innerHTML = `
    <h3>批次状态（每格一个批次编号，共 ${num(states.length)} 个）</h3>
    <div class="tr-strip-wrap"><canvas class="tr-strip"></canvas></div>
    <p class="muted tr-hover">&nbsp;</p>
    <ul class="tr-legend">${BATCH_STATES.map((s) => `<li><span class="tr-swatch" style="background:${s.color}"></span><span class="tr-licon">${s.icon}</span>${s.label} <b class="tnum">${num(counts[s.ch] || 0)}</b></li>`).join('')}</ul>
    ${omitted ? `<p class="muted tr-note">末尾 ${num(omitted)} 个未发送的批次编号没有绘制。</p>` : ''}`;
  if (!shown) return;

  const canvas = box.querySelector('canvas');
  const hover = box.querySelector('.tr-hover');
  const css = getComputedStyle(document.documentElement);
  const palette = Object.fromEntries(BATCH_STATES.map((s) => [s.ch, css.getPropertyValue(s.css).trim() || s.fb]));
  const cell = shown > 5000 ? 4 : shown > 1500 ? 6 : 9;
  const step = cell + 1;
  const width = Math.max(200, box.clientWidth || 800);
  const cols = Math.max(1, Math.floor(width / step));
  const rows = Math.ceil(shown / cols);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = cols * step * dpr;
  canvas.height = rows * step * dpr;
  canvas.style.width = `${cols * step}px`;
  canvas.style.height = `${rows * step}px`;
  const g = canvas.getContext('2d');
  g.scale(dpr, dpr);
  for (let i = 0; i < shown; i++) {
    g.fillStyle = palette[states[i]] || palette['-'];
    g.fillRect((i % cols) * step, Math.floor(i / cols) * step, cell, cell);
  }
  const labels = Object.fromEntries(BATCH_STATES.map((s) => [s.ch, `${s.icon} ${s.label}`]));
  canvas.addEventListener('mousemove', (e) => {
    const rect = canvas.getBoundingClientRect();
    const i = Math.floor((e.clientY - rect.top) / step) * cols + Math.floor((e.clientX - rect.left) / step);
    hover.textContent = i >= 0 && i < shown ? `批次 ${i}：${labels[states[i]] || states[i]}` : ' ';
  });
  canvas.addEventListener('mouseleave', () => { hover.innerHTML = '&nbsp;'; });
}

async function loadQuality(body, ctx) {
  let q = null;
  try {
    q = await ctx.get('/api/quality');
  } catch (err) {
    if (alive) body.innerHTML = `<p class="tr-error">✗ 读取失败：${esc(err && err.message ? err.message : err)}</p>`;
    return;
  }
  if (!alive) return;
  if (!q) {
    body.innerHTML = '<p class="muted">暂无数据。</p>';
    return;
  }
  const rows = Array.isArray(q.rejected) ? q.rejected : [];
  const table = rows.length
    ? `<table class="tr-table">
        <thead><tr><th>时间</th><th>原因</th><th class="tr-r">batch_id</th><th class="tr-r">次数</th></tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td class="tnum">${clock(r.created)}</td>
          <td>${esc(r.reason)}${r.excerpt ? `<details class="tr-excerpt"><summary>查看原文片段</summary><pre>${esc(r.excerpt)}</pre></details>` : ''}</td>
          <td class="tr-r tnum">${r.batch_id == null ? '—' : esc(r.batch_id)}</td>
          <td class="tr-r tnum">${num(r.attempts)}</td>
        </tr>`).join('')}</tbody>
      </table>${rows.length >= 50 ? '<p class="muted tr-note">只显示最近 50 条。</p>' : ''}`
    : '<p class="muted">没有被拒收的消息。</p>';
  body.innerHTML = `
    <div class="tr-qstats">
      <div><span class="tr-big tnum">${num(q.duplicates)}</span><span class="muted">次重复投递已吸收</span></div>
      <div><span class="tr-big tnum">${num(q.rejected_attempts)}</span><span class="muted">次请求被拒收并隔离</span></div>
    </div>
    ${table}
    ${q.policy ? `<p class="muted tr-note">策略：${esc(q.policy)}</p>` : ''}`;
}

async function loadSystem(body, ctx) {
  let s = null;
  try {
    s = await ctx.get('/api/system');
  } catch (err) {
    if (alive) body.innerHTML = `<p class="tr-error">✗ 读取失败：${esc(err && err.message ? err.message : err)}</p>`;
    return;
  }
  if (!alive) return;
  if (!s) {
    body.innerHTML = '<p class="muted">后端没有提供系统指标。</p>';
    return;
  }
  const memRatio = s.memory_bytes != null && s.memory_limit_bytes ? s.memory_bytes / s.memory_limit_bytes : null;
  const cpuCap = s.cpu_limit != null ? s.cpu_limit * 100 : null;
  const cpuRatio = typeof s.cpu_percent === 'number' && cpuCap ? s.cpu_percent / cpuCap : null;
  const budget = s.disk_budget_bytes;
  const diskRatio = s.data_bytes != null && budget ? s.data_bytes / budget : null;
  const diskLevel = s.data_bytes == null
    ? null
    : s.disk_stop_bytes != null && s.data_bytes >= s.disk_stop_bytes
      ? 'critical'
      : s.disk_warn_bytes != null && s.data_bytes >= s.disk_warn_bytes ? 'warning' : 'normal';
  const ticks = budget
    ? [
      s.disk_warn_bytes != null && { at: s.disk_warn_bytes / budget, label: `告警 ${bytes(s.disk_warn_bytes)}（${pct(s.disk_warn_bytes / budget)}）` },
      s.disk_stop_bytes != null && { at: s.disk_stop_bytes / budget, label: `停止写入 ${bytes(s.disk_stop_bytes)}（${pct(s.disk_stop_bytes / budget)}）` },
    ].filter(Boolean)
    : [];

  body.innerHTML = [
    meter('内存', memRatio,
      s.memory_bytes != null ? `${bytes(s.memory_bytes)} / ${s.memory_limit_bytes ? bytes(s.memory_limit_bytes) : '上限未知'}` : '暂无数据',
      level(memRatio)),
    meter('CPU', cpuRatio,
      typeof s.cpu_percent === 'number'
        ? `${s.cpu_percent.toFixed(1)}% / 上限 ${s.cpu_limit != null ? `${s.cpu_limit} 核` : '未知'}（100% = 1 核）`
        : '暂无采样（需要两次采样后才有数值）',
      level(cpuRatio)),
    meter('磁盘', diskRatio,
      s.data_bytes != null ? `${bytes(s.data_bytes)} / 预算 ${budget ? bytes(budget) : '未知'}` : '暂无数据',
      diskLevel, ticks,
      diskLevel === 'critical' ? '已达停止写入水位' : diskLevel === 'warning' ? '超过告警水位' : ''),
    `<p class="tr-uptime">运行时长 <b>${duration(s.uptime_seconds)}</b> <span class="muted">· 每 5 秒刷新</span></p>`,
    s.metrics_source ? `<p class="muted tr-note">指标来源：${esc(s.metrics_source)}</p>` : '',
    s.disk_policy ? `<p class="muted tr-note">磁盘策略：${esc(s.disk_policy)}</p>` : '',
  ].join('');
}

function meter(name, ratio, text, lvl, ticks = [], alert = '') {
  const width = ratio == null ? 0 : Math.max(0, Math.min(1, ratio)) * 100;
  const message = alert || (lvl === 'critical' ? '接近上限' : lvl === 'warning' ? '偏高' : '');
  const icon = lvl === 'critical' ? '✗' : lvl === 'warning' ? '!' : '';
  return `<div class="tr-meter tr-${lvl || 'none'}">
    <div class="tr-mhead"><span class="tr-mname">${name}</span><span class="tr-mval tnum">${text}</span><span class="tr-mpct tnum">${pct(ratio)}</span></div>
    <div class="tr-track"><div class="tr-fill" style="width:${width}%"></div>${ticks.map((t) => `<span class="tr-tick" style="left:${Math.min(100, t.at * 100)}%" title="${esc(t.label)}"></span>`).join('')}</div>
    ${ticks.length ? `<p class="tr-ticks">刻度：${ticks.map((t) => esc(t.label)).join(' · ')}</p>` : ''}
    ${message ? `<p class="tr-alert">${icon} ${message}</p>` : ''}
  </div>`;
}
