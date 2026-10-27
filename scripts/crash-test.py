#!/usr/bin/env python3
"""Exercise one bounded generator run across a deliberate application SIGKILL.

This command intentionally kills only the named application container. Run it with
exclusive access to the generator. It never removes containers or data volumes.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url,payload=None,timeout=15):
    body=None if payload is None else json.dumps(payload).encode()
    req=urllib.request.Request(url,data=body,headers={'Content-Type':'application/json'})
    try:
        with OPENER.open(req,timeout=timeout) as response: return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'{req.get_method()} {url}: HTTP {exc.code}: {exc.read().decode(errors="replace")}') from exc


def save(path,value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    temporary.replace(path)


def docker(args,*command):
    result=subprocess.run([args.docker,*command],capture_output=True,text=True,timeout=60)
    if result.returncode:
        raise RuntimeError(f'docker {command[0]} failed: {result.stderr.strip()}')
    return result.stdout.strip()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--generator',default='http://127.0.0.1:9001')
    p.add_argument('--app',default='http://127.0.0.1:8000')
    p.add_argument('--target',default='http://host.docker.internal:8000/ingest')
    p.add_argument('--container',default='observatory-app-1')
    p.add_argument('--docker',default='docker',help='Docker executable or absolute path, not a shell command')
    p.add_argument('--duration',type=int,default=40)
    p.add_argument('--rate',type=int,default=500)
    p.add_argument('--batch-size',type=int,default=20)
    p.add_argument('--workers',type=int,default=4)
    p.add_argument('--kill-after',type=float,default=8,help='Minimum elapsed seconds before killing; also requires a successful ACK')
    p.add_argument('--test-id')
    p.add_argument('--output',type=Path,default=Path('results'))
    a=p.parse_args()
    if a.duration<15 or not 0<=a.kill_after<a.duration-5:
        p.error('duration must be >=15 and kill-after must leave at least 5 seconds')
    if min(a.rate,a.batch_size,a.workers)<1: p.error('rate, batch-size and workers must be positive')
    run=a.test_id or 'crash-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:6]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}',run): p.error('test-id must match [A-Za-z0-9_-]{1,48}')
    directory=a.output/run
    if directory.exists(): p.error(f'{directory} already exists; choose a fresh test-id')
    directory.mkdir(parents=True)
    gen=a.generator.rstrip('/');app=a.app.rstrip('/')
    cfg={'workload':'objects','target_url':a.target,'test_id':run,'seed':42,
         'batch_size':a.batch_size,'workers':a.workers,'objects_per_second':a.rate,
         'rate_profile':'steady','ramp_seconds':2,'duration_seconds':a.duration,
         'max_batches':20000,'retries':2,'objects':{'scenario':'evolving','tree_every_batches':10}}
    save(directory/'request-config.json',cfg)
    started=False;kill_attempted=False;recovered=False;kill_at=None;recovery_seconds=None
    try:
        status=request(gen+'/status')
        if status.get('state') not in ('idle','finished'):
            raise RuntimeError('Generator is already running; refusing to interrupt another run')
        request(app+'/health')
        details=json.loads(docker(a,'inspect',a.container))[0]
        labels=details.get('Config',{}).get('Labels') or {}
        if labels.get('com.docker.compose.service')!='app':
            raise RuntimeError('Container is not labeled as the Compose app service; refusing to kill it')
        save(directory/'container-before.json',{
            'name':details.get('Name'),'image':details.get('Config',{}).get('Image'),
            'state':details.get('State',{}).get('Status'),
            'limits':{k:details.get('HostConfig',{}).get(k) for k in ['NanoCpus','Memory','MemorySwap']},
        })
        save(directory/'system-before.json',request(app+'/api/system'))
        save(directory/'start.json',request(gen+'/start',cfg))
        started=True
        print(f'Started {run}; will SIGKILL {a.container} after ACKs are observed',flush=True)
        deadline=time.monotonic()+a.duration+90
        while True:
            status=request(gen+'/status')
            if status.get('config',{}).get('test_id')!=run:
                raise RuntimeError('Generator changed to another run during crash test')
            stats=status.get('stats',{})
            elapsed=stats.get('elapsed_seconds',0)
            if not kill_attempted and stats.get('acknowledged_batches',0)>0 and elapsed>=a.kill_after:
                save(directory/'status-before-kill.json',status)
                save(directory/'overview-before-kill.json',request(app+'/api/overview?run='+run))
                save(directory/'system-at-kill.json',request(app+'/api/system'))
                kill_attempted=True;kill_at=time.monotonic()
                killed=docker(a,'kill','--signal=KILL',a.container)
                save(directory/'kill.json',{'container':a.container,'signal':'SIGKILL','docker_output':killed,'at_utc':datetime.now(timezone.utc).isoformat()})
                docker(a,'start',a.container)
                recovery_deadline=time.monotonic()+45
                while True:
                    try:
                        health=request(app+'/health',timeout=2)
                        if health.get('status')=='ok': break
                    except (OSError,ValueError,RuntimeError): pass
                    if time.monotonic()>recovery_deadline: raise TimeoutError('Application did not recover within 45 seconds')
                    time.sleep(.25)
                recovered=True;recovery_seconds=time.monotonic()-kill_at
                save(directory/'recovery.json',{'health':health,'seconds_to_health':recovery_seconds,
                    'meaning':'From issuing SIGKILL to first successful health response; includes Docker CLI overhead.'})
                save(directory/'system-after-recovery.json',request(app+'/api/system'))
                print(f'Application healthy again after {recovery_seconds:.3f}s',flush=True)
            if status.get('state')=='finished': break
            if time.monotonic()>deadline: raise TimeoutError('Generator run did not finish before deadline')
            time.sleep(1)
        save(directory/'status.json',status)
        if not kill_attempted or not recovered: raise RuntimeError('Run finished without completing the intended SIGKILL/recovery sequence')
        receipt=request(gen+'/receipt')
        if receipt.get('config',{}).get('test_id')!=run: raise RuntimeError('Receipt belongs to another run')
        save(directory/'receipt.json',receipt)
        result=request(app+'/api/reconcile',receipt,timeout=60)
        save(directory/'reconcile.json',result)
        save(directory/'overview.json',request(app+'/api/overview?run='+run))
        save(directory/'system-after.json',request(app+'/api/system'))
        missing=result.get('missing_batch_ids')
        missing_check=next((c for c in result.get('checks',[]) if c.get('name')=='已确认批次缺失数'),None)
        no_missing=isinstance(missing,list) and not missing and missing_check is not None and missing_check.get('actual')==0 and missing_check.get('status')=='pass'
        durability_passed=result.get('ok') is True and no_missing
        generator_stats=status.get('stats',{})
        generator_state=generator_stats.get('state')
        finish_reason=generator_stats.get('finish_reason')
        stream_completed=generator_state=='completed' and finish_reason=='duration'
        passed=durability_passed and stream_completed
        summary={'test_id':run,'container':a.container,'sigkill_performed':True,'recovered':recovered,
                 'seconds_to_health':recovery_seconds,'acknowledged_batches_before_kill':json.loads((directory/'status-before-kill.json').read_text()).get('stats',{}).get('acknowledged_batches'),
                 'acknowledged_batches_final':receipt.get('stats',{}).get('acknowledged_batches'),
                 'acknowledged_missing_zero':no_missing,'reconciliation_ok':result.get('ok'),
                 'reconciliation_complete':result.get('complete'),
                 'generator_state':generator_state,'finish_reason':finish_reason,
                 'elapsed_seconds':generator_stats.get('elapsed_seconds'),
                 'durability_passed':durability_passed,'stream_completed':stream_completed,'passed':passed,
                 'unknown_checks':[c.get('name') for c in result.get('checks',[]) if c.get('status')=='unknown'],
                 'scope':'Process SIGKILL and container restart, not host power-loss or hardware failure. Durability and uninterrupted completion are evaluated separately; passed requires both.'}
        save(directory/'summary.json',summary)
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        print(f'Results: {directory}')
        return 0 if passed else 2
    except (OSError,ValueError,RuntimeError,TimeoutError,subprocess.SubprocessError,KeyboardInterrupt) as exc:
        save(directory/'error.json',{'error':str(exc),'run_started':started,'kill_attempted':kill_attempted,'recovered':recovered})
        print(f'Failed: {exc}; evidence retained in {directory}',file=sys.stderr)
        if kill_attempted and not recovered:
            try: docker(a,'start',a.container)
            except Exception: pass
        if started:
            try:
                current=request(gen+'/status')
                if current.get('config',{}).get('test_id')==run and current.get('state')!='finished': request(gen+'/stop',{})
            except Exception: pass
        return 1


if __name__=='__main__': raise SystemExit(main())
