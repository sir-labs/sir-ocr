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
import pymupdf as fitz
from . import config as cfg, db
from .artifacts import package
from .inference import child

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('worker')
STOP=threading.Event()

class InferenceFailure(Exception):
    pass

class Engine:
    def __init__(self):
        self.process=None
        self.connection=None
        self.last_use=time.monotonic()
    def close(self):
        if self.process:
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(15)
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join()
            else:
                self.process.join()
            self.connection.close()
            self.process.close()
            self.process=None
            log.info('inference_subprocess_stopped VRAM released')
    def receive(self,timeout):
        deadline=time.monotonic()+timeout
        while not STOP.is_set():
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
        if self.process and self.process.is_alive():
            return
        self.close()
        state(key,'waiting_gpu')
        log.info('waiting_gpu minimum_free_mib=%s',cfg.GPU_FREE_MIB)
        while not STOP.is_set():
            if gpu_free()>=cfg.GPU_FREE_MIB:
                break
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
        self.receive(1800)
        self.last_use=time.monotonic()
    def predict(self,source,staging):
        self.connection.send({'source':str(source),'staging':str(staging)})
        result=self.receive(600)
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
        c.execute('UPDATE results SET state=?,error=?,updated=? WHERE key=?',(value,error,time.time(),key))

def heartbeat():
    while not STOP.is_set():
        try:
            with db.connect(True) as c:
                c.execute('INSERT OR REPLACE INTO worker VALUES (1,?)',(time.time(),))
        except Exception:
            log.error('heartbeat_failed')
        STOP.wait(5)

def recover():
    # Exclusive flock is held for the worker lifetime. No live peer can own these jobs.
    with db.connect(True) as c:
        c.execute(f"UPDATE results SET state='queued',error=NULL WHERE state IN ({db.PLACEHOLDERS})",db.ACTIVE)
        c.execute("UPDATE pages SET state='pending' WHERE state='running'")
    log.info('recovered_interrupted_jobs')

def complete_page(key,n,meta):
    with db.connect(True) as c:
        c.execute("UPDATE pages SET state='done',seconds=?,error=NULL WHERE result_key=? AND number=?",(meta['seconds'],key,n))
        c.execute('UPDATE results SET model_hashes=?,updated=? WHERE key=?',(json.dumps(meta['model_hashes'],sort_keys=True),time.time(),key))

def process_job(row,engine):
    key=row['key']
    base=cfg.DATA/'results'/key
    pages_dir=base/'pages'
    pages_dir.mkdir(exist_ok=True)
    if row['config'] != cfg.CONFIG_JSON:
        state(key,'failed','configuration_changed: restore the original worker configuration to retry')
        return
    log.info('job_start key=%s pages=%s',key[:12],row['page_count'])
    for n in range(1,row['page_count']+1):
        if STOP.is_set():
            raise InterruptedError()
        final=pages_dir/f'{n:04d}'
        if (final/'complete.json').exists():
            complete_page(key,n,json.loads((final/'complete.json').read_text()))
            continue
        staging=base/f'.page-{n:04d}.tmp'
        for attempt in range(2):
            try:
                db.free_space()
                engine.ensure(key)
                state(key,'running')
                with db.connect(True) as c:
                    c.execute("UPDATE pages SET state='running',attempts=attempts+1,error=NULL WHERE result_key=? AND number=?",(key,n))
                if staging.exists():
                    shutil.rmtree(staging)
                staging.mkdir()
                source=staging/'source.png'
                with fitz.open(base/'source.pdf') as pdf:
                    pdf[n-1].get_pixmap(dpi=cfg.CONFIG['dpi']).save(source)
                response=engine.predict(source,staging)
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
            except InterruptedError:
                raise
            except Exception as e:
                engine.close()
                error=str(e) if isinstance(e,InferenceFailure) else 'storage_or_render_error'
                if error=='gpu_out_of_memory' and attempt==0:
                    log.warning('oom_retry key=%s page=%s',key[:12],n)
                    continue
                with db.connect(True) as c:
                    c.execute("UPDATE pages SET state='failed',error=? WHERE result_key=? AND number=?",(error,key,n))
                log.error('page_failed key=%s page=%s code=%s',key[:12],n,error)
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
    except Exception:
        state(key,'failed','archive_validation_failed')
        log.error('archive_validation_failed key=%s',key[:12])

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
            with db.connect() as c:
                row=c.execute("SELECT * FROM results WHERE state='queued' ORDER BY created,key LIMIT 1").fetchone()
            if row:
                process_job(dict(row),engine)
            else:
                if engine.process and time.monotonic()-engine.last_use>=cfg.IDLE_SECONDS:
                    engine.close()
                STOP.wait(1)
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
