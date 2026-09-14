import contextlib
import hashlib
import os
import secrets
import shutil
import sqlite3
import time
from fastapi import HTTPException
from . import config as cfg

ACTIVE = ('queued', 'waiting_gpu', 'preparing_model', 'running', 'packaging', 'cancelling')
PLACEHOLDERS = ','.join('?' for _ in ACTIVE)

@contextlib.contextmanager
def connect(write=False):
    con = sqlite3.connect(cfg.DATA / 'queue.sqlite3', timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=30000')
    try:
        if write:
            con.execute('BEGIN IMMEDIATE')
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()

def init():
    cfg.DATA.mkdir(parents=True, exist_ok=True)
    (cfg.DATA / 'results').mkdir(exist_ok=True)
    (cfg.DATA / 'incoming').mkdir(exist_ok=True)
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS results (
            key TEXT PRIMARY KEY, pdf_hash TEXT NOT NULL, config TEXT NOT NULL,
            page_count INTEGER NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
            updated REAL NOT NULL, error TEXT, model_hashes TEXT);
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, result_key TEXT NOT NULL REFERENCES results(key),
            token_hash TEXT NOT NULL, ip TEXT NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS jobs_ip ON jobs(ip);
        CREATE INDEX IF NOT EXISTS jobs_result ON jobs(result_key);
        CREATE TABLE IF NOT EXISTS job_cancellations (
            job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
            at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS pages (
            result_key TEXT NOT NULL REFERENCES results(key), number INTEGER NOT NULL,
            state TEXT NOT NULL, seconds REAL, error TEXT, attempts INTEGER DEFAULT 0,
            PRIMARY KEY(result_key, number));
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_key TEXT NOT NULL REFERENCES results(key) ON DELETE CASCADE,
            at REAL NOT NULL, code TEXT NOT NULL, page INTEGER, seconds REAL);
        CREATE INDEX IF NOT EXISTS events_result ON events(result_key,id);
        CREATE TABLE IF NOT EXISTS requests (ip TEXT NOT NULL, at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS requests_at ON requests(at);
        CREATE TABLE IF NOT EXISTS worker (id INTEGER PRIMARY KEY CHECK(id=1), heartbeat REAL NOT NULL);
        ''')

def record_event(c, key, code, page=None, seconds=None):
    c.execute('INSERT INTO events(result_key,at,code,page,seconds) VALUES (?,?,?,?,?)',
              (key, time.time(), code, page, seconds))

def event(key, code, page=None, seconds=None):
    with connect(True) as c:
        record_event(c, key, code, page, seconds)

def free_space():
    if shutil.disk_usage(cfg.DATA).free < cfg.MIN_FREE:
        raise HTTPException(507, 'Storage is below the 10 GiB reserve. Please try later.')

def rate_limit(ip):
    with connect(True) as c:
        now = time.time()
        c.execute('DELETE FROM requests WHERE at < ?', (now-60,))
        if c.execute('SELECT count(*) FROM requests WHERE ip=?', (ip,)).fetchone()[0] >= 10:
            raise HTTPException(429, 'At most 10 create/retry requests per minute.', headers={'Retry-After': '60'})
        c.execute('INSERT INTO requests VALUES (?,?)', (ip, now))

def check_capacity(c, ip, key=None):
    count = c.execute(f'''SELECT count(DISTINCT r.key) FROM jobs j JOIN results r ON r.key=j.result_key
        WHERE NOT EXISTS (SELECT 1 FROM job_cancellations x WHERE x.job_id=j.id) AND j.ip=? AND r.state IN ({PLACEHOLDERS}) AND r.key != ?''', (ip, *ACTIVE, key or '')).fetchone()[0]
    if count >= cfg.MAX_IP:
        raise HTTPException(429, 'At most 2 unfinished documents per IP.')
    count = c.execute(f'SELECT count(*) FROM results WHERE state IN ({PLACEHOLDERS})', ACTIVE).fetchone()[0]
    if count >= cfg.MAX_QUEUE:
        raise HTTPException(503, 'The queue is full. Please try later.')

def register(path, pdf_hash, page_count, ip):
    key = cfg.result_key(pdf_hash)
    token, ident = secrets.token_urlsafe(32), secrets.token_hex(16)
    now = time.time()
    with connect(True) as c:
        free_space()
        r = c.execute('SELECT * FROM results WHERE key=?', (key,)).fetchone()
        if not r:
            check_capacity(c, ip)
            base = cfg.DATA / 'results' / key
            base.mkdir(exist_ok=True)
            os.replace(path, base / 'source.pdf')
            for directory in (base, base.parent):
                fd = os.open(directory, os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            c.execute('INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?)',
                      (key, pdf_hash, cfg.CONFIG_JSON, page_count, 'queued', now, now, None, None))
            c.executemany('INSERT INTO pages(result_key,number,state) VALUES (?,?,?)',
                          [(key, n, 'pending') for n in range(1, page_count+1)])
            record_event(c, key, 'queued')
        elif r['state'] == 'cancelling':
            raise HTTPException(409, 'Cancellation is still in progress. Try again shortly.')
        elif r['state'] == 'cancelled':
            check_capacity(c, ip)
            c.execute("UPDATE results SET state='queued',error=NULL,created=?,updated=? WHERE key=?", (now,now,key))
            c.execute("UPDATE pages SET state='pending',error=NULL WHERE result_key=? AND state!='done'",(key,))
            record_event(c,key,'queued')
        elif r['state'] in ACTIVE:
            # Joining an existing GPU job does not consume global capacity, but does consume IP capacity.
            count = c.execute(f'''SELECT count(DISTINCT r.key) FROM jobs j JOIN results r ON r.key=j.result_key
                WHERE NOT EXISTS (SELECT 1 FROM job_cancellations x WHERE x.job_id=j.id) AND j.ip=? AND r.state IN ({PLACEHOLDERS}) AND r.key != ?''', (ip, *ACTIVE, key)).fetchone()[0]
            if count >= cfg.MAX_IP:
                raise HTTPException(429, 'At most 2 unfinished documents per IP.')
        c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                  (ident, key, hashlib.sha256(token.encode()).hexdigest(), ip, now))
    return {'id': ident, 'token': token, 'reused': r is not None}

def authorize(c, ident, token):
    row = c.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone()
    digest = hashlib.sha256(token.encode()).hexdigest()
    if not row or not secrets.compare_digest(row['token_hash'], digest):
        raise HTTPException(404, 'Job not found or token invalid.')
    return row

def status(ident, token):
    with connect() as c:
        j = authorize(c, ident, token)
        r = dict(c.execute('SELECT * FROM results WHERE key=?', (j['result_key'],)).fetchone())
        pages = [dict(p) for p in c.execute('SELECT number,state,seconds,error,attempts FROM pages WHERE result_key=? ORDER BY number', (j['result_key'],))]
        ahead = c.execute(f'''SELECT count(*) FROM results WHERE state IN ({PLACEHOLDERS})
            AND (created < ? OR (created = ? AND key < ?))''', (*ACTIVE, r['created'], r['created'], r['key'])).fetchone()[0]
        events = [dict(e) for e in c.execute(
            'SELECT id,at,code,page,seconds FROM events WHERE result_key=? ORDER BY id DESC LIMIT 200',
            (j['result_key'],))][::-1]
        first = c.execute("SELECT min(at) FROM events WHERE result_key=? AND code='queued'", (j['result_key'],)).fetchone()[0]
        started = c.execute("SELECT min(at) FROM events WHERE result_key=? AND code='job_start'", (j['result_key'],)).fetchone()[0]
        cancelled = c.execute('SELECT at FROM job_cancellations WHERE job_id=?',(ident,)).fetchone()
        heartbeat = c.execute('SELECT heartbeat FROM worker WHERE id=1').fetchone()
    if cancelled:
        r['state'] = 'cancelling' if r['state']=='cancelling' else 'cancelled'
        r['updated'] = cancelled[0]
        r['error'] = None
        events = [e for e in events if e['at'] <= cancelled[0]]
        events.append({'id': None, 'at':cancelled[0], 'code':r['state'], 'page':None, 'seconds':None})
    now = time.time()
    active = r['state'] in ACTIVE
    end = now if active else r['updated']
    stage = events[-1] if events else None
    return {'events': events, 'stage': stage, 'timing': {
                'active': active, 'server_time': now,
                'elapsed_seconds': max(0, end-(first or r['created'])),
                'processing_seconds': max(0, end-started) if started else None,
                'ocr_seconds': sum(p['seconds'] or 0 for p in pages),
                'stage_seconds': max(0, end-stage['at']) if stage and active else None,
            }, 'id': ident, 'state': r['state'], 'page_count': r['page_count'],
            'completed_pages': sum(p['state']=='done' for p in pages), 'pages': pages,
            'queue_position': ahead+1 if r['state']=='queued' else None,
            'error': r['error'], 'worker_online': bool(heartbeat and time.time()-heartbeat[0]<30)}

def retry(ident, token, ip):
    with connect(True) as c:
        j = authorize(c, ident, token)
        r = c.execute('SELECT * FROM results WHERE key=?', (j['result_key'],)).fetchone()
        if c.execute('SELECT 1 FROM job_cancellations WHERE job_id=?',(ident,)).fetchone():
            raise HTTPException(409, 'This job was cancelled. Upload the PDF again to resume.')
        if r['state'] != 'failed':
            raise HTTPException(409, 'Only failed jobs can be retried.')
        free_space()
        check_capacity(c, ip, r['key'])
        # Also enforce every existing subscriber's outstanding-document quota.
        for owner in c.execute('SELECT DISTINCT ip FROM jobs j WHERE result_key=? AND NOT EXISTS (SELECT 1 FROM job_cancellations x WHERE x.job_id=j.id)', (r['key'],)).fetchall():
            check_capacity(c, owner[0], r['key'])
        c.execute("UPDATE pages SET state='pending', error=NULL WHERE result_key=? AND state!='done'", (r['key'],))
        c.execute("UPDATE results SET state='queued',error=NULL,created=?,updated=? WHERE key=?", (time.time(),time.time(),r['key']))
        record_event(c, r['key'], 'queued')


def cancel(ident, token):
    with connect(True) as c:
        j = authorize(c,ident,token)
        if c.execute('SELECT 1 FROM job_cancellations WHERE job_id=?',(ident,)).fetchone():
            return
        r = c.execute('SELECT state FROM results WHERE key=?',(j['result_key'],)).fetchone()
        if r['state'] not in ACTIVE:
            raise HTTPException(409,'Only unfinished jobs can be cancelled.')
        now = time.time()
        c.execute('INSERT INTO job_cancellations VALUES (?,?)',(ident,now))
        # Cancellation belongs to this capability, not unrelated subscribers.
        remaining = c.execute("SELECT count(*) FROM jobs j WHERE result_key=? AND NOT EXISTS (SELECT 1 FROM job_cancellations x WHERE x.job_id=j.id)",(j['result_key'],)).fetchone()[0]
        if not remaining:
            state = 'cancelled' if r['state']=='queued' else 'cancelling'
            c.execute('UPDATE results SET state=?,updated=? WHERE key=?',(state,now,j['result_key']))
            record_event(c,j['result_key'],state)

class JobCancelled(Exception):
    pass

def check_cancelled(key):
    with connect() as c:
        row = c.execute('SELECT state FROM results WHERE key=?',(key,)).fetchone()
    if row and row[0] in ('cancelling','cancelled'):
        raise JobCancelled()
