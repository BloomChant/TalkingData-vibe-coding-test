import asyncio
import json
import os
import sqlite3
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import storage as store
from .protocol import validate
from .queries import router
from .experiments import router as experiment_router, monitor

MAX_BODY=int(os.environ.get('MAX_BODY_BYTES',8*1024**2))
STATIC=Path(os.environ.get('STATIC_DIR',str(Path(__file__).resolve().parents[2]/'web'/'dist')))


@asynccontextmanager
async def lifespan(app):
    store.init()
    task=asyncio.create_task(monitor())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app=FastAPI(title='Universe Observatory',version='1.0.0',lifespan=lifespan)
app.include_router(router,prefix='/api')
app.include_router(experiment_router,prefix='/api')


@app.get('/health')
def health():
    with store.connect() as db: db.execute('SELECT 1').fetchone()
    return {'status':'ok','version':'1.0.0'}


@app.exception_handler(sqlite3.Error)
async def storage_exception(request,exc):
    return JSONResponse({'detail':'Database unavailable; please retry'},status_code=503)


def decode(body):
    def invalid_constant(value): raise ValueError('Non-finite JSON number')
    msg=json.loads(body,parse_constant=invalid_constant)
    # Reject overflowed numeric literals and isolated Unicode surrogates before SQL.
    store.canonical(msg).encode('utf-8')
    return msg


@app.post('/ingest')
async def ingest(request:Request):
    body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>MAX_BODY: raise HTTPException(413,'Maximum request size is 8 MiB')
        body.extend(chunk)
    body=bytes(body);msg=None
    headers={k:v for k,v in request.headers.items() if k.startswith('x-flightgen-') or k in ('idempotency-key','x-idempotency-key','content-type')}
    try:
        try: msg=await run_in_threadpool(decode,body)
        except (ValueError,UnicodeError,RecursionError,OverflowError): raise store.IngestError(400,'Invalid JSON document')
        await run_in_threadpool(validate,msg)
        result=await run_in_threadpool(store.ingest,body,msg,headers)
        return JSONResponse(result,status_code=200 if result['duplicate'] else 202)
    except store.IngestError as exc:
        if exc.status in (400,409,422):
            try: await run_in_threadpool(store.quarantine,body,msg,headers,exc.reason)
            except (sqlite3.Error,OSError,store.IngestError):
                raise HTTPException(503,'Could not persist quarantine record')
        return JSONResponse({'detail':exc.reason},status_code=exc.status,
                            headers={'Retry-After':'1'} if exc.status in (429,503) else None)


@app.get('/{path:path}')
def frontend(path: str):
    if path.startswith('api/') or path in ('ingest','api'):
        raise HTTPException(404,'Unknown API route')
    root=STATIC.resolve()
    candidate=(root/path).resolve()
    if candidate.is_relative_to(root) and candidate.is_file():
        return FileResponse(candidate)
    index=root/'index.html'
    if index.is_file(): return FileResponse(index)
    return JSONResponse({'status':'backend_ready','frontend':'Awaiting Claude web/dist build','api_docs':'/docs'})
