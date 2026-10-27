// One controller for the official generator; no configurable remote URL.
export function mountExperiments(root, { onStart, onTrust, fmt }) {
  root.innerHTML = `<div class="xp-heading"><div><strong>采集实验</strong><span class="muted">官方发生器 → 入库 → 自动对账</span></div><span id="xp-status" role="status">连接发生器…</span></div>
    <form id="xp-form"><fieldset id="xp-settings">
      <label>目标速率 <span>对象/秒</span><input name="rate" type="number" min="1" max="20000" step="1" value="200" required></label>
      <label>持续时间 <span>秒</span><input name="duration" type="number" min="5" max="600" step="1" value="60" required></label>
      <label>每批对象数<input name="batch_size" type="number" min="1" max="500" step="1" value="20" required></label>
      <label>并发工作数<input name="workers" type="number" min="1" max="8" step="1" value="4" required></label>
      <label>场景<select name="scenario"><option value="evolving">持续演化</option><option value="mixed">混合对象</option><option value="stress">压力测试</option></select></label>
      <label>速率模式<select name="profile"><option value="steady">稳定</option><option value="bursty">突发</option></select></label>
      <details class="xp-advanced"><summary>高级设置</summary><label>随机种子<input name="seed" type="number" min="0" max="2147483647" value="42" step="1" required></label></details>
    </fieldset><div class="xp-actions"><button id="xp-start" type="submit" disabled>开始采集</button><button id="xp-stop" type="button" hidden>停止采集</button><button id="xp-refresh" type="button">刷新状态</button><button id="xp-trust" type="button" hidden>查看对账</button></div></form>
    <div class="xp-progress"><progress id="xp-progress" max="100" value="0" aria-label="采集时间进度"></progress><span id="xp-elapsed">尚未开始</span></div>
    <div class="xp-metrics" id="xp-metrics"></div><div class="xp-message" id="xp-message" aria-live="polite"></div>`;
  const el = (id) => root.querySelector(`#xp-${id}`);
  let snapshot = null, action = false, polling = false, timer, uncertain = false, noticedRun = null;
  let message = '', actionAt = 0, generation = 0;
  const active = () => ['starting', 'running', 'stopping'].includes(snapshot?.experiment?.state);
  const statusNames = { starting: '正在启动', running: '采集中', stopping: '停止中 · 正在收尾', completed: '采集完成', failed: '实验失败' };
  async function request(path, body) {
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(20000) });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const error = new Error(typeof data?.detail === 'string' ? data.detail : `HTTP ${response.status}`);
      error.status = response.status; throw error;
    }
    if (!data) throw new Error('服务返回了无法解析的响应');
    return data;
  }
  function paint() {
    const exp = snapshot?.experiment;
    const g = snapshot?.generator;
    const stats = exp?.receipt?.stats ?? (exp?.test_id && g?.config?.test_id === exp.test_id ? g.stats : null);
    const running = active();
    const external = !running && g?.state === 'running';
    el('settings').disabled = action || running || uncertain || external;
    el('start').disabled = action || running || uncertain || external || snapshot?.reachable !== true;
    el('start').textContent = action && !running ? '正在请求…' : '开始采集';
    el('stop').hidden = !running;
    el('stop').disabled = action || exp?.state === 'stopping';
    el('refresh').disabled = polling || action;
    el('trust').hidden = !exp?.reconciliation;
    el('status').textContent = snapshot?.reachable === false ? '发生器不可达' : uncertain ? '启动结果待确认' : external ? '发生器正在执行其他实验' : exp ? statusNames[exp.state] ?? exp.state : '就绪';
    const duration = exp?.config?.duration ?? exp?.config?.duration_seconds;
    const created = typeof exp?.created_at === 'number' ? exp.created_at * 1000 : Date.parse(exp?.created_at);
    const elapsed = stats?.elapsed_seconds ?? (running && Number.isFinite(created) ? Math.max(0, (Date.now() - created) / 1000) : null);
    el('elapsed').textContent = exp ? `${exp.test_id} · 已运行 ${elapsed == null ? '—' : Math.floor(elapsed)} 秒${duration ? ` / ${duration} 秒` : ''}` : '尚未开始';
    el('progress').value = duration && elapsed != null ? Math.min(100, elapsed / duration * 100) : 0;
    el('metrics').textContent = stats ? `已发送 ${fmt.n(stats.attempted_objects)} 个对象 / ${fmt.n(stats.attempted_batches)} 批 · 已确认 ${fmt.n(stats.acknowledged_objects)} 个对象 / ${fmt.n(stats.acknowledged_batches)} 批` : '发送与确认计数将在开始后显示';
    let reconciliation = '';
    if (exp?.reconciliation) {
      const checks = exp.reconciliation.checks ?? [];
      const count = (status) => checks.filter(c => c.status === status).length;
      reconciliation = `自动对账：${count('pass')} 项通过 · ${count('fail')} 项失败 · ${count('unknown')} 项无法验证`;
    } else if (exp && ['stopping', 'completed'].includes(exp.state)) reconciliation = '自动对账：等待收尾结果';
    el('message').textContent = [snapshot?.reachable === false ? '请先启动官方发生器（compose.generator.yaml），再刷新状态。' : '', message, exp?.error, snapshot?.error, reconciliation].filter(Boolean).join(' · ');
  }
  async function poll(manual = false) {
    clearTimeout(timer);
    if (polling || action) { timer = setTimeout(poll, 2000); return; }
    const seq = generation;
    polling = true; paint();
    try {
      const next = await request('/api/experiment');
      if (seq !== generation) return;
      snapshot = next;
      const exp = snapshot.experiment;
      if (active() && exp.test_id !== noticedRun) { noticedRun = exp.test_id; onStart(exp, false); }
      if (uncertain && exp && (active() || (typeof exp.created_at === 'number' ? exp.created_at * 1000 : Date.parse(exp.created_at)) >= actionAt)) {
        uncertain = false; message = '已从服务器确认实验状态。';
      } else if (manual && uncertain && snapshot.reachable && !active() && snapshot.generator?.state !== 'running') {
        uncertain = false; message = '当前未发现正在执行的实验，可以重新开始。';
      }
    } catch (error) {
      if (seq !== generation) return;
      snapshot = { ...snapshot, reachable: false, error: `状态查询失败：${error.message}` };
    } finally { polling = false; paint(); timer = setTimeout(poll, 2000); }
  }
  el('form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (action || active() || uncertain || snapshot?.reachable !== true || !el('form').reportValidity()) return;
    const data = Object.fromEntries(new FormData(el('form')));
    for (const name of ['rate', 'duration', 'batch_size', 'workers', 'seed']) data[name] = Number(data[name]);
    ++generation;
    action = true; actionAt = Date.now(); message = ''; paint();
    try {
      const exp = await request('/api/experiment/start', data);
      snapshot = { ...snapshot, experiment: exp }; noticedRun = exp.test_id;
      onStart(exp, true);
    } catch (error) {
      uncertain = !error.status || error.status >= 500;
      message = error.status === 409 ? '已有实验正在执行，请等待状态刷新后停止或继续观察。' : uncertain ? `启动结果尚未确认：${error.message}。正在查询状态，请勿重复启动。` : `启动失败：${error.message}`;
    } finally { action = false; paint(); poll(); }
  });
  el('stop').addEventListener('click', async () => {
    if (action || !active()) return;
    ++generation;
    action = true; message = ''; paint();
    try {
      await request('/api/experiment/stop', { test_id: snapshot.experiment.test_id });
      snapshot.experiment.state = 'stopping'; message = '已请求停止，正在等待发生器收尾与自动对账。';
    } catch (error) { message = `停止请求未确认：${error.message}。请查看刷新后的状态。`; }
    finally { action = false; paint(); poll(); }
  });
  el('refresh').addEventListener('click', () => poll(true));
  el('trust').addEventListener('click', () => onTrust(snapshot.experiment));
  poll();
}
