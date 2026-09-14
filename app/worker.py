import fcntl
import json
import logging
import multiprocessing as mp
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
import pika
import pymupdf as fitz
from . import broker, config as cfg, db
from .artifacts import package
from .inference import child

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('worker')
logging.getLogger('pika').setLevel(logging.CRITICAL)  # broker faults are logged once as broker_unavailable
STOP=threading.Event()
LAST_TICK=time.monotonic()
fitz.TOOLS.mupdf_display_errors(False)
fitz.TOOLS.mupdf_display_warnings(False)

def tick():
    # Progress marker: heartbeat() stops beating when the main thread stalls.
    global LAST_TICK
    LAST_TICK=time.monotonic()

class InferenceFailure(Exception):
    pass

class Engine:
    def __init__(self):
        self.process=None
        self.key=None
        self.connection=None
        self.last_use=time.monotonic()
    def close(self):
        if self.process:
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(15)
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join(5)
            else:
                self.process.join()
            self.connection.close()
            if self.process.is_alive():
                # ponytail: a child stuck in the GPU driver survives SIGKILL; the handle is dropped, a container restart reclaims it.
                log.error('inference_subprocess_unkillable pid=%s',self.process.pid)
            else:
                self.process.close()
            self.process=None
            log.info('inference_subprocess_stopped VRAM released')
    def receive(self,timeout):
        deadline=time.monotonic()+timeout
        while not STOP.is_set():
            tick()
            if self.key:
                db.check_cancelled(self.key)
            if self.connection.poll(1):
                try:
                    response=self.connection.recv()
                except EOFError:
                    raise InferenceFailure('inference_process_exited')
                if response['state']=='error':
                    log.warning('inference_error code=%s type=%s',response['error'],response.get('exception_type'))
                    raise InferenceFailure(response['error'])
                return response
            if not self.process.is_alive():
                raise InferenceFailure('inference_process_exited')
            if time.monotonic()>deadline:
                raise InferenceFailure('inference_timeout')
        raise InterruptedError()
    def ensure(self,key):
        self.key=key
        db.check_cancelled(key)
        if self.process and self.process.is_alive():
            return
        self.close()
        state(key,'waiting_gpu')
        log.info('waiting_gpu minimum_free_mib=%s',cfg.GPU_FREE_MIB)
        give_up=time.monotonic()+cfg.GPU_WAIT_TIMEOUT
        while not STOP.is_set():
            tick()
            db.check_cancelled(key)
            if gpu_free()>=cfg.GPU_FREE_MIB:
                break
            if time.monotonic()>give_up:
                raise InferenceFailure('gpu_wait_timeout')
            STOP.wait(5)
        if STOP.is_set():
            raise InterruptedError()
        state(key,'preparing_model')
        log.info('preparing_model cache_present=%s',(cfg.CACHE/'official_models/PaddleOCR-VL-1.6/model.safetensors').exists())
        ctx=mp.get_context('spawn')
        parent,remote=ctx.Pipe()
        self.connection=parent
        self.process=ctx.Process(target=child,args=(remote,))
        self.process.start()
        remote.close()
        self.receive(cfg.MODEL_TIMEOUT)
        db.event(key,'model_ready')
        self.last_use=time.monotonic()
    def predict(self,source,staging):
        self.connection.send({'source':str(source),'staging':str(staging)})
        result=self.receive(cfg.PAGE_TIMEOUT)
        self.last_use=time.monotonic()
        return result

def gpu_free():
    try:
        result=subprocess.run(['nvidia-smi','--id=0','--query-gpu=memory.free','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=10,check=True)
        return int(result.stdout.strip().splitlines()[0])
    except (OSError,ValueError,subprocess.SubprocessError):
        return 0

def state(key,value,error=None):
    with db.connect(True) as c:
        current=c.execute('SELECT state FROM results WHERE key=?',(key,)).fetchone()
        if current and current[0] in ('cancelling','cancelled') and value!='cancelled':
            raise db.JobCancelled()
        c.execute('UPDATE results SET state=?,error=?,updated=? WHERE key=?',(value,error,time.time(),key))
        db.record_event(c,key,value)

def beat():
    # A stalled main thread must read as offline, not merely a live heartbeat thread.
    if time.monotonic()-LAST_TICK>cfg.STALL_SECONDS:
        log.warning('worker_stalled seconds=%.0f',time.monotonic()-LAST_TICK)
        return
    with db.connect(True) as c:
        c.execute('INSERT OR REPLACE INTO worker VALUES (1,?)',(time.time(),))

def heartbeat():
    while not STOP.is_set():
        try:
            beat()
        except Exception:
            log.error('heartbeat_failed')
        STOP.wait(5)

def recover():
    # Exclusive flock is held for the worker lifetime. No live peer can own these jobs.
    with db.connect(True) as c:
        for row in c.execute("SELECT key FROM results WHERE state='cancelling'").fetchall():
            c.execute("UPDATE results SET state='cancelled',updated=? WHERE key=?",(time.time(),row['key']))
            db.record_event(c,row['key'],'cancelled')
        for row in c.execute("SELECT key FROM results WHERE state IN ('waiting_gpu','preparing_model','running','packaging')").fetchall():
            db.record_event(c,row['key'],'recovered')
        c.execute(f"UPDATE results SET state='queued',error=NULL WHERE state IN ({db.PLACEHOLDERS})",db.ACTIVE)
        c.execute("UPDATE pages SET state='pending' WHERE state='running'")
    log.info('recovered_interrupted_jobs')

def complete_page(key,n,meta):
    with db.connect(True) as c:
        previous = c.execute("SELECT state FROM pages WHERE result_key=? AND number=?",(key,n)).fetchone()
        if previous and previous[0] != 'done':
            db.record_event(c,key,'page_done',n,meta['seconds'])
        c.execute("UPDATE pages SET state='done',seconds=?,error=NULL WHERE result_key=? AND number=?",(meta['seconds'],key,n))
        c.execute('UPDATE results SET model_hashes=?,updated=? WHERE key=?',(json.dumps(meta['model_hashes'],sort_keys=True),time.time(),key))

def process_job(row,engine):
    try:
        db.check_cancelled(row['key'])
        _process_job(row,engine)
    except db.JobCancelled:
        engine.close()
        with db.connect(True) as c:
            c.execute("UPDATE pages SET state='pending' WHERE result_key=? AND state='running'",(row['key'],))
        state(row['key'],'cancelled')
        log.info('job_cancelled key=%s',row['key'][:12])

def _process_job(row,engine):
    key=row['key']
    base=cfg.DATA/'results'/key
    pages_dir=base/'pages'
    pages_dir.mkdir(exist_ok=True)
    if row['config'] != cfg.CONFIG_JSON:
        state(key,'failed','configuration_changed: restore the original worker configuration to retry')
        return
    log.info('job_start key=%s pages=%s',key[:12],row['page_count'])
    db.event(key,'job_start')
    # One slow document must not hold the single GPU queue indefinitely.
    deadline=time.monotonic()+cfg.JOB_BASE_SECONDS+cfg.JOB_PAGE_SECONDS*row['page_count']
    for n in range(1,row['page_count']+1):
        db.check_cancelled(key)
        if STOP.is_set():
            raise InterruptedError()
        tick()
        final=pages_dir/f'{n:04d}'
        if (final/'complete.json').exists():
            complete_page(key,n,json.loads((final/'complete.json').read_text()))
            continue
        if time.monotonic()>deadline:
            with db.connect(True) as c:
                db.record_event(c,key,'job_deadline',n)
                c.execute("UPDATE pages SET state='failed',error='job_deadline' WHERE result_key=? AND state!='done'",(key,))
            log.error('job_deadline key=%s page=%s',key[:12],n)
            break
        staging=base/f'.page-{n:04d}.tmp'
        stop=False
        for attempt in range(2):
            try:
                db.free_space()
                waited=time.monotonic()
                engine.ensure(key)
                deadline+=time.monotonic()-waited  # GPU wait and model load don't spend the job budget
                state(key,'running')
                with db.connect(True) as c:
                    c.execute("UPDATE pages SET state='running',attempts=attempts+1,error=NULL WHERE result_key=? AND number=?",(key,n))
                if staging.exists():
                    shutil.rmtree(staging)
                staging.mkdir()
                db.event(key,'rendering',n)
                source=staging/'source.png'
                with fitz.open(base/'source.pdf') as pdf:
                    pdf[n-1].get_pixmap(dpi=cfg.CONFIG['dpi']).save(source)
                db.event(key,'recognizing',n)
                response=engine.predict(source,staging)
                db.check_cancelled(key)
                db.event(key,'saving_page',n)
                if final.exists():
                    shutil.rmtree(final)
                os.replace(staging,final)
                fd=os.open(pages_dir,os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                complete_page(key,n,response)
                log.info('page_done key=%s page=%s seconds=%.2f',key[:12],n,response['seconds'])
                break
            except db.JobCancelled:
                raise
            except InterruptedError:
                raise
            except Exception as e:
                engine.close()
                error=str(e) if isinstance(e,InferenceFailure) else 'storage_or_render_error'
                if error=='gpu_out_of_memory' and attempt==0:
                    db.event(key,'oom_retry',n)
                    log.warning('oom_retry key=%s page=%s',key[:12],n)
                    continue
                with db.connect(True) as c:
                    db.record_event(c,key,'page_failed',n)
                    c.execute("UPDATE pages SET state='failed',error=? WHERE result_key=? AND number=?",(error,key,n))
                    if error=='gpu_wait_timeout':
                        # Waiting again for every remaining page would hold the queue pages×timeout.
                        c.execute("UPDATE pages SET state='failed',error=? WHERE result_key=? AND state!='done'",(error,key))
                        stop=True
                log.error('page_failed key=%s page=%s code=%s',key[:12],n,error)
                break
        if stop:
            break
    with db.connect() as c:
        pages=[dict(p) for p in c.execute('SELECT number,state,seconds,error,attempts FROM pages WHERE result_key=? ORDER BY number',(key,))]
    if any(p['state']!='done' for p in pages):
        state(key,'failed','Some pages failed. Retry will keep completed pages.')
        return
    state(key,'packaging')
    try:
        package(base,row,pages)
        state(key,'completed')
        log.info('job_completed key=%s',key[:12])
    except db.JobCancelled:
        raise
    except Exception:
        state(key,'failed','archive_validation_failed')
        log.error('archive_validation_failed key=%s',key[:12])

def queued_keys():
    with db.connect() as c:
        return [r[0] for r in c.execute("SELECT key FROM results WHERE state='queued' ORDER BY created,key")]

def handle(key,engine):
    # Idempotent consumer: cancelled, finished, deleted or duplicate messages are skipped.
    with db.connect() as c:
        row=c.execute('SELECT * FROM results WHERE key=?',(key,)).fetchone()
    if not row or row['state']!='queued':
        log.info('message_skipped key=%s',key[:12])
        return False
    process_job(dict(row),engine)
    return True

def drain(engine):
    # Fallback poll: covers a missing broker and any message lost between commit and publish.
    while not STOP.is_set():
        keys=queued_keys()
        if not keys:
            return
        tick()
        handle(keys[0],engine)

def idle(engine):
    if engine.process and time.monotonic()-engine.last_use>=cfg.IDLE_SECONDS:
        engine.close()

def consume(engine):
    con=pika.BlockingConnection(broker.params())
    try:
        ch=con.channel()
        broker.declare(ch)
        ch.basic_qos(prefetch_count=1)
        broker.resync(ch,queued_keys)
        log.info('broker_connected')
        polled=time.monotonic()
        for method,_,body in ch.consume(broker.QUEUE,inactivity_timeout=1):
            tick()
            if STOP.is_set():
                break
            if method:
                # Ack before the GPU work: SQLite and recover() own redelivery, so a long job
                # never holds an unacked message past RabbitMQ's consumer_timeout.
                ch.basic_ack(method.delivery_tag)
                handle(body.decode(),engine)
            elif time.monotonic()-polled>=cfg.POLL_SECONDS:
                drain(engine)
                polled=time.monotonic()
            idle(engine)
    finally:
        if con.is_open:
            con.close()

def main():
    db.init()
    lock=(cfg.DATA/'worker.lock').open('a')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Only one worker may use this data directory.')
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *_:STOP.set())
    recover()
    thread=threading.Thread(target=heartbeat,daemon=True)
    thread.start()
    engine=Engine()
    try:
        while not STOP.is_set():
            tick()
            if cfg.AMQP_URL:
                try:
                    consume(engine)
                    continue
                except InterruptedError:
                    raise
                except pika.exceptions.AMQPError as e:
                    # Only broker faults fall back here; job errors (OSError) crash the worker so recover() requeues.
                    log.warning('broker_unavailable error=%s',type(e).__name__)
            drain(engine)
            idle(engine)
            STOP.wait(5 if cfg.AMQP_URL else 1)
    except InterruptedError:
        pass
    finally:
        STOP.set()
        engine.close()
        thread.join(10)
        with db.connect(True) as c:
            c.execute('DELETE FROM worker WHERE id=1')
        lock.close()

if __name__=='__main__':
    main()
