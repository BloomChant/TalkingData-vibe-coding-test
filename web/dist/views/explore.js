// Explore: four abstraction lenses (taxonomy / kind / space / fields) over one shared filter state.
// Every lens is also a filter control; counts come from /api/facets (each facet ignores its own filter).
let root, ctx, st, timers = [];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const FRAME_KIND = { galactic: '星系坐标 · 光年', stellar_system: '恒星系 · 千米', local_surface: '地表 · 米', none: '未提供位置' };
const kindOf = (name) => String(name ?? '').replace(/\s*#\d+$/, '');

export async function render(el, c) {
  root = el; ctx = c;
  st = { f: { type_id: [], kind: [], frame: [], frame_kind: [], q: '' }, lens: 'tree', idx: null, facets: null,
         items: [], total: null, exact: true, cursor: null, sel: null, open: new Set(), openFrames: new Set(),
         frames: null, fields: null, seq: 0, active: true, moreLoading: false };
  const pending = JSON.parse(sessionStorage.getItem('observatory.open') || 'null');
  sessionStorage.removeItem('observatory.open');
  if (pending?.view === 'explore' && pending.params?.type_id) st.f.type_id = [String(pending.params.type_id)];
  root.innerHTML = `
    <div class="ex">
      <aside class="ex-col" id="ex-left">
        <div class="ex-lens-tabs">
          <button data-lens="tree" class="active">分类</button><button data-lens="kind">种类</button>
          <button data-lens="space">空间</button><button data-lens="fields">字段</button>
        </div>
        <div class="ex-lens" id="ex-lens"></div>
      </aside>
      <section class="ex-col ex-main" id="ex-mid">
        <div class="ex-filterbar"><input class="ex-search" id="ex-q" placeholder="按名称搜索，例如：外星晶体生命" /><span id="ex-chips"></span></div>
        <div class="ex-summary"><span class="ex-total tnum" id="ex-total">—</span><span class="muted" id="ex-total-note"></span></div>
        <div id="ex-scatter"></div>
        <table class="ex-table"><thead><tr><th>名称</th><th>种类 <span class="derived">派生</span></th><th>分类</th><th>参考系</th></tr></thead><tbody id="ex-rows"></tbody></table>
        <div id="ex-more-wrap"></div>
      </section>
      <aside class="ex-col" id="ex-right"><div class="ex-detail" id="ex-detail"><div class="ex-empty">点击左侧任一视角或结果中的对象，<br/>逐步收窄这个世界。</div></div></aside>
    </div><div class="ex-tip" id="ex-tip"></div>`;
  root.querySelectorAll('.ex-lens-tabs button').forEach((b) => b.addEventListener('click', () => {
    st.lens = b.dataset.lens;
    root.querySelectorAll('.ex-lens-tabs button').forEach((x) => x.classList.toggle('active', x === b));
    renderLens(); loadLensData();
  }));
  const q = root.querySelector('#ex-q');
  q.addEventListener('input', () => { st.f.q = q.value.trim(); refresh(); });
  root.querySelector('#ex-lens').addEventListener('click', onLensClick);
  root.querySelector('#ex-chips').addEventListener('click', onChipClick);
  root.querySelector('#ex-rows').addEventListener('click', (e) => { const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id); });
  const owner = st;
  await loadTree();
  if (current(owner)) refresh(0);
}

export function dispose() { if (st) st.active = false; timers.forEach(clearTimeout); timers = []; }

const current = (owner, seq = owner.seq) => owner === st && owner.active && seq === owner.seq;
function showRetry(el, err, retry) {
  if (!el) return;
  el.innerHTML = `<div class="ex-empty">加载失败：${esc(err.message || err)} <button type="button">重试</button></div>`;
  el.querySelector('button').addEventListener('click', retry);
}

const params = (extra = {}) => ({ type_id: st.f.type_id, kind: st.f.kind, frame: st.f.frame, frame_kind: st.f.frame_kind, q: st.f.q, ...extra });

async function loadTree() {
  const owner = st;
  const t = await ctx.get('/api/tree');
  if (!current(owner)) return;
  const nodes = t?.nodes ?? [];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const kids = new Map();
  let rootId = null;
  for (const n of nodes) {
    if (n.parent === 0) rootId = n.id;
    if (!kids.has(n.parent)) kids.set(n.parent, []);
    kids.get(n.parent).push(n.id);
  }
  for (const arr of kids.values()) arr.sort((a, b) => byId.get(b).subtree_count - byId.get(a).subtree_count);
  const pathOf = (id) => {
    const out = []; let cur = byId.get(id), hop = 0;
    while (cur && hop++ < 64) { out.unshift(cur); if (cur.parent === 0) break; cur = byId.get(cur.parent); }
    return out;
  };
  st.idx = { byId, kids, rootId, pathOf, revision: t?.revision ?? 0 };
  if (rootId != null) st.open.add(rootId);
}

function refresh(delay = 250) {
  ++st.seq; // Invalidate pending pages immediately, including during the debounce.
  st.moreLoading = false;
  root.querySelector('#ex-more-wrap').innerHTML = '';
  timers.forEach(clearTimeout);
  timers = [setTimeout(load, delay)];
  root.querySelector('#ex-mid')?.classList.add('ex-busy');
  root.querySelector('#ex-left')?.classList.add('ex-busy');
  renderChips();
}

async function load() {
  const owner = st, seq = st.seq;
  try {
    const [facets, objs] = await Promise.all([
      ctx.get('/api/facets', params({ dims: 'type,kind,frame_kind,galaxy' })),
      ctx.get('/api/objects', params({ limit: 50 })),
    ]);
    if (!current(owner, seq)) return;
    st.facets = facets;
    st.items = objs?.items ?? [];
    st.cursor = objs?.next_cursor ?? null;
    st.total = objs?.total ?? null;
    st.exact = objs?.total_exact !== false;
    renderSummary(); renderRows(); renderLens(); renderScatter();
    loadLensData(seq);
  } catch (err) {
    if (current(owner, seq)) showRetry(root.querySelector('#ex-total-note'), err, () => refresh(0));
  } finally {
    if (current(owner, seq)) {
      root.querySelector('#ex-mid')?.classList.remove('ex-busy');
      root.querySelector('#ex-left')?.classList.remove('ex-busy');
    }
  }
}

async function loadLensData(seq = st.seq) {
  const owner = st, lens = st.lens;
  if (lens !== 'space' && lens !== 'fields') return;
  try {
    if (lens === 'fields') { st.fields = 'loading'; renderLens(); }
    const data = await ctx.get(lens === 'space' ? '/api/frames' : '/api/fields', params(lens === 'fields' ? { sample: 2000 } : {}));
    if (!current(owner, seq) || st.lens !== lens) return;
    if (!data) throw new Error('数据暂不可用');
    st[lens === 'space' ? 'frames' : 'fields'] = data;
    renderLens();
  } catch (err) {
    if (current(owner, seq) && st.lens === lens) showRetry(root.querySelector('#ex-lens'), err, () => loadLensData());
  }
}

function toggle(list, v) { const i = list.indexOf(v); if (i >= 0) list.splice(i, 1); else list.push(v); }

function onLensClick(e) {
  const twist = e.target.closest('[data-twist]');
  if (twist) { e.stopPropagation(); const id = Number(twist.dataset.twist); st.open.has(id) ? st.open.delete(id) : st.open.add(id); renderLens(); return; }
  const ftw = e.target.closest('[data-ftwist]');
  if (ftw) { e.stopPropagation(); const id = ftw.dataset.ftwist; st.openFrames.has(id) ? st.openFrames.delete(id) : st.openFrames.add(id); renderLens(); return; }
  const row = e.target.closest('[data-filter]');
  if (!row) return;
  const [key, value] = [row.dataset.filter, row.dataset.value];
  if (key === 'type_id' || key === 'frame') st.f[key] = st.f[key][0] === value ? [] : [value];
  else toggle(st.f[key], value);
  if (key === 'type_id' && value) st.open.add(Number(value));
  refresh();
}

function onChipClick(e) {
  const b = e.target.closest('button[data-key]');
  if (b) {
    const key = b.dataset.key;
    if (key === 'q') { st.f.q = ''; root.querySelector('#ex-q').value = ''; } else st.f[key] = st.f[key].filter((v) => v !== b.dataset.value);
    refresh();
  } else if (e.target.closest('.ex-clear')) {
    st.f = { type_id: [], kind: [], frame: [], frame_kind: [], q: '' }; root.querySelector('#ex-q').value = ''; refresh();
  }
}

function renderChips() {
  const chips = [];
  const name = (id) => st.idx?.byId.get(Number(id))?.name ?? `分类 ${id}`;
  st.f.type_id.forEach((v) => chips.push(['type_id', v, `分类：${name(v)}`]));
  st.f.kind.forEach((v) => chips.push(['kind', v, `种类：${v}`]));
  st.f.frame.forEach((v) => chips.push(['frame', v, `空间：${v}`]));
  st.f.frame_kind.forEach((v) => chips.push(['frame_kind', v, `坐标：${FRAME_KIND[v] ?? v}`]));
  if (st.f.q) chips.push(['q', st.f.q, `名称含「${st.f.q}」`]);
  root.querySelector('#ex-chips').innerHTML = chips.map(([k, v, label]) =>
    `<span class="ex-chip">${esc(label)}<button data-key="${k}" data-value="${esc(v)}" title="移除">×</button></span>`).join(' ')
    + (chips.length ? ' <button class="ex-clear">清空</button>' : '');
}

function renderSummary() {
  root.querySelector('#ex-total').textContent = st.total == null ? `${ctx.fmt.n(st.items.length)}+` : ctx.fmt.n(st.total);
  root.querySelector('#ex-total-note').textContent = `个对象${st.exact ? '' : '（估算）'}满足当前条件 · 分类树第 ${st.idx?.revision ?? '—'} 版`;
}

function rowHtml(o) {
  const path = st.idx?.pathOf(o.type_id) ?? [];
  const top = path[1]?.name ?? path[0]?.name ?? '未知分类';
  return `<tr data-id="${esc(o.id)}" class="${st.sel === o.id ? 'sel' : ''}">
    <td>${esc(o.name)}</td><td>${esc(kindOf(o.name))}</td>
    <td title="${esc(path.map((p) => p.name).join(' / '))}">${path.length > 2 ? `<span class="muted">${esc(top)} /</span> ` : ''}${esc(o.type_name ?? top)}</td>
    <td class="muted">${o.frame_id ? esc(o.frame_id) : '未提供位置'}</td></tr>`;
}

function renderRows() {
  const body = root.querySelector('#ex-rows');
  body.innerHTML = st.items.length ? st.items.map(rowHtml).join('') : `<tr><td colspan="4" class="ex-empty">没有满足条件的对象</td></tr>`;
  const wrap = root.querySelector('#ex-more-wrap');
  wrap.innerHTML = st.cursor != null ? '<button class="ex-more">加载更多</button>' : '';
  wrap.querySelector('button')?.addEventListener('click', async (e) => {
    if (st.moreLoading) return;
    const owner = st, seq = st.seq, button = e.currentTarget;
    st.moreLoading = true; button.disabled = true; button.textContent = '加载中…';
    try {
      const more = await ctx.get('/api/objects', params({ limit: 50, cursor: st.cursor }));
      if (!current(owner, seq)) return;
      if (!more) throw new Error('数据暂不可用');
      st.items.push(...(more.items ?? [])); st.cursor = more.next_cursor ?? null; renderRows();
    } catch (err) {
      if (current(owner, seq)) {
        button.textContent = `加载失败，点击重试：${err.message}`;
      }
    } finally {
      if (current(owner, seq)) { st.moreLoading = false; button.disabled = false; }
    }
  });
}

function rolledTypeCounts() {
  const { byId, kids, rootId } = st.idx;
  const direct = new Map();
  const buckets = st.facets?.facets?.type?.buckets;
  if (buckets) buckets.forEach((b) => direct.set(Number(b.value), b.count));
  else byId.forEach((n) => direct.set(n.id, n.count));
  const total = new Map();
  const walk = (id) => { let s = direct.get(id) ?? 0; for (const k of kids.get(id) ?? []) s += walk(k); total.set(id, s); return s; };
  if (rootId != null) walk(rootId);
  return total;
}

function barRow({ filter, value, label, count, max, sel, indent = 0, twist = '', extra = '' }) {
  return `<div class="ex-row ${sel ? 'sel' : ''} ${count ? '' : 'zero'}" data-filter="${filter}" data-value="${esc(value)}" style="padding-left:${8 + indent * 14}px">
    <span class="lbl">${twist}${esc(label)}${extra}</span><span class="cnt">${ctx.fmt.n(count)}</span>
    <span class="bar" style="left:${8 + indent * 14}px"><i style="width:${max ? Math.max(1, (count / max) * 100) : 0}%"></i></span></div>`;
}

function renderLens() {
  const el = root.querySelector('#ex-lens');
  if (!st.idx) { el.innerHTML = '<div class="ex-empty">加载中…</div>'; return; }
  if (st.lens === 'tree') {
    const counts = rolledTypeCounts();
    const max = counts.get(st.idx.rootId) || 1;
    const rows = [];
    const walk = (id, depth) => {
      const n = st.idx.byId.get(id); const kids = st.idx.kids.get(id) ?? []; const open = st.open.has(id);
      const twist = kids.length ? `<span class="ex-twist" data-twist="${id}">${open ? '▾' : '▸'}</span>` : '<span class="ex-twist"></span>';
      rows.push(barRow({ filter: 'type_id', value: String(id), label: n.name, count: counts.get(id) ?? 0, max,
        sel: st.f.type_id.includes(String(id)), indent: depth, twist }));
      if (open) kids.forEach((k) => walk(k, depth + 1));
    };
    if (st.idx.rootId != null) walk(st.idx.rootId, 0);
    el.innerHTML = `<div class="ex-lens-note">分类树原样来自发生器（第 ${st.idx.revision} 版）。计数含子类，并受其他过滤条件影响。</div>${rows.join('')}`;
  } else if (st.lens === 'kind') {
    const b = st.facets?.facets?.kind;
    el.innerHTML = `<div class="ex-lens-note">种类由对象名称派生（去掉末尾的 #编号），与分类树相互独立：同一种类可能分布在不同分类下。</div>`
      + (b ? `<div class="ex-kinds">${b.buckets.map((x) => `<button class="ex-chip-btn ${st.f.kind.includes(x.value) ? 'sel' : ''}" data-filter="kind" data-value="${esc(x.value)}">${esc(x.label)}<b>${ctx.fmt.n(x.count)}</b></button>`).join('')}</div>${b.other ? `<div class="ex-lens-note">其余 ${ctx.fmt.n(b.other)} 个对象属于更少见的种类</div>` : ''}` : '<div class="ex-empty">后端未提供种类计数</div>');
  } else if (st.lens === 'space') {
    const fk = st.facets?.facets?.frame_kind?.buckets ?? [];
    const fkMax = Math.max(1, ...fk.map((x) => x.count));
    const head = `<div class="ex-lens-note">位置按参考系分开：不同参考系单位不同，不画在同一张坐标图上。frame_id 本身是包含路径：星系 / 恒星系 / 世界。</div>`
      + `<div class="ex-group-title">坐标类型</div>` + fk.map((x) => barRow({ filter: 'frame_kind', value: x.value, label: FRAME_KIND[x.value] ?? x.label, count: x.count, max: fkMax, sel: st.f.frame_kind.includes(x.value) })).join('');
    el.innerHTML = head + `<div class="ex-group-title">空间层级</div>` + frameTreeHtml();
  } else {
    el.innerHTML = fieldsHtml();
  }
}

function frameTreeHtml() {
  const data = st.frames;
  if (!data) return '<div class="ex-empty">加载中…</div>';
  const nodes = new Map();
  const ensure = (id) => { if (!nodes.has(id)) nodes.set(id, { id, own: 0, total: 0, kids: new Set(), unit: null }); return nodes.get(id); };
  for (const fr of data.frames ?? []) {
    const parts = fr.frame_id.split('/');
    for (let i = 1; i <= parts.length; i++) {
      const id = parts.slice(0, i).join('/'); const n = ensure(id); n.total += fr.count;
      if (i < parts.length) n.kids.add(parts.slice(0, i + 1).join('/'));
      else { n.own += fr.count; n.unit = fr.unit; }
    }
  }
  const tops = [...nodes.values()].filter((n) => !n.id.includes('/')).sort((a, b) => b.total - a.total);
  const max = Math.max(1, ...tops.map((n) => n.total));
  const rows = [];
  const walk = (n, depth) => {
    const kids = [...n.kids].map((k) => nodes.get(k)).sort((a, b) => b.total - a.total);
    const open = st.openFrames.has(n.id);
    const twist = kids.length ? `<span class="ex-twist" data-ftwist="${esc(n.id)}">${open ? '▾' : '▸'}</span>` : '<span class="ex-twist"></span>';
    const label = n.id.split('/').pop();
    rows.push(barRow({ filter: 'frame', value: n.id, label, count: n.total, max, sel: st.f.frame.includes(n.id), indent: depth, twist,
      extra: n.unit && n.own ? ` <span class="muted">· ${esc(n.unit)}</span>` : '' }));
    if (open) kids.forEach((k) => walk(k, depth + 1));
  };
  tops.forEach((n) => walk(n, 0));
  return rows.join('') + (data.truncated ? `<div class="ex-lens-note">参考系较多，只显示前 ${ctx.fmt.n(data.limit)} 个</div>` : '');
}

function fieldsHtml() {
  const d = st.fields;
  if (!d || d === 'loading') return '<div class="ex-empty">正在统计字段…</div>';
  const s = d.sample ?? {};
  const note = `<div class="ex-lens-note">在满足条件的最新 ${ctx.fmt.n(s.size)} 个对象上统计（共 ${ctx.fmt.n(s.matched)} 个）。这是有界抽样：比例代表样本，不是全量普查。${d.truncated ? ' 已触及处理上限。' : ''}</div>`;
  return note + (d.fields ?? []).map((f) => {
    const total = Object.values(f.types ?? {}).reduce((a, b) => a + b, 0) || 1;
    const types = Object.entries(f.types ?? {}).sort((a, b) => b[1] - a[1]).map(([t, n]) => `<span class="ty">${t} ${Math.round((n / total) * 100)}%</span>`).join('');
    const ex = (f.examples ?? []).map((x) => esc(JSON.stringify(x))).join('、');
    return `<div class="ex-field"><div class="p">${esc(f.path)}</div>
      <div class="meta"><span class="tnum">覆盖 ${(f.coverage * 100).toFixed(1)}%</span>${types}${f.mixed ? '<span class="mixed">⚠ 类型混合</span>' : ''}</div>
      ${ex ? `<div class="meta">例：${ex}</div>` : ''}<div class="cov"><i style="width:${Math.max(1, f.coverage * 100)}%"></i></div></div>`;
  }).join('');
}

async function renderScatter() {
  const owner = st, seq = st.seq, wrap = root.querySelector('#ex-scatter');
  try { await drawScatter(owner, seq, wrap); }
  catch (err) { if (current(owner, seq)) showRetry(wrap, err, renderScatter); }
}

async function drawScatter(owner, seq, wrap) {
  const frame = st.f.frame.length === 1 ? st.f.frame[0] : null;
  if (!frame) { wrap.innerHTML = ''; return; }
  const frames = st.frames ?? await ctx.get('/api/frames', params());
  if (!current(owner, seq)) return;
  if (!frames) throw new Error('参考系暂不可用');
  const exact = (frames?.frames ?? []).find((f) => f.frame_id === frame);
  if (!exact) {
    wrap.innerHTML = `<div class="card ex-scatter muted">「${esc(frame)}」是上级空间；坐标只在单一参考系内可比，请展开到具体参考系查看分布。</div>`;
    return;
  }
  const pos = await ctx.get('/api/positions', params({ frame_id: exact.frame_id, unit: exact.unit, limit: 2000 }));
  if (!current(owner, seq)) return;
  if (!pos) throw new Error('位置数据暂不可用');
  const pts = pos?.points ?? [];
  wrap.innerHTML = `<div class="card ex-scatter"><div class="card-title">${esc(exact.frame_id)} · ${esc(FRAME_KIND[exact.frame_kind] ?? exact.frame_kind)} · ${ctx.fmt.n(pts.length)} 个点${pos?.sampled ? '（最新接收的样本）' : ''}</div><canvas></canvas></div>`;
  const canvas = wrap.querySelector('canvas');
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const g = canvas.getContext('2d'); g.scale(dpr, dpr);
  if (!pts.length) return;
  const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const pad = 28;
  const sx = (x) => pad + ((x - x0) / (x1 - x0 || 1)) * (w - pad * 2);
  const sy = (y) => h - pad - ((y - y0) / (y1 - y0 || 1)) * (h - pad * 2);
  g.strokeStyle = '#2c2c2a'; g.lineWidth = 1; g.strokeRect(pad, pad, w - pad * 2, h - pad * 2);
  g.fillStyle = '#898781'; g.font = '11px system-ui';
  g.fillText(`${ctx.fmt.n(x0)} – ${ctx.fmt.n(x1)} ${exact.unit}（x）`, pad, h - 8);
  g.fillText(`y ${ctx.fmt.n(y0)} – ${ctx.fmt.n(y1)}`, pad, 16);
  const screen = pts.map((p) => ({ p, x: sx(p.x), y: sy(p.y) }));
  g.fillStyle = 'rgba(57,135,229,0.85)';
  screen.forEach(({ x, y }) => { g.beginPath(); g.arc(x, y, 3, 0, Math.PI * 2); g.fill(); });
  const tip = root.querySelector('#ex-tip');
  const nearest = (e) => {
    const r = canvas.getBoundingClientRect(); const mx = e.clientX - r.left, my = e.clientY - r.top;
    let best = null, bd = 144;
    for (const s of screen) { const d = (s.x - mx) ** 2 + (s.y - my) ** 2; if (d < bd) { bd = d; best = s; } }
    return best;
  };
  canvas.onmousemove = (e) => {
    const s = nearest(e);
    if (!s) { tip.style.display = 'none'; return; }
    tip.style.display = 'block'; tip.style.left = `${e.clientX + 12}px`; tip.style.top = `${e.clientY + 12}px`;
    tip.innerHTML = `${esc(s.p.name)}<br/><span class="muted">x ${ctx.fmt.n(s.p.x)} · y ${ctx.fmt.n(s.p.y)} · z ${ctx.fmt.n(s.p.z)} ${esc(exact.unit)}</span>`;
  };
  canvas.onmouseleave = () => { tip.style.display = 'none'; };
  canvas.onclick = (e) => { const s = nearest(e); if (s) select(s.p.id); };
}

const isScalar = (v) => v === null || typeof v !== 'object';
const typeName = (v) => (v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v);
const scalar = (v) => (v === null ? '<span class="v null">null</span>' : `<span class="v">${esc(typeof v === 'string' ? v : JSON.stringify(v))}</span>`);
function attrHtml(v) {
  if (Array.isArray(v)) {
    if (v.every(isScalar)) return `<span class="v">[${v.map((x) => esc(JSON.stringify(x))).join(', ')}]</span>`;
    return `<ul>${v.map((x, i) => `<li><span class="k">[${i}]</span> ${isScalar(x) ? scalar(x) : attrHtml(x)}</li>`).join('')}</ul>`;
  }
  if (v && typeof v === 'object') {
    return `<ul>${Object.entries(v).map(([k, x]) => `<li><span class="k">${esc(k)}</span><span class="t">${typeName(x)}</span> ${isScalar(x) ? scalar(x) : attrHtml(x)}</li>`).join('')}</ul>`;
  }
  return scalar(v);
}

async function select(id) {
  const owner = st;
  st.sel = id;
  root.querySelectorAll('#ex-rows tr').forEach((tr) => tr.classList.toggle('sel', tr.dataset.id === id));
  const el = root.querySelector('#ex-detail');
  el.innerHTML = '<div class="ex-empty">加载中…</div>';
  let d;
  try { d = await ctx.get(`/api/objects/${encodeURIComponent(id)}`); }
  catch (err) {
    if (current(owner) && st.sel === id) showRetry(el, err, () => select(id));
    return;
  }
  if (!current(owner) || st.sel !== id) return;
  if (!d) { el.innerHTML = '<div class="ex-empty">对象不存在</div>'; return; }
  const o = d.object;
  const path = st.idx?.pathOf(o.type_id) ?? [];
  const p = o.position;
  el.innerHTML = `
    <h2>${esc(o.name)}</h2>
    <div class="ex-path">${path.map((n) => esc(n.name)).join(' <span class="muted">/</span> ') || esc(d.type_name ?? '未知分类')}</div>
    <dl class="ex-kv">
      <dt>种类</dt><dd>${esc(kindOf(o.name))} <span class="derived">派生</span></dd>
      <dt>ID</dt><dd class="tnum">${esc(o.id)}</dd>
      <dt>生成序号</dt><dd class="tnum">${ctx.fmt.n(o.sequence)}</dd>
      <dt>生成时间</dt><dd class="tnum">${new Date(o.timestamp_ms).toLocaleString('zh-CN', { hour12: false })}</dd>
      <dt>分类版本</dt><dd>随第 ${esc(d.tree_revision)} 版分类到达</dd>
      <dt>位置</dt><dd>${p ? `${esc(p.frame_id)}<br/><span class="muted">${esc(FRAME_KIND[p.frame_kind] ?? p.frame_kind)}</span><br/><span class="tnum">x ${ctx.fmt.n(p.x)} · y ${ctx.fmt.n(p.y)} · z ${ctx.fmt.n(p.z)} ${esc(p.unit)}</span>` : '<span class="muted">未提供位置</span>'}</dd>
    </dl>
    <div class="card-title">属性（${Object.keys(o.attributes ?? {}).length} 个顶层字段，原样展示）</div>
    <div class="ex-attr">${attrHtml(o.attributes ?? {})}</div>
    <details class="ex-raw"><summary>原始 JSON</summary><pre>${esc(JSON.stringify(o, null, 2))}</pre></details>`;
}
