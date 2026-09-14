import hashlib
import ipaddress
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
import pymupdf as fitz
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from . import config as cfg, db

STATIC = Path(__file__).parent / 'static'

@asynccontextmanager
async def lifespan(app):
    db.init()
    yield

app = FastAPI(title='SIR OCR', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

def client_ip(request):
    peer = request.client.host if request.client else 'unknown'
    trusted = [ipaddress.ip_network(s.strip()) for s in os.getenv('OCR_TRUSTED_PROXIES', '').split(',') if s.strip()]
    def is_trusted(value):
        try:
            return any(ipaddress.ip_address(value) in n for n in trusted)
        except ValueError:
            return False
    if not is_trusted(peer):
        return peer
    # Walk from the nearest hop. Never trust the arbitrary leftmost value.
    hops = [s.strip() for s in request.headers.get('x-forwarded-for','').split(',') if s.strip()]
    for value in reversed(hops):
        if not is_trusted(value):
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
        try:
            if scope['method']=='POST':
                origin = request.headers.get('origin')
                expected = os.getenv('OCR_PUBLIC_ORIGIN', 'http://localhost:8000')
                if origin and origin != expected:
                    raise HTTPException(403, 'Cross-origin requests are disabled.')
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
                    message['headers'] += [(b'cache-control',b'no-store'),(b'referrer-policy',b'no-referrer'),
                        (b'x-content-type-options',b'nosniff'),
                        (b'content-security-policy',b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
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

def validate(path):
    try:
        with path.open('rb') as f:
            if not f.read(1024).lstrip().startswith(b'%PDF-'):
                raise ValueError('Not a PDF.')
        with fitz.open(path) as pdf:
            if pdf.needs_pass or pdf.is_encrypted:
                raise ValueError('Password-protected PDFs are not accepted.')
            if pdf.is_repaired:
                raise ValueError('PDF is damaged. Please export a valid PDF.')
            if not 1 <= len(pdf) <= cfg.MAX_PAGES:
                raise ValueError('PDF must contain 1–500 pages.')
            for page in pdf:
                if page.rect.width <= 0 or page.rect.height <= 0:
                    raise ValueError('Invalid page dimensions.')
                if page.rect.width * page.rect.height * (cfg.CONFIG['dpi']/72)**2 > 40_000_000:
                    raise ValueError('A page exceeds the 40 megapixel render safety limit at 200 DPI.')
            return len(pdf)
    except (ValueError, RuntimeError, fitz.FileDataError) as e:
        raise HTTPException(400, str(e))

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
        count = await run_in_threadpool(validate,path)
        return await run_in_threadpool(db.register,path,digest.hexdigest(),count,client_ip(request))
    finally:
        if path:
            path.unlink(missing_ok=True)

@app.get('/api/jobs/{ident}')
def get_job(ident: str, request: Request):
    return db.status(ident,bearer(request))

@app.post('/api/jobs/{ident}/retry')
def retry_job(ident: str, request: Request):
    db.retry(ident,bearer(request),client_ip(request))
    return db.status(ident,bearer(request))

@app.get('/api/jobs/{ident}/download')
def download(ident: str, request: Request):
    with db.connect() as c:
        j = db.authorize(c,ident,bearer(request))
        r = c.execute('SELECT state FROM results WHERE key=?',(j['result_key'],)).fetchone()
        if r['state'] != 'completed':
            raise HTTPException(409,'All pages must succeed before downloading.')
        path = cfg.DATA/'results'/j['result_key']/'result.zip'
    return FileResponse(path,media_type='application/zip',filename='ocr-result.zip')

@app.get('/')
def index():
    return FileResponse(STATIC/'index.html')

app.mount('/static', StaticFiles(directory=STATIC), name='static')
