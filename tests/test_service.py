import concurrent.futures
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path
import pymupdf as fitz
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app import config as cfg, db
from app.api import app, client_ip
from app.artifacts import finish_page, package, normalize_math
from app.worker import recover, process_job, InferenceFailure, STOP
from starlette.requests import Request

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(cfg,'DATA',tmp_path)
    monkeypatch.setattr(cfg,'MIN_FREE',0)
    STOP.clear()
    with TestClient(app) as client:
        yield client

def pdf(text='Hello OCR',pages=1):
    d=fitz.open()
    for n in range(pages):
        d.new_page().insert_text((72,72),f'{text} page {n+1}')
    return d.tobytes()

def submit(c,blob=None):
    return c.post('/api/jobs',files={'file':('input.pdf',blob or pdf(),'application/pdf')})

def headers(j):
    return {'Authorization':f"Bearer {j['token']}"}

def result_row(j):
    with db.connect() as c:
        return dict(c.execute('SELECT r.* FROM results r JOIN jobs j ON j.result_key=r.key WHERE j.id=?',(j['id'],)).fetchone())

class FakeEngine:
    def __init__(self,fail=None):
        self.calls=[]
        self.fail=fail
        self.closed=0
    def ensure(self,key):
        pass
    def close(self):
        self.closed+=1
    def predict(self,source,staging):
        n=int(staging.name.split('-')[1].split('.')[0])
        self.calls.append(n)
        if self.fail and self.fail(n,self.calls.count(n)):
            raise InferenceFailure('gpu_out_of_memory')
        raw=staging/'raw';raw.mkdir()
        (raw/'imgs').mkdir()
        (raw/'imgs'/'same.png').write_bytes(b'test-image')
        (raw/'raw.md').write_text(f'Page {n}: \\(x^2\\)\n<img src="imgs/same.png">')
        (raw/'raw.json').write_text(json.dumps({'page':n}))
        finish_page(staging,0.01,{'model':'hash'})
        return {'seconds':0.01,'model_hashes':{'model':'hash'}}

def test_upload_token_zip_and_reuse(client):
    blob=pdf(pages=3)
    first=submit(client,blob).json()
    second=submit(client,blob).json()
    assert second['reused'] and first['token']!=second['token']
    assert client.get(f"/api/jobs/{first['id']}").status_code==404
    assert client.get(f"/api/jobs/{first['id']}",headers=headers(second)).status_code==404
    assert client.get(f"/api/jobs/{first['id']}/download",headers=headers(first)).status_code==409
    engine=FakeEngine()
    row=result_row(first)
    process_job(row,engine)
    assert engine.calls==[1,2,3]
    response=client.get(f"/api/jobs/{second['id']}/download",headers=headers(second))
    assert response.status_code==200
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        doc=z.read('document.md').decode()
        assert doc.index('Page 1')<doc.index('Page 2')<doc.index('Page 3')
        assert '$x^2$' in doc
        for n in range(1,4):
            assert f'pages/{n:04d}/raw/imgs/same.png' in doc
            assert json.loads(z.read(f'pages/{n:04d}/page.json'))['page']==n
        assert json.loads(z.read('manifest.json'))['pdf_sha256']==hashlib.sha256(blob).hexdigest()
    assert submit(client,blob).json()['reused']
    assert 'token' not in (cfg.DATA/'results'/row['key']/'manifest.json').read_text().replace('max_new_tokens','')

def test_concurrent_duplicate_atomic(client):
    blob=pdf()
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda _:submit(client,blob),range(6)))
    assert all(r.status_code==201 for r in results)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM results').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==6

def test_config_keys(client,monkeypatch):
    blob=pdf()
    a=submit(client,blob).json()
    monkeypatch.setattr(cfg,'CONFIG_JSON',cfg.CONFIG_JSON+' ')
    b=submit(client,blob).json()
    assert not b['reused']
    assert result_row(a)['key']!=result_row(b)['key']

def test_limits_and_validation(client,monkeypatch):
    assert submit(client,b'not pdf').status_code==400
    doc=fitz.open();doc.new_page()
    encrypted=doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,user_pw='secret',owner_pw='owner')
    assert submit(client,encrypted).status_code==400
    monkeypatch.setattr(cfg,'MAX_PAGES',1)
    assert submit(client,pdf(pages=2)).status_code==400
    monkeypatch.setattr(cfg,'MAX_BYTES',100)
    assert submit(client,pdf()).status_code==413
    assert client.post('/api/jobs',content=b'x'*66000).status_code==413
    monkeypatch.setattr(cfg,'MAX_BYTES',50*1024**2)
    monkeypatch.setattr(cfg,'MIN_FREE',10**20)
    assert submit(client).status_code==507

def test_queue_and_ip_and_rate(client,monkeypatch):
    assert submit(client,pdf('a')).status_code==201
    assert submit(client,pdf('b')).status_code==201
    assert submit(client,pdf('c')).status_code==429
    monkeypatch.setattr(cfg,'MAX_IP',20)
    monkeypatch.setattr(cfg,'MAX_QUEUE',2)
    assert submit(client,pdf('c')).status_code==503
    for _ in range(6):
        client.post('/api/jobs',content=b'')
    assert submit(client).status_code==429

def test_resume_oom_and_failed_page_retry(client):
    j=submit(client,pdf(pages=3)).json()
    row=result_row(j)
    engine=FakeEngine(fail=lambda n,count:n==2)
    process_job(row,engine)
    assert engine.calls==[1,2,2,3]
    status=client.get(f"/api/jobs/{j['id']}",headers=headers(j)).json()
    assert status['state']=='failed' and status['completed_pages']==2
    assert client.post(f"/api/jobs/{j['id']}/retry",headers=headers(j)).status_code==200
    recover()
    retry=FakeEngine()
    process_job(result_row(j),retry)
    assert retry.calls==[2]
    assert client.get(f"/api/jobs/{j['id']}",headers=headers(j)).json()['state']=='completed'

def test_crash_after_atomic_page_before_db(client):
    j=submit(client,pdf(pages=2)).json()
    row=result_row(j)
    process_job(row,FakeEngine())
    with db.connect(True) as c:
        c.execute("UPDATE pages SET state='running' WHERE number=2")
        c.execute("UPDATE results SET state='running'")
    recover()
    engine=FakeEngine()
    process_job(result_row(j),engine)
    assert not engine.calls
    assert client.get(f"/api/jobs/{j['id']}",headers=headers(j)).json()['completed_pages']==2

def test_oom_once_recovers(client):
    j=submit(client).json()
    engine=FakeEngine(fail=lambda n,count:count==1)
    process_job(result_row(j),engine)
    assert engine.calls==[1,1] and engine.closed==1
    assert client.get(f"/api/jobs/{j['id']}",headers=headers(j)).json()['state']=='completed'

def test_proxy_and_origin(client,monkeypatch):
    monkeypatch.setenv('OCR_TRUSTED_PROXIES','172.20.0.2/32,172.20.0.9/32')
    def request(peer,xff):
        return Request({'type':'http','client':(peer,123),'headers':[(b'x-forwarded-for',xff.encode())]})
    assert client_ip(request('1.2.3.4','9.9.9.9'))=='1.2.3.4'
    assert client_ip(request('172.20.0.2','9.9.9.9, 1.2.3.4, 172.20.0.9'))=='1.2.3.4'
    assert client.post('/api/jobs',headers={'Origin':'https://evil.example'}).status_code==403

def test_math_code_unchanged():
    text='`\\(code\\)` and \\( x \\) and \\[y\\]'
    assert normalize_math(text)=='`\\(code\\)` and $x$ and \n\n$$\ny\n$$\n\n'

def test_chunked_size_limit(client,monkeypatch):
    monkeypatch.setattr(cfg,'MAX_BYTES',1024)
    def chunks():
        for _ in range(70):
            yield b'x'*1024
    r=client.post('/api/jobs',content=chunks(),headers={'Content-Type':'multipart/form-data; boundary=bad'})
    assert r.status_code==413

def test_retry_cannot_bypass_capacity(client,monkeypatch):
    j=submit(client,pdf('first')).json()
    with db.connect(True) as c:
        c.execute("UPDATE results SET state='failed'")
    submit(client,pdf('second'));submit(client,pdf('third'))
    assert client.post(f"/api/jobs/{j['id']}/retry",headers=headers(j)).status_code==429
    assert client.post(f"/api/jobs/{j['id']}/retry").status_code==404

def test_missing_image_and_mixed_weights_fail_closed(tmp_path):
    raw=tmp_path/'raw';raw.mkdir()
    (raw/'x.md').write_text('<img src="../missing.png">')
    (raw/'x.json').write_text('{}')
    with pytest.raises(ValueError):
        finish_page(tmp_path,1,{'model':'hash'})

def test_worker_exclusive_lock(client):
    import fcntl
    import os
    import subprocess
    import sys
    with (cfg.DATA/'worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        run=subprocess.run([sys.executable,'-m','app.worker'],env={**os.environ,'OCR_DATA_DIR':str(cfg.DATA)},capture_output=True,text=True,timeout=10)
    assert run.returncode!=0 and 'Only one worker' in run.stderr


def test_inline_math_whitespace_preserves_currency_and_code():
    text = r'Formula: $ x^2 = y $; `$ x $`; price $ 5 and $ 10.'
    assert normalize_math(text) == r'Formula: $x^2 = y$; `$ x $`; price $ 5 and $ 10.'


def test_validation_timeout_keeps_api_healthy(client, monkeypatch):
    import subprocess
    from app import api
    with monkeypatch.context() as patch:
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired('validator', 30)
        patch.setattr(api.subprocess, 'run', timeout)
        assert submit(client).status_code == 400
    assert client.get('/healthz').status_code == 200
    assert submit(client).status_code == 201
