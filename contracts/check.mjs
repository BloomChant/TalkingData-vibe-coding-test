#!/usr/bin/env node
// Contract self-check: fetch every endpoint and compare its JSON structure with the fixture.
// usage: node contracts/check.mjs http://localhost:8000 [--run <id>]
// A fixture key missing from the response, or a changed value type, is an error.
// Extra keys are allowed (additive changes). Endpoints marked "pending" only warn until implemented.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(readFileSync(join(here, 'manifest.json'), 'utf8'));
const args = process.argv.slice(2);
const base = (args.find((a) => !a.startsWith('--')) ?? 'http://localhost:8000').replace(/\/$/, '');
const runArg = args.includes('--run') ? args[args.indexOf('--run') + 1] : null;

const kindOf = (v) => (v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v);
const toPattern = (p) => new RegExp('^' + p.replace(/[.]/g, '\\.').replace(/\[\]/g, '\\[\\]').replace(/\*/g, '[^.]+') + '$');
const freeform = manifest.freeform.map(toPattern);
const nullable = manifest.nullable.map(toPattern);
const matches = (list, path) => list.some((re) => re.test(path));

function compare(expected, actual, path, problems, pendingKeys) {
  if (expected === null) return; // fixture cannot tell the type; accept anything
  if (actual === null || actual === undefined) {
    if (!matches(nullable, path)) problems.push(`${path}: 应为 ${kindOf(expected)}，实际为 ${actual === null ? 'null' : '缺失'}`);
    return;
  }
  if (kindOf(expected) !== kindOf(actual)) {
    if (!matches(freeform, path)) problems.push(`${path}: 类型应为 ${kindOf(expected)}，实际为 ${kindOf(actual)}`);
    return;
  }
  if (matches(freeform, path)) return;
  if (Array.isArray(expected)) {
    if (!expected.length) return;
    actual.slice(0, 3).forEach((item) => compare(expected[0], item, `${path}[]`, problems, []));
    return;
  }
  if (kindOf(expected) === 'object') {
    for (const [key, value] of Object.entries(expected)) {
      const child = `${path}.${key}`;
      if (!(key in actual)) {
        if (pendingKeys.includes(key)) continue;
        if (!matches(nullable, child)) problems.push(`${child}: 缺失`);
        continue;
      }
      compare(value, actual[key], child, problems, []);
    }
  }
}

async function getJson(path) {
  const res = await fetch(base + path, { signal: AbortSignal.timeout(15000) });
  const text = await res.text();
  let body = null;
  try { body = JSON.parse(text); } catch { /* reported below */ }
  return { status: res.status, body, text };
}

const withRun = (path, run) => (run ? path + (path.includes('?') ? '&' : '?') + 'run=' + encodeURIComponent(run) : path);

let run = runArg;
const vars = {};
try {
  if (!run) {
    const { body } = await getJson('/api/runs');
    run = body?.runs?.find((r) => r.id !== '_unassigned' && r.objects > 0)?.id ?? null;
  }
  const objects = await getJson(withRun('/api/objects?limit=1', run));
  vars.object_id = objects.body?.items?.[0]?.id;
  const frames = await getJson(withRun('/api/frames', run));
  vars.frame_id = frames.body?.frames?.[0]?.frame_id;
  vars.unit = frames.body?.frames?.[0]?.unit;
} catch (err) {
  console.error(`无法连接 ${base}：${err.message}`);
  process.exit(2);
}

console.log(`契约 v${manifest.version} 自检  ${base}  run=${run ?? '(默认)'}\n`);
let errors = 0, pending = 0;
for (const ep of manifest.endpoints) {
  const fixture = JSON.parse(readFileSync(join(here, 'fixtures', ep.fixture), 'utf8'));
  let path = ep.path.replace(/\{(\w+)\}/g, (_, k) => encodeURIComponent(vars[k] ?? ''));
  if (ep.run_scoped !== false) path = withRun(path, run);
  const label = ep.name.padEnd(16);
  let res;
  try { res = await getJson(path); } catch (err) { res = { status: 0, body: null, text: err.message }; }
  if (res.status !== 200 || res.body === null) {
    if (ep.pending) { pending++; console.log(`…  ${label} PENDING（HTTP ${res.status}，🆕 尚未实现）`); continue; }
    errors++; console.log(`✗  ${label} HTTP ${res.status} ${res.body === null ? '（不是 JSON）' : ''} ${path}`); continue;
  }
  let problems = [];
  compare(fixture, res.body, ep.name, problems, ep.pending_keys ?? []);
  for (const rule of ep.assert_null ?? []) {
    // "items[].frame_id": every element of items must have frame_id === null (proves a filter took effect)
    const [arrayKey, field] = rule.split('[].');
    const bad = (res.body[arrayKey] ?? []).filter((item) => item?.[field] !== null).length;
    if (bad) problems.push(`${ep.name}.${rule}: ${bad} 条不为 null（过滤参数未生效）`);
  }
  problems = [...new Set(problems)];
  const missingPending = (ep.pending_keys ?? []).filter((k) => !(k in res.body));
  if (problems.length) {
    if (ep.pending) pending++; else errors++;
    console.log(`${ep.pending ? '…' : '✗'}  ${label} ${problems.length} 处不一致`);
    problems.slice(0, 8).forEach((p) => console.log(`      - ${p}`));
  } else {
    console.log(`✓  ${label}${missingPending.length ? `（🆕 待补字段：${missingPending.join(', ')}）` : ''}`);
  }
}
console.log(`\n${errors ? `✗ ${errors} 个接口不符合契约` : '✓ 基线全部符合契约'}${pending ? `；${pending} 项 🆕 待实现` : ''}`);
process.exit(errors ? 1 : 0);
