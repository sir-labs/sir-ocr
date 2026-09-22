import json
import logging
import asyncio
import subprocess
import sys
import hashlib
import ipaddress
import os
import tempfile
import time
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, unquote
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from . import classification as cl, config as cfg, dataset, db
from .artifacts import image_refs

STATIC = Path(__file__).parent / 'static'

@asynccontextmanager
async def lifespan(app):
    db.init()
    app.state.validation_slots = asyncio.Semaphore(2)
    async def sync_loop():
        while True:
            await asyncio.sleep(15)
            await run_in_threadpool(cl.sweep_exports)
    sync_task = asyncio.create_task(sync_loop())
    try:
        yield
    finally:
        sync_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sync_task

app = FastAPI(title='SIR OCR', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

def is_trusted_proxy(value):
    try:
        address = ipaddress.ip_address(value)
        return any(address in ipaddress.ip_network(n.strip())
                   for n in os.getenv('OCR_TRUSTED_PROXIES', '').split(',') if n.strip())
    except ValueError:
        return False

def client_ip(request):
    peer = request.client.host if request.client else 'unknown'
    if not is_trusted_proxy(peer):
        return peer
    # Walk from the nearest hop. Never trust the arbitrary leftmost value.
    hops = [s.strip() for s in request.headers.get('x-forwarded-for','').split(',') if s.strip()]
    for value in reversed(hops):
        if not is_trusted_proxy(value):
            try:
                return str(ipaddress.ip_address(value))
            except ValueError:
                return peer
    return peer

class BodyTooLarge(Exception):
    pass

class LimitsMiddleware:
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request = Request(scope)
        expected = os.getenv('OCR_PUBLIC_ORIGIN', 'http://localhost:8000')
        # nginx's transport is HTTP even for public HTTPS. CF-Visitor preserves
        # the original scheme; accept it only from the configured proxy peers.
        if expected.startswith('https://') and request.client and is_trusted_proxy(request.client.host):
            try:
                visitor = json.loads(request.headers.get('cf-visitor', '{}'))
            except (ValueError, TypeError):
                visitor = {}
            if isinstance(visitor, dict) and visitor.get('scheme') == 'http':
                target = expected.rstrip('/') + request.url.path
                if request.url.query:
                    target += '?' + request.url.query
                return await RedirectResponse(target, status_code=308)(scope, receive, send)
        try:
            if scope['method']=='POST':
                origin = request.headers.get('origin')
                if origin and origin != expected:
                    # Only classify headers; never log arbitrary URLs or capabilities.
                    origin_kind = 'null' if origin == 'null' else 'other'
                    logging.getLogger('uvicorn.error').warning(
                        'origin_rejected origin_kind=%s', origin_kind)
                    raise HTTPException(403, 'Cross-origin requests are disabled.')
                # Cancellation must remain available during low disk or admission throttling.
                if not scope['path'].endswith('/cancel'):
                    await run_in_threadpool(db.rate_limit, client_ip(request))
                    await run_in_threadpool(db.free_space)
                try:
                    length = int(request.headers.get('content-length', '0'))
                except ValueError:
                    raise HTTPException(400, 'Invalid Content-Length.')
                if length > cfg.MAX_BYTES + 65536:
                    raise BodyTooLarge()
            total = 0
            async def limited_receive():
                nonlocal total
                message = await receive()
                total += len(message.get('body', b''))
                if total > cfg.MAX_BYTES + 65536:
                    raise BodyTooLarge()
                return message
            async def safe_send(message):
                if message['type']=='http.response.start':
                    message['headers'] += [(b'cache-control',b'no-store'),(b'referrer-policy',b'strict-origin'),
                        (b'x-content-type-options',b'nosniff'),
                        (b'content-security-policy',b"default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
                await send(message)
            await self.app(scope, limited_receive, safe_send)
        except BodyTooLarge:
            await JSONResponse({'detail':'PDF exceeds 50 MiB.'},status_code=413)(scope,receive,send)
        except HTTPException as e:
            await JSONResponse({'detail':e.detail},status_code=e.status_code,headers=e.headers)(scope,receive,send)

app.add_middleware(LimitsMiddleware)

def bearer(request):
    auth = request.headers.get('authorization', '')
    return auth[7:] if auth.startswith('Bearer ') else ''

def signed_in_owner(request):
    # Only nginx may assert identity. A job capability alone is not a user ID.
    if request.client and is_trusted_proxy(request.client.host):
        return request.headers.get('x-auth-user-id','')[:200]
    return ''

def validate(path):
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'app.pdf_validation', str(path), str(cfg.MAX_PAGES), str(cfg.CONFIG['dpi'])],
            capture_output=True, text=True, timeout=30,
        )
        response = json.loads(result.stdout)
        if result.returncode or 'page_count' not in response:
            raise HTTPException(400, response.get('error', 'PDF validation failed.'))
        return response['page_count']
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        raise HTTPException(400, 'PDF validation exceeded its time or memory limit.')

@app.get('/healthz')
def health():
    with db.connect() as c:
        c.execute('SELECT 1').fetchone()
    return {'status':'ok'}

@app.post('/api/jobs', status_code=201)
async def create_job(request: Request):
    path = None
    try:
        async with request.form(max_files=1,max_fields=0,max_part_size=cfg.MAX_BYTES) as form:
            upload = form.get('file')
            if upload is None or not hasattr(upload,'read'):
                raise HTTPException(400, 'Upload a PDF in the file field.')
            digest, size = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(dir=cfg.DATA/'incoming', suffix='.pdf', delete=False) as f:
                path = Path(f.name)
                while chunk := await upload.read(1024*1024):
                    size += len(chunk)
                    if size > cfg.MAX_BYTES:
                        raise HTTPException(413, 'PDF exceeds 50 MiB.')
                    digest.update(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
        async with app.state.validation_slots:
            count = await run_in_threadpool(validate,path)
        return await run_in_threadpool(db.register,path,digest.hexdigest(),count,client_ip(request))
    finally:
        if path:
            path.unlink(missing_ok=True)

def save_to_dataset(ident, token, owner_id):
    """Copy a finished result into the caller's sir-data, once.

    ponytail: triggered by the status poll, which is the only moment both the finished result
    and the signed-in user are known here (a result is shared between users; the worker knows
    neither). A user who closes the tab before the job finishes never triggers it — add a
    sweep over jobs if that turns out to matter.
    """
    key = db.result_key(ident, token)
    if not db.claim_push(key, owner_id):
        return
    try:
        dataset.push_result(owner_id, key, cfg.DATA / 'results' / key)
    except Exception:
        db.release_push(key, owner_id)
        logging.getLogger('uvicorn.error').exception('dataset_push_failed key=%s', key[:12])


@app.get('/api/jobs/{ident}')
def get_job(ident: str, request: Request, response: Response, background: BackgroundTasks):
    token = bearer(request)
    result = db.status(ident, token)
    owner_id = signed_in_owner(request)
    if owner_id and dataset.enabled():
        cl.register_owner(db.result_key(ident,token),owner_id)
    if result['state'] == 'completed' and owner_id and dataset.enabled():
        background.add_task(save_to_dataset, ident, token, owner_id)
    # Native file downloads cannot add an Authorization header. Grant only this
    # job's download path a Secure/HttpOnly capability after bearer verification.
    response.set_cookie(
        'ocr_download', token, max_age=86400,
        path=f'/api/jobs/{ident}/download', httponly=True, samesite='strict',
        secure=os.getenv('OCR_PUBLIC_ORIGIN', '').startswith('https://'),
    )
    response.set_cookie('ocr_preview', token, max_age=86400, path=f'/api/jobs/{ident}/preview',
                        httponly=True, samesite='strict', secure=os.getenv('OCR_PUBLIC_ORIGIN','').startswith('https://'))
    return result

@app.get('/api/jobs/{ident}/classification')
def classification_status(ident: str, request: Request, background: BackgroundTasks):
    key = db.result_key(ident,bearer(request))
    cl.enqueue(key)
    owner = signed_in_owner(request)
    if owner and dataset.enabled():
        cl.register_owner(key,owner)
    result = cl.status(key,owner)
    result['can_personalize'] = bool(owner and dataset.enabled())
    result['storage'] = cl.export_state(key,owner) if dataset.enabled() else 'not_configured'
    # Avoid persisting a new annotation on every per-page progress poll.
    if owner and dataset.enabled() and (result['state'] in ('done','partial','paused') or (result['personal'] and result['personal']['review'])):
        background.add_task(cl.sync_to_dataset,key,owner)
    return result

@app.post('/api/jobs/{ident}/classification/retry')
def classification_retry(ident: str, request: Request):
    key = db.result_key(ident,bearer(request))
    with db.connect(True) as c:
        cancelled = c.execute('SELECT 1 FROM job_cancellations WHERE job_id=?',(ident,)).fetchone()
        if cancelled:
            raise HTTPException(409,'งานนี้ถูกยกเลิกแล้ว')
        c.execute("UPDATE page_classifications SET state='queued',error=NULL,updated=? WHERE result_key=? AND version=? AND state='failed'",
                  (time.time(),key,cl.VERSION))
    return {'state':'queued'}

async def personal_request(ident,request):
    key = db.result_key(ident,bearer(request))
    owner = signed_in_owner(request)
    if not owner:
        raise HTTPException(401,'เข้าสู่ระบบเพื่อเก็บหมวดและปลายทางส่วนตัว')
    if not dataset.enabled():
        raise HTTPException(503,'ยังไม่ได้เชื่อมต่อคลังข้อมูลส่วนตัว')
    raw = await request.body()
    if len(raw)>20000:
        raise HTTPException(413,'ข้อมูลหมวดมีขนาดใหญ่เกินไป')
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(422,'JSON ไม่ถูกต้อง')
    if not isinstance(payload,dict):
        raise HTTPException(422,'JSON ต้องเป็น object')
    return key,owner,payload

@app.post('/api/jobs/{ident}/classification/folders',status_code=202)
async def classification_folders(ident: str, request: Request):
    key,owner,payload = await personal_request(ident,request)
    await run_in_threadpool(cl.queue_personal,key,owner,payload.get('folders'))
    await run_in_threadpool(cl.enqueue,key)
    return {'state':'queued'}

@app.post('/api/jobs/{ident}/classification/review')
async def classification_review(ident: str, request: Request, background: BackgroundTasks):
    key,owner,payload = await personal_request(ident,request)
    saved = await run_in_threadpool(cl.review,key,owner,payload)
    background.add_task(cl.sync_to_dataset,key,owner)
    return {'review':saved,'storage':'pending'}

@app.post('/api/jobs/{ident}/retry')
def retry_job(ident: str, request: Request):
    db.retry(ident,bearer(request),client_ip(request))
    return db.status(ident,bearer(request))

@app.get('/api/jobs/{ident}/download')
def download(ident: str, request: Request):
    with db.connect() as c:
        token = bearer(request) if request.headers.get('authorization') else request.cookies.get('ocr_download', '')
        j = db.authorize(c,ident,token)
        r = c.execute('SELECT state FROM results WHERE key=?',(j['result_key'],)).fetchone()
        if r['state'] != 'completed':
            raise HTTPException(409,'All pages must succeed before downloading.')
        path = cfg.DATA/'results'/j['result_key']/'result.zip'
    return FileResponse(path,media_type='application/zip',filename='ocr-result.zip')

@app.post('/api/jobs/{ident}/cancel')
def cancel_job(ident: str, request: Request):
    db.cancel(ident,bearer(request))
    return db.status(ident,bearer(request))

def preview_page(ident, number, token):
    with db.connect() as c:
        j = db.authorize(c,ident,token)
        page = c.execute('SELECT state FROM pages WHERE result_key=? AND number=?',(j['result_key'],number)).fetchone()
        if not page:
            raise HTTPException(404,'Page not found.')
        if page['state'] != 'done':
            raise HTTPException(409,'This page is not finished yet.')
        base = cfg.DATA/'results'/j['result_key']/'pages'/f'{number:04d}'
    markdown = (base/'page.md').read_text()
    images = {}
    for ref in image_refs(markdown):
        path = (base/unquote(ref)).resolve()
        if path.is_relative_to(base.resolve()) and path.is_file() and path.suffix.lower() in ('.png','.jpg','.jpeg','.webp','.gif'):
            images[hashlib.sha256(ref.encode()).hexdigest()] = (ref,path)
    return markdown,images

@app.get('/api/jobs/{ident}/preview/pages/{number}')
def read_page(ident: str, number: int, request: Request):
    markdown, images = preview_page(ident,number,bearer(request))
    return {'number':number,'markdown':markdown,'images':{
        ref:f'/api/jobs/{ident}/preview/pages/{number}/images/{key}'
        for key,(ref,path) in images.items()}}

@app.get('/api/jobs/{ident}/preview/pages/{number}/images/{image_id}')
def read_image(ident: str, number: int, image_id: str, request: Request):
    token = bearer(request) if request.headers.get('authorization') else request.cookies.get('ocr_preview','')
    _,images = preview_page(ident,number,token)
    if image_id not in images:
        raise HTTPException(404,'Image not found.')
    return FileResponse(images[image_id][1])

@app.get('/view')
def viewer():
    return FileResponse(STATIC/'viewer.html')

@app.get('/')
def index():
    return FileResponse(STATIC/'index.html')

app.mount('/static', StaticFiles(directory=STATIC), name='static')
