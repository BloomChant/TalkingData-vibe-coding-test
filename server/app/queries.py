"""Read-only projections for the observatory. SQLite remains the source of truth."""
import json
import re
import zlib
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from . import storage as store
from .reconcile import reconcile_receipt, bitmap

router = APIRouter()
STARTED = time.monotonic()
_CPU_LOCK = threading.Lock()
_CPU_SAMPLE = None


def select_run(db, run):
    if run:
        row = db.execute('SELECT * FROM runs WHERE id=?', (run,)).fetchone()
    else:
        row = db.execute("SELECT * FROM runs WHERE id!='_unassigned' ORDER BY last_seen DESC LIMIT 1").fetchone()
    return dict(row) if row else None


@router.get('/runs')
def runs():
    with store.connect() as db:
        rows = db.execute("SELECT * FROM runs WHERE id!='_unassigned' ORDER BY last_seen DESC LIMIT 100").fetchall()
    return {'runs': [dict(r) for r in rows]}


@router.get('/overview')
def overview(run: Optional[str] = Query(None, max_length=128)):
    now = time.time()
    with store.connect() as db:
        db.execute('BEGIN')
        selected = select_run(db, run)
        if not selected:
            return {'run': None, 'types': 0, 'series': [], 'rate': 0,
                    'active': False, 'operations': {}, 'positioned': 0}
        run = selected['id']
        active = now - selected['last_seen'] < 5
        end = int(now if active else selected['last_seen'])
        stored = {r['second']: dict(r) for r in db.execute(
            'SELECT * FROM buckets WHERE run=? AND second BETWEEN ? AND ? ORDER BY second',
            (run, end - 59, end))}
        series = [stored.get(s, {'run': run, 'second': s, 'objects': 0, 'batches': 0,
                                'duplicates': 0, 'rejected': 0, 'latency_sum': 0,
                                'latency_count': 0, 'latency_max': 0}) for s in range(end - 59, end + 1)]
        # Five complete seconds, rather than a misleading extrapolation of a partial second.
        recent = db.execute('SELECT COALESCE(SUM(objects),0) FROM buckets WHERE run=? AND second>=? AND second<?',
                            (run, int(now) - 5, int(now))).fetchone()[0]
        types = db.execute('SELECT COUNT(*) FROM types WHERE run=?', (run,)).fetchone()[0]
        positioned = db.execute('SELECT COUNT(*) FROM objects WHERE run=? AND frame_id IS NOT NULL', (run,)).fetchone()[0]
        operations = {r['kind']: r['n'] for r in db.execute(
            "SELECT kind,COUNT(*) AS n FROM events WHERE run=? AND kind IN ('create','rename','move') GROUP BY kind", (run,))}
    return {'run': selected, 'types': types, 'series': series, 'rate': recent / 5 if active else 0,
            'active': active, 'operations': operations, 'positioned': positioned,
            'activity_semantics': 'A request was persisted in the last five seconds; not generator control state.'}


@router.get('/tree')
def tree(run: Optional[str] = Query(None, max_length=128)):
    with store.connect() as db:
        db.execute('BEGIN')
        selected = select_run(db, run)
        if not selected:
            return {'revision': 0, 'nodes': []}
        nodes = [dict(r) for r in db.execute('SELECT id,parent,name,count,revision FROM types WHERE run=? ORDER BY id',
                                           (selected['id'],))]
    by_id = {n['id']: n for n in nodes}
    children = {n['id']: 0 for n in nodes}
    for n in nodes:
        n['subtree_count'] = n['count']
        if n['parent'] in children:
            children[n['parent']] += 1
    leaves = deque(t for t, count in children.items() if not count)
    visited = 0
    while leaves:
        node = by_id[leaves.popleft()]
        visited += 1
        parent = node['parent']
        if parent in by_id:
            by_id[parent]['subtree_count'] += node['subtree_count']
            children[parent] -= 1
            if not children[parent]:
                leaves.append(parent)
    if visited != len(nodes):
        raise HTTPException(503, 'Stored taxonomy contains a cycle')
    return {'revision': selected['revision'], 'nodes': nodes}


# The derived label follows the frontend's ASCII-digit suffix rule exactly.
KIND_SUFFIX = re.compile(r'\s*#[0-9]+$')


def object_kind(name):
    return KIND_SUFFIX.sub('', name)


def read_filters(request):
    filters = {}
    for key in ('type_id', 'kind', 'frame', 'frame_kind'):
        values = request.query_params.getlist(key)
        if len(values) > 50:
            raise HTTPException(422, 'At most 50 values per filter')
        if any(not value or len(value) > 512 for value in values):
            raise HTTPException(422, 'Invalid filter value')
        if key == 'type_id':
            try:
                values = [int(value) for value in values]
            except ValueError:
                raise HTTPException(422, 'Invalid type_id')
            if any(not 0 < value < 2**63 for value in values):
                raise HTTPException(422, 'Invalid type_id')
        filters[key] = list(dict.fromkeys(values))
    query = request.query_params.get('q', '')
    if len(query) > 128:
        raise HTTPException(422, 'Search is limited to 128 characters')
    filters['q'] = query
    return filters


def criteria(db, run, filters, exclude=None):
    db.create_function('object_kind', 1, object_kind, deterministic=True)
    where, params = ['o.run=?'], [run]
    types = filters.get('type_id', []) if exclude != 'type' else []
    if types:
        where.append('o.type_id IN (WITH RECURSIVE subtree(id) AS ('
                     'SELECT id FROM types WHERE run=? AND id IN (' + ','.join('?' for _ in types) + ') '
                     'UNION SELECT t.id FROM types t JOIN subtree s ON t.parent=s.id WHERE t.run=?) '
                     'SELECT id FROM subtree)')
        params.extend([run, *types, run])
    if filters.get('q'):
        where.append('instr(o.name,?)>0')
        params.append(filters['q'])
    kinds = filters.get('kind', []) if exclude != 'kind' else []
    if kinds:
        where.append('object_kind(o.name) IN (' + ','.join('?' for _ in kinds) + ')')
        params.extend(kinds)
    frames = filters.get('frame', []) if exclude != 'galaxy' else []
    if frames:
        clauses = []
        for frame in frames:
            clauses.append('(o.frame_id=? OR substr(o.frame_id,1,?)=?)')
            params.extend([frame, len(frame)+1, frame+'/'])
        where.append('(' + ' OR '.join(clauses) + ')')
    frame_kinds = filters.get('frame_kind', []) if exclude != 'frame_kind' else []
    if frame_kinds:
        clauses = []
        for value in frame_kinds:
            if value == 'none':
                clauses.append('o.frame_id IS NULL')
            else:
                clauses.append('o.frame_kind=?')
                params.append(value)
        where.append('(' + ' OR '.join(clauses) + ')')
    return ' AND '.join(where), params


@router.get('/objects')
def objects(request: Request, run: Optional[str] = Query(None, max_length=128),
            cursor: Optional[int] = Query(None, ge=0, lt=2**63),
            limit: int = Query(25, ge=1, le=100)):
    filters = read_filters(request)
    with store.connect() as db:
        db.execute('BEGIN')
        selected = select_run(db, run)
        if not selected:
            return {'items': [], 'next_cursor': None, 'total': 0, 'total_exact': True}
        where, params = criteria(db, selected['id'], filters)
        total = db.execute('SELECT COUNT(*) FROM objects o WHERE ' + where, params).fetchone()[0]
        if cursor is not None:
            where += ' AND o.row_id<?'
            params.append(cursor)
        rows = db.execute('SELECT o.row_id,o.id,o.name,o.type_id,t.name AS type_name,'
                          'o.timestamp_ms,o.frame_id,o.frame_kind,o.unit FROM objects o '
                          'LEFT JOIN types t ON t.run=o.run AND t.id=o.type_id WHERE '
                          + where + ' ORDER BY o.row_id DESC LIMIT ?', [*params, limit+1]).fetchall()
    items = [dict(r) for r in rows[:limit]]
    return {'items': items, 'next_cursor': items[-1]['row_id'] if len(rows)>limit else None,
            'total': total, 'total_exact': True}


@router.get('/objects/{object_id:path}')
def object_detail(object_id: str, run: Optional[str] = Query(None, max_length=128)):
    if len(object_id) > 512:
        raise HTTPException(422, 'Object ID too long')
    with store.connect() as db:
        selected = select_run(db, run)
        row = db.execute('''SELECT o.body,o.tree_revision,t.name AS type_name FROM objects o
            LEFT JOIN types t ON t.run=o.run AND t.id=o.type_id
            WHERE o.run=? AND o.id=?''', (selected['id'] if selected else '', object_id)).fetchone()
    if not row:
        raise HTTPException(404, 'Object not found')
    return {'object': json.loads(row['body']), 'tree_revision': row['tree_revision'], 'type_name': row['type_name']}


@router.get('/events')
def events(run: Optional[str] = Query(None, max_length=128), limit: int = Query(50, ge=1, le=50)):
    with store.connect() as db:
        selected = select_run(db, run)
        rows = db.execute('SELECT * FROM events WHERE run=? ORDER BY id DESC LIMIT ?',
                          (selected['id'] if selected else '', limit)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item['details'] = json.loads(item['details'])
        items.append(item)
    return {'events': items}


@router.get('/quality')
def quality(run: Optional[str] = Query(None, max_length=128)):
    with store.connect() as db:
        selected = select_run(db, run)
        if not selected:
            return {'rejected': [], 'duplicates': 0, 'rejected_attempts': 0}
        rows = db.execute('SELECT id,created,reason,batch_id,attempts,body_z FROM rejected WHERE run=? ORDER BY id DESC LIMIT 50',
                          (selected['id'],)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        compressed = item.pop('body_z')
        try:
            item['excerpt'] = zlib.decompressobj().decompress(compressed, 1024).decode('utf-8', errors='replace')
        except zlib.error:
            item['excerpt'] = '[原始片段不可读]'
        items.append(item)
    return {'rejected': items, 'duplicates': selected['duplicates'],
            'rejected_attempts': selected['rejected'], 'policy': 'Invalid batches are quarantined in full; unknown attributes are accepted.'}


def _read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, ValueError):
        return None


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@router.get('/system')
def system():
    global _CPU_SAMPLE
    memory = _number(_read('/sys/fs/cgroup/memory.current'))
    memory_limit = _number(_read('/sys/fs/cgroup/memory.max'))
    cpu_limit = None
    quota = _read('/sys/fs/cgroup/cpu.max')
    if quota:
        values = quota.split()
        if len(values) == 2 and values[0] != 'max':
            try:
                cpu_limit = int(values[0]) / int(values[1])
            except (ValueError, ZeroDivisionError):
                pass
    stats = _read('/sys/fs/cgroup/cpu.stat')
    usage_us = None
    if stats:
        for line in stats.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == 'usage_usec':
                usage_us = _number(parts[1])
    cpu_percent = None
    now = time.monotonic()
    if usage_us is not None:
        with _CPU_LOCK:
            if _CPU_SAMPLE is not None:
                before, before_us = _CPU_SAMPLE
                if now > before and usage_us >= before_us:
                    cpu_percent = (usage_us - before_us) / (now - before) / 10000
            _CPU_SAMPLE = (now, usage_us)
    return {'memory_bytes': memory, 'memory_limit_bytes': memory_limit,
            'cpu_limit': cpu_limit, 'cpu_percent': cpu_percent,
            'data_bytes': store.usage(), 'disk_warn_bytes': store.WARN_BYTES,
            'disk_stop_bytes': store.STOP_BYTES, 'disk_budget_bytes': 20 * 1024**3,
            'uptime_seconds': now - STARTED,
            'metrics_source': 'cgroup v2; null means unavailable; 100% CPU equals one core',
            'disk_policy': 'Application high-water protection, not a filesystem hard quota; Docker logs have separate rotation.'}


@router.get('/frames')
def frames(request: Request, run: Optional[str] = Query(None, max_length=128)):
    filters = read_filters(request)
    with store.connect() as db:
        selected = select_run(db, run)
        where, params = criteria(db, selected['id'] if selected else '', filters)
        rows = db.execute('SELECT o.frame_id,o.frame_kind,o.unit,COUNT(*) AS count FROM objects o '
                          'WHERE ' + where + ' AND o.frame_id IS NOT NULL '
                          'GROUP BY o.frame_id,o.frame_kind,o.unit ORDER BY count DESC LIMIT 1001', params).fetchall()
    return {'frames': [dict(r) for r in rows[:1000]], 'limit': 1000, 'filters_applied': True, 'truncated': len(rows)>1000}


@router.get('/positions')
def positions(request: Request, frame_id: str = Query(..., min_length=1, max_length=512),
              unit: str = Query(..., min_length=1, max_length=512),
              run: Optional[str] = Query(None, max_length=128), limit: int = Query(1000, ge=1, le=2000)):
    filters = read_filters(request)
    with store.connect() as db:
        selected = select_run(db, run)
        where, params = criteria(db, selected['id'] if selected else '', filters)
        rows = db.execute('SELECT o.id,o.name,o.x,o.y,o.z,o.type_id FROM objects o WHERE '
                          + where + ' AND o.frame_id=? AND o.unit=? ORDER BY o.row_id DESC LIMIT ?',
                          [*params, frame_id, unit, limit]).fetchall()
    return {'points': [dict(r) for r in rows], 'sampled': True, 'filters_applied': True,
            'sampling': 'Most recently received objects in this exact frame and unit; not an unbiased sample.'}


@router.get('/facets')
def facets(request: Request, run: Optional[str] = Query(None, max_length=128),
           dims: str = Query('type,kind,frame_kind,galaxy', max_length=128)):
    dimensions = list(dict.fromkeys(dims.split(',')))
    if any(d not in ('type', 'kind', 'frame_kind', 'galaxy') for d in dimensions):
        raise HTTPException(422, 'Unknown facet dimension')
    filters = read_filters(request)
    output = {}
    with store.connect() as db:
        db.execute('BEGIN')
        selected = select_run(db, run)
        actual_run = selected['id'] if selected else (run or '')
        where, params = criteria(db, actual_run, filters)
        total = db.execute('SELECT COUNT(*) FROM objects o WHERE '+where, params).fetchone()[0]
        for dimension in dimensions:
            where, params = criteria(db, actual_run, filters, exclude=dimension)
            if dimension == 'type':
                expression, label = 'CAST(o.type_id AS TEXT)', 'COALESCE(t.name,CAST(o.type_id AS TEXT))'
            elif dimension == 'kind':
                expression = label = 'object_kind(o.name)'
            elif dimension == 'frame_kind':
                expression = "COALESCE(o.frame_kind,'none')"
                label = "COALESCE(o.frame_kind,'未提供位置')"
            else:
                expression = "CASE WHEN o.frame_id IS NULL THEN 'none' WHEN instr(o.frame_id,'/')=0 THEN o.frame_id ELSE substr(o.frame_id,1,instr(o.frame_id,'/')-1) END"
                label = "CASE WHEN o.frame_id IS NULL THEN '未提供位置' WHEN instr(o.frame_id,'/')=0 THEN o.frame_id ELSE substr(o.frame_id,1,instr(o.frame_id,'/')-1) END"
            rows = db.execute('SELECT '+expression+' AS value,'+label+' AS label,COUNT(*) AS count '
                              'FROM objects o LEFT JOIN types t ON t.run=o.run AND t.id=o.type_id WHERE '
                              +where+' GROUP BY value ORDER BY count DESC,value', params)
            buckets, other = [], 0
            for row in rows:
                if dimension == 'type' or len(buckets)<100:
                    buckets.append(dict(row))
                else:
                    other += row['count']
            output[dimension] = {'buckets': buckets, 'other': other}
    return {'run': actual_run, 'filters_applied': True, 'total': total, 'facets': output,
            'facet_semantics': 'Each facet ignores its own filter; galaxy ignores frame prefixes. total applies all filters.'}


def field_type(value):
    if value is None: return 'null'
    if isinstance(value, bool): return 'boolean'
    if isinstance(value, (int, float)): return 'number'
    if isinstance(value, str): return 'string'
    if isinstance(value, list): return 'array'
    return 'object'


@router.get('/fields')
def fields(request: Request, run: Optional[str] = Query(None, max_length=128),
           sample: int = Query(2000, ge=1, le=2000)):
    filters = read_filters(request)
    stats, size, payload_bytes, observations = {}, 0, 0, 0
    reasons = set()
    with store.connect() as db:
        db.execute('BEGIN')
        selected = select_run(db, run)
        actual_run = selected['id'] if selected else (run or '')
        where, params = criteria(db, actual_run, filters)
        matched = db.execute('SELECT COUNT(*) FROM objects o WHERE '+where, params).fetchone()[0]
        rows = db.execute('SELECT o.body FROM objects o WHERE '+where+' ORDER BY o.row_id DESC LIMIT ?', [*params, sample])
        for row in rows:
            raw = row['body']
            payload_bytes += len(raw.encode('utf-8'))
            if payload_bytes > 16*1024**2:
                reasons.add('16 MiB sample payload limit')
                break
            if observations >= 500000:
                reasons.add('500000 field observation limit')
                break
            obj = json.loads(raw)
            size += 1
            present, typed = set(), {}
            stack = [(f'attributes.{key}', value, 1) for key, value in obj['attributes'].items()]
            while stack:
                path, value, depth = stack.pop()
                observations += 1
                if observations > 500000:
                    reasons.add('500000 field observation limit')
                    break
                if path not in stats:
                    if len(stats)>=10000:
                        reasons.add('10000 distinct path limit')
                        continue
                    stats[path] = {'path': path, 'present': 0, 'types': {}, 'examples': [], 'numeric': None}
                entry = stats[path]
                present.add(path)
                kind = field_type(value)
                typed.setdefault(path,set()).add(kind)
                if kind in ('string','number','boolean','null'):
                    example = value[:40] if isinstance(value,str) else value
                    if len(entry['examples'])<3 and not any(type(v) is type(example) and v==example for v in entry['examples']):
                        entry['examples'].append(example)
                if kind=='number':
                    if entry['numeric'] is None: entry['numeric']={'min':value,'max':value}
                    else:
                        entry['numeric']['min']=min(entry['numeric']['min'],value)
                        entry['numeric']['max']=max(entry['numeric']['max'],value)
                if depth<4:
                    if isinstance(value,dict):
                        stack.extend((path+'.'+key, child, depth+1) for key,child in value.items())
                    elif isinstance(value,list):
                        stack.extend((path+'[]', child, depth+1) for child in value)
            for path in present:
                stats[path]['present']+=1
                for kind in typed[path]:
                    stats[path]['types'][kind]=stats[path]['types'].get(kind,0)+1
    ordered=sorted(stats.values(),key=lambda item:(-item['present'],item['path']))
    if len(ordered)>200: reasons.add('200 returned field limit')
    for entry in ordered[:200]:
        entry['coverage']=entry['present']/size if size else 0
        entry['mixed']=len(set(entry['types'])-{'null'})>1
    return {'run':actual_run,'filters_applied':True,
            'sample':{'size':size,'matched':matched,'sampled':size<matched,'order':'most_recent'},
            'fields':ordered[:200],'truncated':bool(reasons),'truncation_reasons':sorted(reasons),
            'type_count_semantics':'One count per object per observed type; array paths can have multiple types in one object.'}


@router.post('/reconcile')
async def reconcile(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 2 * 1024**2:
            raise HTTPException(413, 'Receipt exceeds 2 MiB')
        body.extend(chunk)
    try:
        receipt = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, 'Invalid receipt JSON')
    return await run_in_threadpool(reconcile_and_store, receipt)


def reconcile_and_store(receipt):
    # All ingestion writers share this lock: checks and batch states see one stable world.
    if not store.WRITE_LOCK.acquire(timeout=1):
        raise HTTPException(503, 'Writer busy; retry reconciliation')
    try:
        result = reconcile_receipt(receipt)
        run = result['run']
        capacity = receipt['config']['max_batches']
        ack = bitmap(receipt['ack_bitmap'], 'ack_bitmap', capacity)
        sent = bitmap(receipt['sent_bitmap'], 'sent_bitmap', capacity)
        with store.connect() as db:
            stored = {row[0] for row in db.execute("SELECT key FROM messages WHERE run=? AND kind='objects' AND key<?", (run, capacity))}
        states = bytearray(b'-')*capacity
        for key in sent: states[key] = ord('X')
        for key in stored: states[key] = ord('U')
        for key in ack: states[key] = ord('A') if key in stored else ord('M')
        result['batches'] = {'size': capacity, 'states': states.decode('ascii')}
        result['checked_at'] = time.time()
        result['available'] = True
        payload = json.dumps(result,ensure_ascii=False,separators=(',',':'))
        store.admission(payload.encode('utf-8'))
        with store.connect() as db:
            db.execute('INSERT INTO reconciliations(run,checked_at,result) VALUES(?,?,?) '
                       'ON CONFLICT(run) DO UPDATE SET checked_at=excluded.checked_at,result=excluded.result',
                       (run,result['checked_at'],payload))
        return result
    except store.IngestError as exc:
        raise HTTPException(exc.status, exc.reason)
    except sqlite3.Error:
        raise HTTPException(503, 'Could not persist reconciliation')
    finally:
        store.WRITE_LOCK.release()


@router.get('/reconcile')
def last_reconcile(run: Optional[str] = Query(None,max_length=128)):
    with store.connect() as db:
        selected = select_run(db,run)
        actual_run = selected['id'] if selected else (run or '')
        row = db.execute('SELECT result FROM reconciliations WHERE run=?',(actual_run,)).fetchone()
    return json.loads(row[0]) if row else {'run':actual_run,'available':False}
