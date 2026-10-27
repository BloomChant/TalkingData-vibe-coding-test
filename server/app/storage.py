import hashlib
import json
import os
import sqlite3
import threading
import time
import zlib
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get('DATA_DIR', 'data'))
DATABASE = DATA_DIR / 'observatory.sqlite3'
WARN_BYTES = int(os.environ.get('DISK_WARN_BYTES', 16 * 1024**3))
STOP_BYTES = int(os.environ.get('DISK_STOP_BYTES', 18 * 1024**3))
WRITE_LOCK = threading.Lock()
SCHEMA = '''
CREATE TABLE IF NOT EXISTS reconciliations(run TEXT PRIMARY KEY,checked_at REAL NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,created REAL NOT NULL,last_seen REAL NOT NULL,revision INTEGER NOT NULL DEFAULT 0,objects INTEGER NOT NULL DEFAULT 0,batches INTEGER NOT NULL DEFAULT 0,duplicates INTEGER NOT NULL DEFAULT 0,rejected INTEGER NOT NULL DEFAULT 0,payload_bytes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS types(run TEXT NOT NULL,id INTEGER NOT NULL,parent INTEGER NOT NULL,name TEXT NOT NULL,count INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL,PRIMARY KEY(run,id));
CREATE INDEX IF NOT EXISTS types_parent ON types(run,parent);
CREATE TABLE IF NOT EXISTS messages(run TEXT NOT NULL,kind TEXT NOT NULL,key INTEGER NOT NULL,digest TEXT NOT NULL,body_z BLOB NOT NULL,size INTEGER NOT NULL,objects INTEGER NOT NULL,created REAL NOT NULL,headers TEXT NOT NULL,PRIMARY KEY(run,kind,key));
CREATE TABLE IF NOT EXISTS objects(row_id INTEGER PRIMARY KEY,run TEXT NOT NULL,id TEXT NOT NULL,type_id INTEGER NOT NULL,name TEXT NOT NULL,sequence INTEGER NOT NULL,timestamp_ms INTEGER NOT NULL,tree_revision INTEGER NOT NULL,body TEXT NOT NULL,digest TEXT NOT NULL,frame_id TEXT,frame_kind TEXT,unit TEXT,x REAL,y REAL,z REAL,UNIQUE(run,id));
CREATE INDEX IF NOT EXISTS objects_run_row ON objects(run,row_id DESC);
CREATE INDEX IF NOT EXISTS objects_type_row ON objects(run,type_id,row_id DESC);
CREATE INDEX IF NOT EXISTS objects_frame ON objects(run,frame_id,unit,row_id);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,run TEXT NOT NULL,created REAL NOT NULL,kind TEXT NOT NULL,summary TEXT NOT NULL,details TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_run ON events(run,id DESC);
CREATE TABLE IF NOT EXISTS rejected(id INTEGER PRIMARY KEY,run TEXT NOT NULL,created REAL NOT NULL,reason TEXT NOT NULL,digest TEXT NOT NULL,body_z BLOB NOT NULL,headers TEXT NOT NULL,batch_id INTEGER,attempts INTEGER NOT NULL DEFAULT 1,UNIQUE(run,digest,reason));
CREATE TABLE IF NOT EXISTS buckets(run TEXT NOT NULL,second INTEGER NOT NULL,objects INTEGER NOT NULL DEFAULT 0,batches INTEGER NOT NULL DEFAULT 0,duplicates INTEGER NOT NULL DEFAULT 0,rejected INTEGER NOT NULL DEFAULT 0,latency_sum REAL NOT NULL DEFAULT 0,latency_count INTEGER NOT NULL DEFAULT 0,latency_max REAL NOT NULL DEFAULT 0,PRIMARY KEY(run,second));
'''


class IngestError(Exception):
    def __init__(self,status,reason):
        self.status,self.reason=status,reason
        super().__init__(reason)


@contextmanager
def connect():
    db=sqlite3.connect(DATABASE,timeout=10)
    db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA synchronous=FULL')
        with db: yield db
    finally: db.close()


def init():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)


def usage():
    total=0
    for p in DATA_DIR.rglob('*'):
        try:
            if p.is_file(): total+=p.stat().st_size
        except FileNotFoundError: pass
    return total


def admission(body):
    if usage()+4*len(body)+1024**2>=STOP_BYTES: raise IngestError(507,'Storage high-water mark reached')


def ensure_run(db,run,now):
    db.execute('INSERT OR IGNORE INTO runs(id,created,last_seen) VALUES(?,?,?)',(run,now,now))


def event(db,run,kind,summary,details):
    db.execute('INSERT INTO events(run,created,kind,summary,details) VALUES(?,?,?,?,?)',(run,time.time(),kind,summary,json.dumps(details,ensure_ascii=False)))


def bucket(db,run,now,objects=0,batches=0,duplicates=0,rejected=0):
    db.execute('''INSERT INTO buckets(run,second,objects,batches,duplicates,rejected) VALUES(?,?,?,?,?,?)
      ON CONFLICT(run,second) DO UPDATE SET objects=objects+excluded.objects,batches=batches+excluded.batches,duplicates=duplicates+excluded.duplicates,rejected=rejected+excluded.rejected''',
      (run,int(now),objects,batches,duplicates,rejected))


def canonical(obj):
    return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def quarantine(body,msg,headers,reason):
    run=msg.get('test_id','_unassigned') if isinstance(msg,dict) else '_unassigned'
    if not isinstance(run,str) or not 1<=len(run)<=128: run='_unassigned'
    bid=msg.get('batch_id') if isinstance(msg,dict) else None
    if type(bid) is not int or not 0<=bid<2**63: bid=None
    now=time.time()
    with WRITE_LOCK:
        admission(body)
        with connect() as db:
            ensure_run(db,run,now)
            db.execute('''INSERT INTO rejected(run,created,reason,digest,body_z,headers,batch_id) VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(run,digest,reason) DO UPDATE SET attempts=attempts+1''',
              (run,now,reason,hashlib.sha256(body).hexdigest(),zlib.compress(body,1),json.dumps(headers),bid))
            db.execute('UPDATE runs SET rejected=rejected+1,last_seen=? WHERE id=?',(now,run))
            bucket(db,run,now,rejected=1)


def apply_tree(db,msg):
    run,rev=msg['test_id'],msg['revision']
    current=db.execute('SELECT revision FROM runs WHERE id=?',(run,)).fetchone()[0]
    if rev!=current+1: raise IngestError(503,f'Expected taxonomy revision {current+1}, got {rev}')
    for change in msg['changes']:
        op,node=change['op'],change['node']
        tid,parent,name=node['type_id'],node['parent_id'],node['name']
        old=db.execute('SELECT * FROM types WHERE run=? AND id=?',(run,tid)).fetchone()
        if parent and not db.execute('SELECT 1 FROM types WHERE run=? AND id=?',(run,parent)).fetchone():
            raise IngestError(422,'Unknown taxonomy parent')
        if op=='create':
            if old: raise IngestError(409,'Taxonomy node already exists')
            if parent==tid: raise IngestError(422,'Taxonomy cycle')
            db.execute('INSERT INTO types(run,id,parent,name,revision) VALUES(?,?,?,?,?)',(run,tid,parent,name,rev))
            summary=f'新增分类「{name}」'
        else:
            if not old: raise IngestError(422,'Unknown taxonomy node')
            if op=='rename' and parent!=old['parent']: raise IngestError(422,'Rename cannot change parent')
            cursor=parent;visited={tid}
            while cursor:
                if cursor in visited: raise IngestError(422,'Taxonomy cycle')
                visited.add(cursor)
                row=db.execute('SELECT parent FROM types WHERE run=? AND id=?',(run,cursor)).fetchone()
                if not row: raise IngestError(422,'Unknown taxonomy ancestor')
                cursor=row[0]
            db.execute('UPDATE types SET parent=?,name=?,revision=? WHERE run=? AND id=?',(parent,name,rev,run,tid))
            summary=f'分类改名：{old["name"]} → {name}' if op=='rename' else f'分类迁移：{name}（{old["parent"]} → {parent}）'
        event(db,run,op,summary,{'revision':rev,'node':node,'previous':dict(old) if old else None})
    db.execute('UPDATE runs SET revision=? WHERE id=?',(rev,run))
    return 0


def apply_objects(db,msg):
    run=msg['test_id']
    current=db.execute('SELECT revision FROM runs WHERE id=?',(run,)).fetchone()[0]
    if msg['tree_revision']>current: raise IngestError(503,'Referenced taxonomy revision has not arrived')
    known={r[0] for r in db.execute('SELECT id FROM types WHERE run=?',(run,))}
    counts=Counter();added=0
    for obj in msg['objects']:
        if obj['type_id'] not in known: raise IngestError(422,'Object references unknown type_id')
        content=canonical(obj);digest=hashlib.sha256(content.encode()).hexdigest();pos=obj.get('position') or {}
        cur=db.execute('''INSERT OR IGNORE INTO objects(run,id,type_id,name,sequence,timestamp_ms,tree_revision,body,digest,frame_id,frame_kind,unit,x,y,z)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(run,obj['id'],obj['type_id'],obj['name'],obj['sequence'],obj['timestamp_ms'],msg['tree_revision'],content,digest,pos.get('frame_id'),pos.get('frame_kind'),pos.get('unit'),float(pos['x']) if pos else None,float(pos['y']) if pos else None,float(pos['z']) if pos else None))
        if cur.rowcount: added+=1;counts[obj['type_id']]+=1
        else:
            previous=db.execute('SELECT digest FROM objects WHERE run=? AND id=?',(run,obj['id'])).fetchone()[0]
            if previous!=digest: raise IngestError(409,'Object ID has conflicting content; no update-version contract is known')
    db.executemany('UPDATE types SET count=count+? WHERE run=? AND id=?',[(n,run,t) for t,n in counts.items()])
    db.execute('UPDATE runs SET objects=objects+?,batches=batches+1 WHERE id=?',(added,run))
    return added


def ingest(body,msg,headers):
    run,kind=msg['test_id'],msg['kind'];key=msg['batch_id'] if kind=='objects' else msg['revision']
    digest=hashlib.sha256(canonical(msg).encode()).hexdigest();now=time.time();started=time.perf_counter()
    if not WRITE_LOCK.acquire(timeout=0.15): raise IngestError(429,'Writer busy; retry later')
    try:
        with connect() as db:
            ensure_run(db,run,now)
            previous=db.execute('SELECT digest FROM messages WHERE run=? AND kind=? AND key=?',(run,kind,key)).fetchone()
            if previous:
                if previous[0]!=digest: raise IngestError(409,'Idempotency key has conflicting content')
                db.execute('UPDATE runs SET duplicates=duplicates+1,last_seen=? WHERE id=?',(now,run));bucket(db,run,now,duplicates=1)
                return {'persisted':True,'duplicate':True,'inserted_objects':0}
            admission(body)
            added=apply_tree(db,msg) if kind=='types' else apply_objects(db,msg)
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)',(run,kind,key,digest,zlib.compress(body,1),len(body),len(msg.get('objects',[])),now,json.dumps(headers)))
            db.execute('UPDATE runs SET last_seen=?,payload_bytes=payload_bytes+? WHERE id=?',(now,len(body),run))
            bucket(db,run,now,objects=added,batches=int(kind=='objects'))
        elapsed=(time.perf_counter()-started)*1000
        try:
            with connect() as db:
                db.execute('UPDATE buckets SET latency_sum=latency_sum+?,latency_count=latency_count+1,latency_max=MAX(latency_max,?) WHERE run=? AND second=?',(elapsed,elapsed,run,int(now)))
        except sqlite3.Error: pass
        return {'persisted':True,'duplicate':False,'inserted_objects':added}
    finally: WRITE_LOCK.release()
