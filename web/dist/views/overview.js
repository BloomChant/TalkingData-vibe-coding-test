// 总览：KPI、入库节奏（对象数 / 延迟，两张图共用时间轴）、顶层类目构成、世界动态、世界日志。
let timer = null;
let alive = false;

export function dispose() {
  alive = false;
  if (timer) { clearTimeout(timer); timer = null; }
}

export async function render(root, ctx) {
  dispose();
  alive = true;
  root.innerHTML = skeleton();
  const els = {
    status: root.querySelector('.ov-status'),
    kpis: root.querySelector('.ov-kpis'),
    rate: root.querySelector('.ov-chart-rate'),
    lat: root.querySelector('.ov-chart-lat'),
    cats: root.querySelector('.ov-cats'),
    events: root.querySelector('.ov-events'),
    log: root.querySelector('.ov-log'),
  };
  const tick = async () => {
    if (!alive) return;
    let active = false;
    try {
      const [ov, tree, ev] = await Promise.all([
        ctx.get('/api/overview'),
        ctx.get('/api/tree'),
        ctx.get('/api/events', { limit: 30 }),
      ]);
      if (!alive) return;
      active = !!(ov && ov.active);
      draw(els, ctx, ov, tree, ev);
      els.status.textContent = '';
    } catch (err) {
      if (!alive) return;
      els.status.textContent = '数据暂不可用：' + (err && err.message ? err.message : String(err));
    }
    if (alive) timer = setTimeout(tick, active ? 3000 : 10000);
  };
  await tick();
}

function skeleton() {
  return `
  <div class="ov">
    <div class="ov-status"></div>
    <section class="ov-kpis"></section>
    <section class="ov-row">
      <div class="card ov-charts">
        <div class="card-title">入库节奏 <span class="muted">最近 60 秒 · 按服务端入库时间</span></div>
        <div class="ov-chart ov-chart-rate"></div>
        <div class="ov-chart ov-chart-lat"></div>
      </div>
      <div class="card">
        <div class="card-title">顶层类目构成 <span class="muted">含子树 · 点击到探索页过滤</span></div>
        <div class="ov-cats"></div>
      </div>
    </section>
    <section class="ov-row">
      <div class="card">
        <div class="card-title">世界日志 <span class="muted">由实时统计生成</span></div>
        <p class="ov-log"></p>
      </div>
      <div class="card">
        <div class="card-title">世界动态 <span class="muted">真实事件，最新在前</span></div>
        <ol class="ov-events"></ol>
      </div>
    </section>
  </div>`;
}

function draw(els, ctx, ov, tree, ev) {
  const f = ctx.fmt;
  if (!ov || !ov.run) {
    els.kpis.innerHTML = '<div class="card ov-empty">尚未收到任何数据。启动发生器后，这里会实时更新。</div>';
    for (const k of ['rate', 'lat', 'cats', 'events']) els[k].innerHTML = '';
    els.log.textContent = '';
    return;
  }
  const run = ov.run;
  const objects = num(run.objects);
  const positioned = num(ov.positioned);
  const unplaced = Math.max(0, objects - positioned);
  const nodes = (tree && tree.nodes) || [];
  const revision = num(tree && tree.revision != null ? tree.revision : run.revision);
  const rootNode = nodes.find((n) => n.parent === 0);
  const tops = rootNode ? nodes.filter((n) => n.parent === rootNode.id) : [];

  const kpis = [
    { label: '世界对象', value: f.n(objects), hero: true, sub: `${f.n(num(run.batches))} 个对象批次已入库` },
    { label: '分类节点', value: f.n(num(ov.types)), sub: `分类树第 ${f.n(revision)} 版` },
    ov.active
      ? { label: '当前入库速率', value: f.n(Math.round(num(ov.rate))), unit: '对象/秒', sub: '近 5 秒均值' }
      : { label: '当前入库速率', value: '空闲', sub: run.last_seen ? `最后入库 ${f.ago(run.last_seen)}` : '—' },
    { label: '吸收重复', value: f.n(num(run.duplicates)), sub: '同键同内容，未重复写入' },
    { label: '拒收', value: f.n(num(run.rejected)), sub: '结构不合法，整条隔离' },
    { label: '已定位 / 未提供位置', value: `${f.n(positioned)} / ${f.n(unplaced)}`, sub: objects ? `${pct(positioned, objects)} 带坐标` : '—' },
  ];
  els.kpis.innerHTML = kpis.map((k) => `
    <div class="card ov-kpi${k.hero ? ' ov-kpi-hero' : ''}">
      <div class="ov-kpi-label">${esc(k.label)}</div>
      <div class="ov-kpi-value">${esc(k.value)}${k.unit ? `<span class="ov-kpi-unit">${esc(k.unit)}</span>` : ''}</div>
      <div class="ov-kpi-sub muted">${esc(k.sub)}</div>
    </div>`).join('');

  drawCharts(els, ctx, ov.series || []);
  drawCats(els.cats, ctx, rootNode, tops);
  drawEvents(els.events, ctx, ev);
  drawLog(els.log, ctx, ov, rootNode, tops, nodes, revision, positioned, unplaced);
}

// ---------- 入库节奏：两张独立 Y 轴的小图，共用 60 秒时间轴，联动十字线 ----------
function drawCharts(els, ctx, series) {
  const f = ctx.fmt;
  const pts = series.map((s) => ({
    t: num(s.second),
    objects: num(s.objects),
    batches: num(s.batches),
    dup: num(s.duplicates),
    rej: num(s.rejected),
    lat: num(s.latency_count) > 0 ? num(s.latency_sum) / num(s.latency_count) : null,
    latMax: num(s.latency_count) > 0 && s.latency_max != null ? num(s.latency_max) : null,
  }));
  const charts = [
    renderChart(els.rate, pts, {
      key: 'objects', kind: 'area', title: '每秒入库对象数', unit: '个/秒', cls: 'ov-accent', time: f.time,
      fmt: (v) => f.n(Math.round(v)),
      tip: (p) => `<b>${esc(f.time(p.t))}</b><br>入库 ${f.n(p.objects)} 个对象 · ${f.n(p.batches)} 批<br><span class="muted">重复 ${f.n(p.dup)} · 拒收 ${f.n(p.rej)}</span>`,
    }),
    renderChart(els.lat, pts, {
      key: 'lat', kind: 'line', title: '每秒平均处理延迟', unit: 'ms', cls: 'ov-muted', time: f.time,
      fmt: (v) => (v >= 10 ? Math.round(v).toString() : v.toFixed(1)),
      empty: '这段时间没有请求，暂无延迟数据',
      tip: (p) => p.lat == null
        ? `<b>${esc(f.time(p.t))}</b><br><span class="muted">该秒没有请求</span>`
        : `<b>${esc(f.time(p.t))}</b><br>平均 ${p.lat.toFixed(1)} ms${p.latMax != null ? ` · 最大 ${p.latMax.toFixed(1)} ms` : ''}`,
    }),
  ];
  const hover = (i) => charts.forEach((c) => c.show(i));
  charts.forEach((c) => c.bind(hover));
}

function renderChart(el, pts, o) {
  const W = Math.max(320, Math.floor(el.clientWidth || 560));
  const H = 128, L = 48, R = 14, T = 24, B = 22;
  const n = pts.length;
  const vals = pts.map((p) => p[o.key]).filter((v) => v != null);
  const max = niceMax(vals.length ? Math.max(...vals) : 0);
  const xi = (i) => L + (n > 1 ? (i * (W - L - R)) / (n - 1) : 0);
  const yv = (v) => T + (1 - v / max) * (H - T - B);

  let line = '';
  let open = false;
  pts.forEach((p, i) => {
    const v = p[o.key];
    if (v == null) { open = false; return; }
    line += `${open ? 'L' : 'M'}${xi(i).toFixed(1)},${yv(v).toFixed(1)}`;
    open = true;
  });
  let area = '';
  if (o.kind === 'area' && n) {
    area = `M${xi(0).toFixed(1)},${yv(0).toFixed(1)}` +
      pts.map((p, i) => `L${xi(i).toFixed(1)},${yv(p[o.key] ?? 0).toFixed(1)}`).join('') +
      `L${xi(n - 1).toFixed(1)},${yv(0).toFixed(1)}Z`;
  }
  const grid = [max / 2, max].map((t) =>
    `<line x1="${L}" x2="${W - R}" y1="${yv(t).toFixed(1)}" y2="${yv(t).toFixed(1)}" class="ov-grid"/>` +
    `<text x="${L - 6}" y="${(yv(t) + 3).toFixed(1)}" class="ov-tick" text-anchor="end">${esc(o.fmt(t))}</text>`).join('');
  const xTicks = n ? [0, Math.floor((n - 1) / 2), n - 1].map((i) =>
    `<text x="${xi(i).toFixed(1)}" y="${H - 6}" class="ov-tick" text-anchor="${i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle'}">${esc(o.time(pts[i].t))}</text>`).join('') : '';
  const emptyNote = !vals.length && o.empty
    ? `<text x="${(L + W - R) / 2}" y="${(T + H - B) / 2 + 4}" class="ov-tick" text-anchor="middle">${esc(o.empty)}</text>` : '';

  el.innerHTML = `
    <svg class="ov-svg ${o.cls}" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
      <text x="${L}" y="13" class="ov-chart-title">${esc(o.title)}<tspan class="ov-chart-unit" dx="6">${esc(o.unit)}</tspan></text>
      ${grid}
      <line x1="${L}" x2="${W - R}" y1="${yv(0).toFixed(1)}" y2="${yv(0).toFixed(1)}" class="ov-axis"/>
      <text x="${L - 6}" y="${(yv(0) + 3).toFixed(1)}" class="ov-tick" text-anchor="end">0</text>
      ${area ? `<path d="${area}" class="ov-area"/>` : ''}
      ${line ? `<path d="${line}" class="ov-line"/>` : ''}
      ${xTicks}${emptyNote}
      <g class="ov-cross" style="display:none">
        <line class="ov-cross-line" y1="${T}" y2="${H - B}"/>
        <circle class="ov-cross-dot" r="4"/>
      </g>
      <rect class="ov-hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}"/>
    </svg>
    <div class="ov-tip" style="display:none"></div>`;

  const svg = el.querySelector('svg');
  const cross = el.querySelector('.ov-cross');
  const crossLine = cross.querySelector('line');
  const dot = cross.querySelector('circle');
  const tip = el.querySelector('.ov-tip');
  return {
    show(i) {
      if (i == null || !n) { cross.style.display = 'none'; tip.style.display = 'none'; return; }
      const p = pts[i];
      const v = p[o.key];
      const x = xi(i);
      cross.style.display = '';
      crossLine.setAttribute('x1', x.toFixed(1));
      crossLine.setAttribute('x2', x.toFixed(1));
      if (v == null) dot.style.display = 'none';
      else { dot.style.display = ''; dot.setAttribute('cx', x.toFixed(1)); dot.setAttribute('cy', yv(v).toFixed(1)); }
      tip.innerHTML = o.tip(p);
      tip.style.display = '';
      const w = tip.offsetWidth;
      tip.style.left = `${x + 12 + w > W ? Math.max(0, x - 12 - w) : x + 12}px`;
      tip.style.top = `${T - 4}px`;
    },
    bind(onHover) {
      const hit = el.querySelector('.ov-hit');
      const index = (ev) => {
        const r = svg.getBoundingClientRect();
        const i = Math.round(((ev.clientX - r.left - L) / (W - L - R)) * (n - 1));
        return Math.max(0, Math.min(n - 1, i));
      };
      hit.addEventListener('mousemove', (ev) => onHover(index(ev)));
      hit.addEventListener('mouseleave', () => onHover(null));
    },
  };
}

// ---------- 顶层类目构成 ----------
function drawCats(el, ctx, rootNode, tops) {
  const f = ctx.fmt;
  const rows = tops.map((n) => ({ id: n.id, name: n.name, v: num(n.subtree_count), link: true }));
  if (rootNode && num(rootNode.count) > 0) rows.push({ id: rootNode.id, name: '万物（未细分）', v: num(rootNode.count), link: false });
  rows.sort((a, b) => b.v - a.v);
  if (!rows.length) { el.innerHTML = '<div class="muted">分类树尚未到达</div>'; return; }
  const total = rootNode ? num(rootNode.subtree_count) : rows.reduce((s, r) => s + r.v, 0);
  const max = Math.max(1, ...rows.map((r) => r.v));
  el.innerHTML = rows.map((r) => `
    <${r.link ? 'button' : 'div'} class="ov-cat${r.link ? '' : ' ov-cat-static'}" data-type="${r.id}"
      title="${r.link ? `在探索页按「${esc(r.name)}」过滤` : '直接挂在根节点上的对象'}">
      <span class="ov-cat-name">${esc(r.name)}</span>
      <span class="ov-cat-bar"><span style="width:${((r.v / max) * 100).toFixed(2)}%"></span></span>
      <span class="ov-cat-val tnum">${f.n(r.v)}</span>
      <span class="ov-cat-pct tnum muted">${pct(r.v, total)}</span>
    </${r.link ? 'button' : 'div'}>`).join('');
  if (ctx.openTab) {
    el.querySelectorAll('button.ov-cat').forEach((b) =>
      b.addEventListener('click', () => ctx.openTab('explore', { type_id: Number(b.dataset.type) })));
  }
}

// ---------- 世界动态 ----------
const EVENT_LABEL = { create: '新增分类', rename: '分类改名', move: '分类迁移' };
const EVENT_ICON = { create: '＋', rename: '✎', move: '⇄' };

function drawEvents(el, ctx, ev) {
  const items = (ev && ev.events) || [];
  el.innerHTML = items.length ? items.map((e) => `
    <li class="ov-ev">
      <span class="ov-ev-time tnum muted">${esc(ctx.fmt.time(e.created))}</span>
      <span class="ov-ev-icon" aria-hidden="true">${EVENT_ICON[e.kind] || '•'}</span>
      <span class="ov-ev-kind">${esc(EVENT_LABEL[e.kind] || e.kind || '事件')}</span>
      <span class="ov-ev-text">${esc(e.summary || '')}</span>
    </li>`).join('') : '<li class="muted ov-ev-none">暂无事件</li>';
}

// ---------- 世界日志 ----------
function drawLog(el, ctx, ov, rootNode, tops, nodes, revision, positioned, unplaced) {
  const f = ctx.fmt;
  const run = ov.run;
  const ops = ov.operations || {};
  const biggest = tops.slice().sort((a, b) => num(b.subtree_count) - num(a.subtree_count))[0];
  const total = rootNode ? num(rootNode.subtree_count) : num(run.objects);
  const parts = [
    `轮次 <b>${esc(run.id)}</b>：本轮已接收 <b>${f.n(num(run.objects))}</b> 个对象，分属 <b>${f.n(tops.length)}</b> 个顶层类目、<b>${f.n(nodes.length)}</b> 个分类节点`,
    biggest ? `其中「${esc(biggest.name)}」最多，占 ${pct(num(biggest.subtree_count), total)}` : '',
    `分类树演进到第 <b>${f.n(revision)}</b> 版（新增 ${f.n(num(ops.create))}、改名 ${f.n(num(ops.rename))}、迁移 ${f.n(num(ops.move))}）`,
    `拒收 <b>${f.n(num(run.rejected))}</b> 次结构不合法的投递，吸收 <b>${f.n(num(run.duplicates))}</b> 次重复投递`,
    `<b>${f.n(positioned)}</b> 个对象带坐标，<b>${f.n(unplaced)}</b> 个未提供位置`,
    ov.active ? `正在接收，近 5 秒约 <b>${f.n(Math.round(num(ov.rate)))}</b> 个/秒` : (run.last_seen ? `最后一次入库在 ${esc(f.ago(run.last_seen))}` : ''),
  ].filter(Boolean);
  el.innerHTML = parts.join('；') + '。';
}

// ---------- 工具 ----------
function num(x) {
  const v = Number(x);
  return Number.isFinite(v) ? v : 0;
}

function pct(part, whole) {
  return whole > 0 ? `${((part / whole) * 100).toFixed(1)}%` : '—';
}

function niceMax(v) {
  if (!(v > 0)) return 1;
  const mag = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * mag) return m * mag;
  return 10 * mag;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
