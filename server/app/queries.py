"""Read-only projections for the observatory. SQLite remains the source of truth."""
import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from . import storage as store
from .reconcile import reconcile_receipt

router = APIRouter()
STARTED = time.monotonic()
_CPU_LOCK = threading.Lock()
_CPU_SAMPLE = None


def select_run(db, run):
    if run:
        row = db.execute('SELECT * FROM runs WHERE id=?', (run,)).fetchone()
    else:
        row = db.execute('SELECT * FROM runs ORDER BY last_seen DESC LIMIT 1').fetchone()
    return dict(row) if row else None


@router.get('/runs')
def runs():
    with store.connect() as db:
        rows = db.execute('SELECT * FROM runs ORDER BY last_seen DESC LIMIT 100').fetchall()
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
        series = [stored.get(s, {'second': s, 'objects': 0, 'batches': 0,
                                'duplicates': 0, 'rejected': 0}) for s in range(end - 59, end + 1)]
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


@router.get('/objects')
def objects(run: Optional[str] = Query(None, max_length=128),
            type_id: Optional[int] = Query(None, ge=1, lt=2**63),
            q: str = Query('', max_length=128),
            cursor: Optional[int] = Query(None, ge=0, lt=2**63),
            limit: int = Query(25, ge=1, le=100)):
    with store.connect() as db:
        selected = select_run(db, run)
        if not selected:
            return {'items': [], 'next_cursor': None}
        run = selected['id']
        prefix = ''
        params = []
        where = ['o.run=?']
        if type_id is not None:
            prefix = '''WITH RECURSIVE subtree(id) AS (
                SELECT id FROM types WHERE run=? AND id=?
                UNION SELECT t.id FROM types t JOIN subtree s ON t.parent=s.id WHERE t.run=?) '''
            params.extend([run, type_id, run])
            where.append('o.type_id IN (SELECT id FROM subtree)')
        params.append(run)
        if q:
            where.append('instr(o.name,?)>0')
            params.append(q)
        if cursor is not None:
            where.append('o.row_id<?')
            params.append(cursor)
        params.append(limit + 1)
        rows = db.execute(prefix + '''SELECT o.row_id,o.id,o.name,o.type_id,t.name AS type_name,
            o.timestamp_ms,o.frame_id,o.frame_kind,o.unit
            FROM objects o LEFT JOIN types t ON t.run=o.run AND t.id=o.type_id
            WHERE ''' + ' AND '.join(where) + ' ORDER BY o.row_id DESC LIMIT ?', params).fetchall()
    items = [dict(r) for r in rows[:limit]]
    return {'items': items, 'next_cursor': items[-1]['row_id'] if len(rows) > limit else None}


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
        rows = db.execute('SELECT id,created,reason,batch_id,attempts FROM rejected WHERE run=? ORDER BY id DESC LIMIT 50',
                          (selected['id'],)).fetchall()
    return {'rejected': [dict(r) for r in rows], 'duplicates': selected['duplicates'],
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
def frames(run: Optional[str] = Query(None, max_length=128)):
    with store.connect() as db:
        selected = select_run(db, run)
        rows = db.execute('''SELECT frame_id,frame_kind,unit,COUNT(*) AS count FROM objects
          WHERE run=? AND frame_id IS NOT NULL GROUP BY frame_id,frame_kind,unit ORDER BY count DESC LIMIT 1000''',
          (selected['id'] if selected else '',)).fetchall()
    return {'frames': [dict(r) for r in rows], 'limit': 1000}


@router.get('/positions')
def positions(frame_id: str = Query(..., min_length=1, max_length=512),
              unit: str = Query(..., min_length=1, max_length=512),
              run: Optional[str] = Query(None, max_length=128), limit: int = Query(1000, ge=1, le=2000)):
    with store.connect() as db:
        selected = select_run(db, run)
        rows = db.execute('''SELECT id,name,x,y,z,type_id FROM objects
          WHERE run=? AND frame_id=? AND unit=? ORDER BY row_id DESC LIMIT ?''',
          (selected['id'] if selected else '', frame_id, unit, limit)).fetchall()
    return {'points': [dict(r) for r in rows], 'sampled': True,
            'sampling': 'Most recently received objects in this exact frame and unit; not an unbiased sample.'}


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
    return await run_in_threadpool(reconcile_receipt, receipt)
