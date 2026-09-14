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
from app import broker, config as cfg, db, worker
from app.api import app, client_ip
from app.artifacts import finish_page, package, normalize_math
from app.worker import recover, process_job, InferenceFailure, STOP
from starlette.requests import Request

REAL_PUBLISH=broker.publish
PUBLISHED=[]

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(cfg,'DATA',tmp_path)
    monkeypatch.setattr(cfg,'MIN_FREE',0)
    PUBLISHED.clear()
    monkeypatch.setattr(broker,'publish',PUBLISHED.append)
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
        self.process=None
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
    assert client.post('/api/jobs',headers={'Origin':'null'}).status_code==403
    monkeypatch.setenv('OCR_PUBLIC_ORIGIN', 'https://ocr.sir-labs.com')
    assert client.post('/api/jobs',headers={'Origin':'https://ocr.sir-labs.com'}).status_code==400
    assert client.get('/').headers['referrer-policy']=='strict-origin'

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


def test_native_download_cookie_is_job_scoped(client):
    j = submit(client).json()
    process_job(result_row(j), FakeEngine())
    url = f"/api/jobs/{j['id']}"
    assert client.get(url + '/download').status_code == 404
    response = client.get(url, headers=headers(j))
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie
    assert f'Path={url}/download' in cookie
    assert client.get(url + '/download').status_code == 200
    assert client.get(url).status_code == 404
    assert client.get(url + '/download', headers={'Authorization':'Bearer wrong'}).status_code == 404
    other = submit(client, pdf('different')).json()
    assert client.get(f"/api/jobs/{other['id']}/download").status_code == 404


def test_https_redirect_trust_and_no_loop(client, monkeypatch):
    monkeypatch.setenv('OCR_PUBLIC_ORIGIN', 'https://ocr.sir-labs.com')
    # Untrusted clients cannot trigger a redirect by supplying CF headers.
    assert client.get('/', headers={'CF-Visitor':'{"scheme":"http"}'}, follow_redirects=False).status_code == 200
    monkeypatch.setenv('OCR_TRUSTED_PROXIES', '172.20.0.2/32')
    with TestClient(app, client=('172.20.0.2', 1234)) as proxy:
        response = proxy.get('/?view=upload', headers={'CF-Visitor':'{"scheme":"http"}'}, follow_redirects=False)
        assert response.status_code == 308
        assert response.headers['location'] == 'https://ocr.sir-labs.com/?view=upload'
        for value in ['{"scheme":"https"}', '{}', 'invalid', 'null']:
            assert proxy.get('/', headers={'CF-Visitor': value}, follow_redirects=False).status_code == 200
        assert proxy.get('/healthz', follow_redirects=False).status_code == 200


def test_job_event_timeline_and_private_timing(client):
    blob = pdf()
    j = submit(client, blob).json()
    process_job(result_row(j), FakeEngine())
    result = client.get(f"/api/jobs/{j['id']}", headers=headers(j)).json()
    codes = [e['code'] for e in result['events']]
    assert codes == ['queued','job_start','running','rendering','recognizing','saving_page','page_done','packaging','completed']
    assert [e['at'] for e in result['events']] == sorted(e['at'] for e in result['events'])
    assert result['timing']['ocr_seconds'] == .01
    assert result['timing']['active'] is False
    assert result['timing']['stage_seconds'] is None
    assert result['timing']['elapsed_seconds'] >= 0
    assert result['timing']['processing_seconds'] is not None
    assert all(e['page'] == 1 for e in result['events'] if e['code'] in ('rendering','recognizing','saving_page','page_done'))
    assert j['token'] not in json.dumps(result)
    assert 'Hello OCR' not in json.dumps(result)
    assert client.get(f"/api/jobs/{j['id']}", headers={'Authorization':'Bearer wrong'}).status_code == 404
    # Joining an identical document shares the actual processing history.
    alias = submit(client, blob).json()
    assert client.get(f"/api/jobs/{alias['id']}", headers=headers(alias)).json()['events'] == result['events']


def test_events_recovery_bounded_and_legacy(client):
    j = submit(client).json()
    key = result_row(j)['key']
    with db.connect(True) as c:
        c.execute("UPDATE results SET state='running' WHERE key=?", (key,))
    recover()
    result = db.status(j['id'],j['token'])
    assert result['state'] == 'queued'
    assert result['events'][-1]['code'] == 'recovered'
    with db.connect(True) as c:
        for _ in range(205):
            db.record_event(c,key,'recognizing',1)
    result = db.status(j['id'],j['token'])
    assert len(result['events']) == 200
    assert result['timing']['active'] and result['timing']['stage_seconds'] >= 0
    with db.connect(True) as c:
        c.execute('DELETE FROM events WHERE result_key=?',(key,))
    db.init()  # Existing databases remain usable and migration is idempotent.
    result = db.status(j['id'],j['token'])
    assert result['events'] == [] and result['stage'] is None


def test_cancel_queued_private_idempotent_and_low_disk(client, monkeypatch):
    j=submit(client).json();url=f"/api/jobs/{j['id']}/cancel"
    assert client.post(url,headers={'Authorization':'Bearer wrong'}).status_code==404
    monkeypatch.setattr(db,'free_space',lambda: (_ for _ in ()).throw(HTTPException(507,'full')))
    monkeypatch.setattr(db,'rate_limit',lambda ip: (_ for _ in ()).throw(HTTPException(429,'busy')))
    assert client.post(url,headers=headers(j)).json()['state']=='cancelled'
    assert client.post(url,headers=headers(j)).status_code==200
    engine=FakeEngine();process_job(result_row(j),engine)
    assert engine.calls==[]
    assert db.status(j['id'],j['token'])['state']=='cancelled'


def test_cancel_shared_job_does_not_stop_other_subscriber(client):
    blob=pdf();a=submit(client,blob).json();b=submit(client,blob).json()
    db.cancel(a['id'],a['token'])
    assert db.status(a['id'],a['token'])['state']=='cancelled'
    assert db.status(b['id'],b['token'])['state']=='queued'
    process_job(result_row(b),FakeEngine())
    assert db.status(b['id'],b['token'])['state']=='completed'
    assert db.status(a['id'],a['token'])['state']=='cancelled'


def test_cancel_running_keeps_pages_and_reupload_resumes(client):
    blob=pdf(pages=2);j=submit(client,blob).json()
    class CancellingEngine(FakeEngine):
        def predict(self,source,staging):
            result=super().predict(source,staging)
            if len(self.calls)==2:db.cancel(j['id'],j['token'])
            return result
    engine=CancellingEngine();process_job(result_row(j),engine)
    result=db.status(j['id'],j['token'])
    assert result['state']=='cancelled' and result['completed_pages']==1
    assert engine.closed==1
    again=submit(client,blob).json();engine=FakeEngine()
    process_job(result_row(again),engine)
    assert engine.calls==[2]
    assert db.status(again['id'],again['token'])['state']=='completed'
    assert db.status(j['id'],j['token'])['state']=='cancelled'


def test_cancellation_survives_restart_and_interrupts_receive(client):
    from app.worker import Engine,state
    j=submit(client).json();key=result_row(j)['key']
    state(key,'preparing_model');db.cancel(j['id'],j['token'])
    assert db.status(j['id'],j['token'])['state']=='cancelling'
    engine=Engine();engine.key=key
    with pytest.raises(db.JobCancelled):engine.receive(600)
    recover()
    assert db.status(j['id'],j['token'])['state']=='cancelled'
    assert result_row(j)['state']=='cancelled'


def test_preview_auth_images_and_unfinished_page(client):
    j=submit(client).json();url=f"/api/jobs/{j['id']}/preview/pages/1"
    assert client.get(url,headers=headers(j)).status_code==409
    process_job(result_row(j),FakeEngine())
    assert client.get(url).status_code==404
    response=client.get(url,headers=headers(j))
    assert response.status_code==200 and 'Page 1' in response.json()['markdown']
    image=next(iter(response.json()['images'].values()))
    assert client.get(image).status_code==404
    client.get(f"/api/jobs/{j['id']}",headers=headers(j))
    assert client.get(image).status_code==200
    assert client.get(image,headers={'Authorization':'Bearer wrong'}).status_code==404
    assert client.get(url+'/images/../../source.pdf',headers=headers(j)).status_code in (404,422)
    assert client.get(f"/api/jobs/{j['id']}/preview/pages/2",headers=headers(j)).status_code==404
    assert client.post(f"/api/jobs/{j['id']}/cancel",headers=headers(j)).status_code==409


def test_preview_rejects_paths_and_active_images(client):
    j=submit(client).json();process_job(result_row(j),FakeEngine())
    base=cfg.DATA/'results'/result_row(j)['key']/'pages/0001'
    (base/'evil.svg').write_text('<svg onload="alert(1)"/>')
    (base/'page.md').write_text('![bad](../../source.pdf) ![svg](evil.svg) ![external](https://evil.example/pixel.png)')
    assert client.get(f"/api/jobs/{j['id']}/preview/pages/1",headers=headers(j)).json()['images']=={}


def test_running_cancel_terminates_inference_subprocess(client):
    import multiprocessing as mp
    import threading
    from app.worker import Engine,state
    j=submit(client).json();key=result_row(j)['key'];state(key,'running')
    engine=Engine();engine.key=key
    ctx=mp.get_context('spawn');parent,remote=ctx.Pipe()
    engine.connection=parent
    engine.process=ctx.Process(target=time.sleep,args=(60,));engine.process.start()
    pid=engine.process.pid
    trigger=threading.Timer(.1,lambda:db.cancel(j['id'],j['token']));trigger.start()
    start=time.monotonic()
    try:
        with pytest.raises(db.JobCancelled):engine.receive(600)
    finally:
        engine.close();remote.close();trigger.join()
    assert time.monotonic()-start < 5
    assert engine.process is None
    assert pid not in [p.pid for p in mp.active_children()]


def test_publish_only_when_a_queued_event_is_recorded(client):
    blob=pdf()
    a=submit(client,blob).json();key=result_row(a)['key']
    assert PUBLISHED==[key]
    b=submit(client,blob).json()  # joins the in-flight result: no second message
    assert PUBLISHED==[key]
    db.cancel(a['id'],a['token']);db.cancel(b['id'],b['token'])
    c=submit(client,blob).json()  # resume after cancel re-queues
    assert PUBLISHED==[key,key]
    process_job(result_row(c),FakeEngine(fail=lambda n,count:True))
    assert client.post(f"/api/jobs/{c['id']}/retry",headers=headers(c)).status_code==200
    assert PUBLISHED==[key,key,key]


def test_publish_failure_never_fails_upload(client,monkeypatch):
    monkeypatch.setattr(broker,'publish',REAL_PUBLISH)
    monkeypatch.setattr(cfg,'AMQP_URL','amqp://u:p@127.0.0.1:1/%2F')
    r=submit(client)
    assert r.status_code==201
    assert db.status(r.json()['id'],r.json()['token'])['state']=='queued'


def test_consumer_skips_stale_and_duplicate_messages(client):
    a=submit(client,pdf('a')).json();b=submit(client,pdf('b')).json()
    db.cancel(a['id'],a['token'])
    engine=FakeEngine()
    assert not worker.handle(result_row(a)['key'],engine)
    assert not worker.handle('missing',engine)
    assert engine.calls==[]
    assert worker.handle(result_row(b)['key'],engine)
    assert db.status(b['id'],b['token'])['state']=='completed'
    assert not worker.handle(result_row(b)['key'],engine)
    assert engine.calls==[1]


def test_resync_rebuilds_queue_oldest_first(client):
    ka=result_row(submit(client,pdf('a')).json())['key']
    kb=result_row(submit(client,pdf('b')).json())['key']
    with db.connect(True) as c:
        c.execute('UPDATE results SET created=1 WHERE key=?',(kb,))
        c.execute('UPDATE results SET created=2 WHERE key=?',(ka,))
    class Channel:
        calls=[]
        def queue_purge(self,queue):
            self.calls.append(('purge',queue))
        def basic_publish(self,exchange,routing_key,body,properties=None,mandatory=False):
            self.calls.append(('publish',body.decode()))
    ch=Channel()
    broker.resync(ch,worker.queued_keys)
    assert ch.calls==[('purge','ocr.jobs'),('publish',kb),('publish',ka)]


def test_job_deadline_fails_remaining_pages_and_retry_resumes(client,monkeypatch):
    j=submit(client,pdf(pages=2)).json()
    with monkeypatch.context() as patch:
        patch.setattr(cfg,'JOB_BASE_SECONDS',-1)
        patch.setattr(cfg,'JOB_PAGE_SECONDS',0)
        engine=FakeEngine();process_job(result_row(j),engine)
    status=db.status(j['id'],j['token'])
    assert engine.calls==[] and status['state']=='failed'
    assert {p['error'] for p in status['pages']}=={'job_deadline'}
    assert client.post(f"/api/jobs/{j['id']}/retry",headers=headers(j)).status_code==200
    engine=FakeEngine();process_job(result_row(j),engine)
    assert engine.calls==[1,2] and db.status(j['id'],j['token'])['state']=='completed'


def test_gpu_wait_is_bounded(client,monkeypatch):
    key=result_row(submit(client).json())['key']
    monkeypatch.setattr(worker,'gpu_free',lambda:0)
    monkeypatch.setattr(cfg,'GPU_WAIT_TIMEOUT',-1)
    with pytest.raises(InferenceFailure,match='gpu_wait_timeout'):
        worker.Engine().ensure(key)


def test_heartbeat_stops_when_worker_stalls(client,monkeypatch):
    def rows():
        with db.connect() as c:
            return c.execute('SELECT count(*) FROM worker').fetchone()[0]
    monkeypatch.setattr(worker,'LAST_TICK',time.monotonic()-cfg.STALL_SECONDS-1)
    worker.beat()
    assert rows()==0
    worker.tick();worker.beat()
    assert rows()==1


@pytest.mark.skipif(not cfg.AMQP_URL,reason='needs RabbitMQ at OCR_AMQP_URL')
def test_rabbitmq_round_trip(client,monkeypatch):
    monkeypatch.setattr(broker,'publish',REAL_PUBLISH)
    j=submit(client).json()
    real=worker.process_job
    def once(row,engine):
        real(row,engine);STOP.set()
    monkeypatch.setattr(worker,'process_job',once)
    worker.consume(FakeEngine())
    assert db.status(j['id'],j['token'])['state']=='completed'


def test_gpu_wait_timeout_fails_remaining_pages_once(client):
    j=submit(client,pdf(pages=3)).json()
    class Busy(FakeEngine):
        waits=0
        def ensure(self,key):
            self.waits+=1
            raise InferenceFailure('gpu_wait_timeout')
    engine=Busy();process_job(result_row(j),engine)
    status=db.status(j['id'],j['token'])
    assert engine.waits==1 and status['state']=='failed'
    assert {p['error'] for p in status['pages']}=={'gpu_wait_timeout'}


def test_gpu_wait_and_model_load_do_not_spend_job_budget(client,monkeypatch):
    j=submit(client,pdf(pages=2)).json()
    monkeypatch.setattr(cfg,'JOB_BASE_SECONDS',0.5)
    monkeypatch.setattr(cfg,'JOB_PAGE_SECONDS',0)
    class SlowStart(FakeEngine):
        def ensure(self,key):
            time.sleep(1)  # longer than the whole budget
    process_job(result_row(j),SlowStart())
    assert db.status(j['id'],j['token'])['state']=='completed'
