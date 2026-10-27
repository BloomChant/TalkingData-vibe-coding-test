#!/usr/bin/env python3
"""Run a bounded official-generator workload and save its independently returned receipt."""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url, payload=None, timeout=15):
    body=None if payload is None else json.dumps(payload).encode()
    req=urllib.request.Request(url,data=body,headers={'Content-Type':'application/json'})
    try:
        with OPENER.open(req,timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail=exc.read().decode(errors='replace')
        raise RuntimeError(f'{req.get_method()} {url}: HTTP {exc.code}: {detail}') from exc


def save(path, value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    temporary.replace(path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generator',default='http://127.0.0.1:9001')
    parser.add_argument('--app',default='http://127.0.0.1:8000')
    parser.add_argument('--target',default='http://host.docker.internal:8000/ingest')
    parser.add_argument('--rate',type=int,default=200,help='Objects/sec; 0 means maximum-speed sending, not pause')
    parser.add_argument('--duration',type=int,default=120)
    parser.add_argument('--batch-size',type=int,default=20)
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--scenario',choices=['mixed','evolving','stress'],default='evolving')
    parser.add_argument('--profile',choices=['steady','bursty'],default='bursty')
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--tree-every-batches',type=int,default=10)
    parser.add_argument('--max-batches',type=int,default=20000)
    parser.add_argument('--retries',type=int,default=2)
    parser.add_argument('--ramp-seconds',type=int,default=10)
    parser.add_argument('--test-id',default=None)
    parser.add_argument('--output',type=Path,default=Path('results'))
    args=parser.parse_args()
    if args.rate<0 or args.duration<1 or args.batch_size<1 or args.workers<1 or args.max_batches<1:
        parser.error('rate must be >=0; duration, batch-size, workers and max-batches must be positive')
    run=args.test_id or 'run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:6]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}',run): parser.error('test-id must match [A-Za-z0-9_-]{1,48}')
    directory=args.output/run
    if directory.exists(): parser.error(f'{directory} already exists; use a fresh test-id')
    directory.mkdir(parents=True)
    cfg={'workload':'objects','target_url':args.target,'test_id':run,'seed':args.seed,
         'batch_size':args.batch_size,'workers':args.workers,'objects_per_second':args.rate,
         'rate_profile':args.profile,'ramp_seconds':args.ramp_seconds,'duration_seconds':args.duration,
         'max_batches':args.max_batches,'retries':args.retries,
         'objects':{'scenario':args.scenario,'tree_every_batches':args.tree_every_batches}}
    save(directory/'request-config.json',cfg)
    started=False
    try:
        request(args.app.rstrip('/')+'/health')
        status=request(args.generator.rstrip('/')+'/status')
        if status.get('state') not in ('idle','finished'):
            raise RuntimeError('Generator is already running; this script will not interrupt another run')
        start=request(args.generator.rstrip('/')+'/start',cfg)
        started=True
        save(directory/'start.json',start)
        print(f'Started {run}; target={args.rate}/s, duration={args.duration}s',flush=True)
        deadline=time.monotonic()+args.duration+max(60,args.duration//2)
        last_print=-1
        while True:
            status=request(args.generator.rstrip('/')+'/status')
            save(directory/'status.json',status)
            if status.get('state')=='finished': break
            elapsed=int(status.get('stats',{}).get('elapsed_seconds',0))
            if elapsed//5!=last_print:
                last_print=elapsed//5
                stats=status.get('stats',{})
                print(f"{elapsed}s: acknowledged={stats.get('acknowledged_objects',0)}, retries={stats.get('retries',0)}, timeouts={stats.get('timeouts',0)}",flush=True)
            if time.monotonic()>deadline: raise TimeoutError('Generator did not finish before the bounded deadline')
            time.sleep(1)
        receipt=request(args.generator.rstrip('/')+'/receipt')
        save(directory/'receipt.json',receipt)
        if receipt.get('config',{}).get('test_id')!=run:
            raise RuntimeError('Receipt belongs to another run; refusing to reconcile')
        reconciliation=request(args.app.rstrip('/')+'/api/reconcile',receipt,timeout=60)
        save(directory/'reconcile.json',reconciliation)
        system=request(args.app.rstrip('/')+'/api/system')
        save(directory/'system.json',system)
        overview=request(args.app.rstrip('/')+'/api/overview?run='+run)
        save(directory/'overview.json',overview)
        stats=receipt.get('stats',{})
        elapsed=stats.get('elapsed_seconds',0)
        summary={'test_id':run,'elapsed_seconds':elapsed,
                 'generator_acknowledged_main_objects':stats.get('acknowledged_objects',0),
                 'acknowledged_main_objects_per_second':stats.get('acknowledged_objects',0)/elapsed if elapsed else None,
                 'generator_main_request_average_latency_ms':stats.get('average_latency_ms'),
                 'generator_main_request_max_latency_ms':stats.get('max_latency_ms'),
                 'reconciliation_ok':reconciliation.get('ok'),
                 'note':'Generator main-object counters exclude extra fault/duplicate injections. This is one measured run, not a guaranteed capacity.'}
        save(directory/'summary.json',summary)
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        print(f'Results: {directory}')
        return 0 if reconciliation.get('ok') is True else 2
    except (OSError,ValueError,RuntimeError,TimeoutError,KeyboardInterrupt) as exc:
        save(directory/'error.json',{'error':str(exc),'run_started':started})
        print(f'Failed: {exc}; evidence retained in {directory}',file=sys.stderr)
        if started:
            try:
                current=request(args.generator.rstrip('/')+'/status')
                if current.get('config',{}).get('test_id')==run and current.get('state')!='finished':
                    request(args.generator.rstrip('/')+'/stop',{})
            except Exception: pass
        return 1


if __name__=='__main__':
    raise SystemExit(main())
