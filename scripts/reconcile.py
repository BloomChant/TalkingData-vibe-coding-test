#!/usr/bin/env python3
"""Submit a saved generator receipt to the local observatory for reconciliation."""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipt',type=Path)
    parser.add_argument('--app',default='http://127.0.0.1:8000')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        payload=args.receipt.read_bytes()
        if len(payload)>2*1024**2: raise ValueError('Receipt exceeds 2 MiB limit')
        json.loads(payload)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request=urllib.request.Request(args.app.rstrip('/')+'/api/reconcile',data=payload,
                                       headers={'Content-Type':'application/json'})
        with opener.open(request,timeout=60) as response:
            result=json.load(response)
        rendered=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(rendered)
        print(rendered,end='')
        return 0 if result.get('ok') is True else 2
    except (OSError,ValueError) as exc:
        if isinstance(exc,urllib.error.HTTPError):
            print(exc.read().decode(errors='replace'),file=sys.stderr)
        print(str(exc),file=sys.stderr)
        return 1


if __name__=='__main__': raise SystemExit(main())
