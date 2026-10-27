// Unit-level DOM stubs; no browser automation or external dependencies.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'web', 'dist', 'views', 'explore.js'), 'utf8').replace(/^export /gm, '');
const sandbox = { setTimeout: () => 1, clearTimeout: () => {} };
vm.createContext(sandbox);
vm.runInContext(source + `
globalThis.test = {
  init(r, c) { root = r; ctx = c; st = {active:true, seq:1, moreLoading:false, f:{type_id:[],kind:[],frame:[],frame_kind:[],q:''}, items:[],cursor:10, lens:'fields',idx:{byId:new Map(),kids:new Map(),rootId:null},open:new Set()}; },
  state:()=>st, renderRows, refresh, loadLensData, renderScatter, select
};`, sandbox);
const t = sandbox.test;
function element() {
  const button = { disabled: false, textContent: '', addEventListener(_, fn) { this.click = fn; } };
  return { innerHTML: '', button, querySelector: () => button, classList: { add() {}, remove() {} } };
}
const els = new Map();
const root = { querySelector(s) { if (!els.has(s)) els.set(s, element()); return els.get(s); }, querySelectorAll: () => [] };
const fmt = { n: String };
(async () => {
  let resolve, calls = 0;
  t.init(root, { fmt, get() { calls++; return new Promise(r => { resolve = r; }); } });
  t.renderRows();
  const button = root.querySelector('#ex-more-wrap').button;
  const pending = button.click({ currentTarget: button });
  await button.click({ currentTarget: button });
  assert.equal(calls, 1, 'double-click must issue one page request');
  assert.equal(button.disabled, true);
  t.refresh(0);
  t.state().items = [{ id: 'new-filter-result' }];
  resolve({ items: [{ id: 'stale-page' }], next_cursor: 5 });
  await pending;
  assert.deepEqual(t.state().items.map(x => x.id), ['new-filter-result'], 'stale page must not append');
  const failing = { fmt, get: async () => { throw new Error('HTTP 503'); } };
  t.init(root, failing);
  await t.loadLensData();
  assert.match(root.querySelector('#ex-lens').innerHTML, /加载失败.*重试/);
  t.state().lens = 'space';
  await t.loadLensData();
  assert.match(root.querySelector('#ex-lens').innerHTML, /HTTP 503/);
  t.state().f.frame = ['galaxy-0'];
  await t.renderScatter();
  assert.match(root.querySelector('#ex-scatter').innerHTML, /加载失败.*重试/);
  await t.select('object-1');
  assert.match(root.querySelector('#ex-detail').innerHTML, /加载失败.*重试/);
  console.log('PASS: pagination double-click + stale filter response; fields/space/scatter/detail error retry states');
})().catch(err => { console.error(err); process.exitCode = 1; });
