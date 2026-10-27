"""Persisted, single-owner control of the supplied HTTP object generator."""
import asyncio
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, conint
from starlette.concurrency import run_in_threadpool

from . import storage as store
from .queries import reconcile_and_store

router = APIRouter()
CONTROL_LOCK = threading.Lock()
GENERATOR_URL = os.environ.get('GENERATOR_URL', 'http://127.0.0.1:9001').rstrip('/')
GENERATOR_TARGET_URL = os.environ.get('GENERATOR_TARGET_URL', 'http://host.docker.internal:8000/ingest')
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ACTIVE = ('starting', 'running', 'stopping')


class StartConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rate: conint(strict=True, ge=1, le=20000) = 200
    duration: conint(strict=True, ge=5, le=600) = 60
    batch_size: conint(strict=True, ge=1, le=500) = 20
    workers: conint(strict=True, ge=1, le=8) = 4
    seed: conint(strict=True, ge=0, le=2**31-1) = 42
    scenario: Literal['mixed', 'evolving', 'stress'] = 'evolving'
    profile: Literal['steady', 'bursty'] = 'steady'


class StopConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    test_id: str


def gateway(path, payload=None):
    body = None if payload is None else json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(GENERATOR_URL + path, data=body,
                                    headers={'Content-Type': 'application/json'})
    with OPENER.open(request, timeout=3) as response:
        raw = response.read(2*1024**2+1)
    if len(raw)>2*1024**2:
        raise ValueError('Generator response exceeds 2 MiB')
    result = json.loads(raw)
    if not isinstance(result,dict):
        raise ValueError('Generator response is not an object')
    return result


def latest(active_only=False):
    with store.connect() as db:
        where = " WHERE state IN ('starting','running','stopping')" if active_only else ''
        row = db.execute('SELECT record FROM experiments'+where+' ORDER BY created_at DESC LIMIT 1').fetchone()
    return json.loads(row[0]) if row else None


def save(record):
    payload = json.dumps(record,ensure_ascii=False,allow_nan=False,separators=(',',':'))
    if not store.WRITE_LOCK.acquire(timeout=3):
        raise HTTPException(503,'Storage writer busy')
    try:
        store.admission(payload.encode('utf-8'))
        with store.connect() as db:
            if record['state']=='running' or record.get('receipt') is not None:
                store.ensure_run(db,record['test_id'],record['created_at'])
            db.execute('INSERT INTO experiments(test_id,created_at,state,record) VALUES(?,?,?,?) '
                       'ON CONFLICT(test_id) DO UPDATE SET state=excluded.state,record=excluded.record',
                       (record['test_id'],record['created_at'],record['state'],payload))
    except store.IngestError as exc:
        raise HTTPException(exc.status,exc.reason)
    finally:
        store.WRITE_LOCK.release()


def error_text(exc):
    return str(getattr(exc,'detail',exc))[:1000]


def identity(status):
    config = status.get('config')
    return config.get('test_id') if isinstance(config,dict) else None


@router.get('/experiment')
def experiment():
    record = latest()
    try:
        status = gateway('/status')
        return {'reachable':True,'generator':status,'experiment':record,'error':None}
    except (OSError,ValueError) as exc:
        return {'reachable':False,'generator':None,'experiment':record,'error':error_text(exc)}


@router.post('/experiment/start')
def start(config: StartConfig):
    if not CONTROL_LOCK.acquire(blocking=False):
        raise HTTPException(409,'Experiment control is busy')
    try:
        if latest(active_only=True):
            raise HTTPException(409,'An application experiment is still active; wait for its final receipt')
        try:
            status = gateway('/status')
        except (OSError,ValueError) as exc:
            raise HTTPException(503,'Generator unavailable: '+error_text(exc))
        if status.get('state') not in ('idle','finished'):
            raise HTTPException(409,'Generator is already occupied; external runs are never interrupted')
        run = 'ui-'+time.strftime('%Y%m%dT%H%M%S',time.gmtime())+'-'+uuid.uuid4().hex[:8]
        params = {'workload':'objects','target_url':GENERATOR_TARGET_URL,'test_id':run,
                  'seed':config.seed,'batch_size':config.batch_size,'workers':config.workers,
                  'objects_per_second':config.rate,'rate_profile':config.profile,
                  'duration_seconds':config.duration,'ramp_seconds':min(5,config.duration//2),
                  'max_batches':min(1000000,max(20000,math.ceil(config.rate*config.duration/config.batch_size)*2)),
                  'retries':2,'objects':{'scenario':config.scenario,'tree_every_batches':10}}
        record = {'test_id':run,'state':'starting','config':params,'created_at':time.time(),
                  'finished_at':None,'receipt':None,'reconciliation':None,'error':None}
        save(record)  # Persist intent before the non-idempotent network action.
        try:
            gateway('/start',params)
        except urllib.error.HTTPError as exc:
            if 400<=exc.code<500:
                record.update(state='failed',finished_at=time.time(),error='Start rejected: '+error_text(exc))
                save(record)
                raise HTTPException(409 if exc.code==409 else 502,record['error'])
            record['error']='Start outcome unknown; checking status without retrying: '+error_text(exc)
            save(record)
            return JSONResponse(record,status_code=202)
        except (OSError,ValueError) as exc:
            record['error']='Start outcome unknown; checking status without retrying: '+error_text(exc)
            save(record)
            return JSONResponse(record,status_code=202)
        record['state']='running'
        save(record)
        return {'test_id':run,'state':'running','config':params}
    finally:
        CONTROL_LOCK.release()


@router.post('/experiment/stop')
def stop(config: StopConfig):
    if not CONTROL_LOCK.acquire(blocking=False):
        raise HTTPException(409,'Experiment control is busy')
    try:
        record=latest(active_only=True)
        if not record or record['test_id']!=config.test_id:
            raise HTTPException(409,'Stop is only allowed for the active application experiment')
        try:
            status=gateway('/status')
        except (OSError,ValueError) as exc:
            raise HTTPException(503,'Cannot verify generator ownership: '+error_text(exc))
        if identity(status)!=config.test_id:
            raise HTTPException(409,'Generator run does not match; refusing to stop an external experiment')
        record['state']='stopping'
        save(record)
        if status.get('state')!='finished':
            try:
                gateway('/stop',{})
            except (OSError,ValueError) as exc:
                record['error']='Stop outcome unknown; awaiting status: '+error_text(exc)
                save(record)
                return JSONResponse(record,status_code=202)
        return {'test_id':config.test_id,'state':'stopping'}
    finally:
        CONTROL_LOCK.release()


def finish(record):
    stats = record['receipt'].get('stats', {})
    generator_state = stats.get('state')
    reason = stats.get('finish_reason')
    record.update(state='completed', finished_at=time.time(), error=None)
    if generator_state == 'failed' or (isinstance(reason, str) and 'error' in reason):
        record.update(state='failed', error=f'Generator failed: {generator_state}; {reason}')
    elif record['reconciliation'].get('ok') is not True:
        record['error'] = 'Experiment finished, but one or more reconciliation checks failed'
    save(record)


def poll_once():
    if not CONTROL_LOCK.acquire(blocking=False):
        return
    try:
        record=latest(active_only=True)
        if not record:
            return
        try:
            if record.get('receipt') is not None:
                record['reconciliation']=reconcile_and_store(record['receipt'])
                finish(record)
                return
            status=gateway('/status')
            if identity(status)!=record['test_id']:
                if record['state']=='starting' and time.time()-record['created_at']<5:
                    return
                record.update(state='failed',finished_at=time.time(),
                              error='Generator no longer identifies this experiment; no start or stop was retried')
                save(record)
                return
            if status.get('state')!='finished':
                if record['state']=='starting':
                    record.update(state='running',error=None)
                    save(record)
                return
            receipt=gateway('/receipt')
            if identity(receipt)!=record['test_id']:
                record.update(state='failed',finished_at=time.time(),error='Receipt identity mismatch; refusing reconciliation')
                save(record)
                return
            record['receipt']=receipt
            save(record)  # A crash during reconciliation can resume from the retained receipt.
            record['reconciliation']=reconcile_and_store(receipt)
            finish(record)
        except (OSError,ValueError,HTTPException) as exc:
            # Preserve active state on transport/storage errors, and retry observation, never start.
            message=error_text(exc)
            if isinstance(exc,HTTPException) and 400<=exc.status_code<500:
                record.update(state='failed',finished_at=time.time())
            if record.get('error')!=message:
                record['error']=message
                save(record)
    finally:
        CONTROL_LOCK.release()


async def monitor():
    while True:
        try:
            await run_in_threadpool(poll_once)
        except Exception:
            # Database interruptions must not permanently kill restart recovery.
            import logging
            logging.getLogger(__name__).exception('Experiment monitor iteration failed')
        await asyncio.sleep(1)
