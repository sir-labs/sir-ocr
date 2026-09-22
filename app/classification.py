"""Independent, versioned OCR enrichment. No model imports in the API process."""
import hashlib
import json
import logging
import time
from pathlib import PurePosixPath
from fastapi import HTTPException
from . import config as cfg, db

VERSION = 'openthai-f3709948-taxonomy-v1'
MODEL = 'iapp/OpenThai-SystemOne'
REVISION = 'f3709948b5e3cc9606a57e74ba62b7a639d17dd3'
CATALOG = {
    'document_type': {
        'lecture': 'สไลด์หรือเอกสารประกอบการเรียน Lecture notes / slides',
        'research': 'บทความวิจัย วิทยานิพนธ์ Research paper / thesis',
        'exercise': 'ข้อสอบหรือแบบฝึกหัด Exam / worksheet',
        'book': 'หนังสือหรือตำรา Textbook',
        'report': 'รายงานหรือเอกสารการทำงาน Report / business document',
        'form': 'แบบฟอร์ม ใบเสร็จ หรือเอกสารธุรกรรม Form / receipt',
        'other': 'อื่น ๆ หรือหลักฐานไม่เพียงพอ Other / unknown',
    },
    'topic': {
        'algorithms': 'การออกแบบและวิเคราะห์อัลกอริทึม Algorithms, complexity, graphs, dynamic programming',
        'machine_learning': 'การเรียนรู้ของเครื่อง สถิติ AI Machine learning, statistics, artificial intelligence',
        'signal_processing': 'สัญญาณ ภาพ Fourier Signal and image processing',
        'software': 'ซอฟต์แวร์ ฐานข้อมูล ระบบเครือข่าย Software, databases, networking',
        'mathematics': 'คณิตศาสตร์ทั่วไป Mathematics',
        'health': 'การแพทย์ สุขภาพ Health and medicine',
        'business': 'ธุรกิจ การเงิน การบริหาร Business and finance',
        'other': 'หัวข้ออื่นหรือระบุไม่ได้ Other / unknown',
    },
    'page_role': {
        'cover': 'หน้าปก สารบัญ หรือข้อมูลชื่อเอกสาร Cover / contents',
        'explanation': 'คำอธิบายเนื้อหา แนวคิด หรือทฤษฎี Explanation / theory',
        'definition': 'คำนิยามหรือคำศัพท์ Definition',
        'proof': 'บทพิสูจน์ทางคณิตศาสตร์ Proof',
        'example': 'ตัวอย่างพร้อมวิธีทำ Worked example',
        'exercise': 'โจทย์ แบบฝึกหัด หรือคำถาม Exercise / questions',
        'references': 'บรรณานุกรมหรือรายการอ้างอิง References',
        'other': 'อื่น ๆ หรือเนื้อหาไม่เพียงพอ Other / unknown',
    },
}


def init(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS page_classifications (
      result_key TEXT NOT NULL REFERENCES results(key) ON DELETE CASCADE,
      number INTEGER NOT NULL, version TEXT NOT NULL, state TEXT NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL, error TEXT, payload TEXT,
      PRIMARY KEY(result_key,number,version));
    CREATE TABLE IF NOT EXISTS classification_worker (
      id INTEGER PRIMARY KEY CHECK(id=1), heartbeat REAL NOT NULL, state TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS classification_personal (
      result_key TEXT NOT NULL REFERENCES results(key) ON DELETE CASCADE,
      owner_id TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
      state TEXT NOT NULL DEFAULT 'idle', folders TEXT NOT NULL DEFAULT '[]',
      payload TEXT, review TEXT, updated REAL NOT NULL,
      PRIMARY KEY(result_key,owner_id));
    CREATE TABLE IF NOT EXISTS classification_exports (
      result_key TEXT NOT NULL REFERENCES results(key) ON DELETE CASCADE,
      owner_id TEXT NOT NULL, digest TEXT NOT NULL DEFAULT '', item_id INTEGER,
      claimed REAL NOT NULL DEFAULT 0,
      PRIMARY KEY(result_key,owner_id));
    ''')


def enqueue(key):
    with db.connect(True) as c:
        c.execute('''INSERT OR IGNORE INTO page_classifications
          (result_key,number,version,state,updated)
          SELECT p.result_key,p.number,?,'queued',? FROM pages p JOIN results r ON r.key=p.result_key
          WHERE p.result_key=? AND p.state='done' AND r.state NOT IN ('cancelled','cancelling')''',
          (VERSION,time.time(),key))


def aggregate(pages):
    done = [p['prediction'] for p in pages if p['state']=='done' and p['prediction']]
    summary = {}
    for kind in ('document_type','topic'):
        # Exclude covers/references from topic votes when substantive pages exist.
        substantive = [p for p in done if p['page_role']['label'] not in ('cover','references')]
        use = substantive or done
        if not use:
            continue
        scores = {label: sum(p[kind]['probabilities'].get(label,0) for p in use)/len(use)
                  for label in CATALOG[kind]}
        summary[kind] = {'label': max(scores,key=scores.get), 'scores': scores,
                         'pages_used': len(use)}
    return summary


def status(key, owner_id=''):
    with db.connect() as c:
        rows = c.execute('SELECT * FROM page_classifications WHERE result_key=? AND version=? ORDER BY number',
                         (key,VERSION)).fetchall()
        source = c.execute('SELECT state,page_count FROM results WHERE key=?',(key,)).fetchone()
        heartbeat = c.execute('SELECT * FROM classification_worker WHERE id=1').fetchone()
        personal = c.execute('SELECT * FROM classification_personal WHERE result_key=? AND owner_id=?',
                             (key,owner_id)).fetchone() if owner_id else None
    pages = [{'number':r['number'],'state':r['state'],'error':r['error'],
              'prediction':json.loads(r['payload']) if r['payload'] else None} for r in rows]
    counts = {s:sum(p['state']==s for p in pages) for s in ('queued','running','done','failed')}
    state = ('running' if counts['running'] else 'queued' if counts['queued'] else
             'partial' if counts['failed'] else 'done' if rows and source['state'] not in db.ACTIVE else 'waiting_ocr')
    if source['state'] in ('cancelled','cancelling'):
        state = 'paused'
    result = {'version':VERSION,'state':state,'counts':counts,'page_count':source['page_count'],
              'coverage':{'classified':counts['done'],'total':source['page_count']},
              'worker_online':bool(heartbeat and time.time()-heartbeat['heartbeat']<45),
              'worker_state':heartbeat['state'] if heartbeat else 'offline',
              'pages':pages,'summary':aggregate(pages),'catalog':CATALOG,
              'can_personalize':bool(owner_id),'personal':None}
    if personal:
        result['personal'] = {'state':personal['state'],'folders':json.loads(personal['folders']),
                              'suggestion':json.loads(personal['payload']) if personal['payload'] else None,
                              'review':json.loads(personal['review']) if personal['review'] else None}
    return result


def validate_folders(folders):
    if not isinstance(folders,list) or not 1 <= len(folders) <= 30:
        raise HTTPException(422,'ระบุโฟลเดอร์ 1–30 รายการ')
    cleaned = []
    for item in folders:
        if not isinstance(item,dict):
            raise HTTPException(422,'รูปแบบโฟลเดอร์ไม่ถูกต้อง')
        path,description = item.get('path',''),item.get('description','')
        if (not isinstance(path,str) or not isinstance(description,str) or not path.strip()
            or path.strip()=='__none__' or len(path)>240 or len(description)>240 or PurePosixPath(path).is_absolute()
            or '..' in PurePosixPath(path).parts or '\\' in path or ':' in path
            or any(ord(ch)<32 for ch in path+description)):
            raise HTTPException(422,'ใช้เส้นทางสัมพัทธ์ใน vault และคำอธิบายสั้น ๆ')
        cleaned.append({'path':path.strip(),'description':description.strip()})
    if len({p['path'] for p in cleaned}) != len(cleaned):
        raise HTTPException(422,'โฟลเดอร์ซ้ำกัน')
    return cleaned


def queue_personal(key,owner_id,folders):
    folders = validate_folders(folders)
    with db.connect(True) as c:
        c.execute('''INSERT INTO classification_personal(result_key,owner_id,generation,state,folders,updated)
          VALUES (?,?,1,'queued',?,?) ON CONFLICT(result_key,owner_id) DO UPDATE SET
          generation=generation+1,state='queued',folders=excluded.folders,payload=NULL,updated=excluded.updated''',
          (key,owner_id,json.dumps(folders,ensure_ascii=False),time.time()))


def review(key,owner_id,payload):
    clean = {}
    for kind in ('document_type','topic'):
        if payload.get(kind) not in CATALOG[kind]:
            raise HTTPException(422,'เลือกประเภทและหัวข้อจากรายการ')
        clean[kind] = payload[kind]
    destination = payload.get('destination','')
    if destination:
        validate_folders([{'path':destination}])
    if not isinstance(destination,str):
        raise HTTPException(422,'ปลายทางต้องเป็นข้อความ')
    clean.update(destination=destination,confirmed_at=time.time(),version=VERSION)
    with db.connect(True) as c:
        c.execute('''INSERT INTO classification_personal(result_key,owner_id,review,updated)
          VALUES (?,?,?,?) ON CONFLICT(result_key,owner_id) DO UPDATE SET
          review=excluded.review,updated=excluded.updated''',
          (key,owner_id,json.dumps(clean,ensure_ascii=False),time.time()))
    return clean


def export_payload(key,owner_id):
    data = status(key,owner_id)
    return {k:data[k] for k in ('version','coverage','pages','summary','personal')}


def sync_to_dataset(key,owner_id):
    """Retryable per-owner annotation; never changes OCR status or ZIP."""
    from . import dataset
    payload = export_payload(key,owner_id)
    # Export only completed snapshots (including partial OCR) or explicit reviews.
    if not payload['pages'] and not payload['personal']:
        return
    digest = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    now = time.time()
    with db.connect(True) as c:
        c.execute('INSERT OR IGNORE INTO classification_exports(result_key,owner_id) VALUES (?,?)',(key,owner_id))
        row = c.execute('SELECT * FROM classification_exports WHERE result_key=? AND owner_id=?',(key,owner_id)).fetchone()
        if row['digest']==digest or row['claimed']>now-600:
            return
        c.execute('UPDATE classification_exports SET claimed=? WHERE result_key=? AND owner_id=?',(now,key,owner_id))
    try:
        item_id = row['item_id'] or dataset.source_item(owner_id,key,cfg.DATA/'results'/key)['id']
        dataset._annotate(owner_id,item_id,'classification',{**payload,'snapshot_sha256':digest})
        with db.connect(True) as c:
            c.execute('UPDATE classification_exports SET digest=?,item_id=?,claimed=0 WHERE result_key=? AND owner_id=?',
                      (digest,item_id,key,owner_id))
    except Exception:
        with db.connect(True) as c:
            c.execute('UPDATE classification_exports SET claimed=0 WHERE result_key=? AND owner_id=?',(key,owner_id))
        # Deliberately omit exception text: remote responses can contain document data.
        import logging
        logging.getLogger(__name__).warning('classification_export_failed key=%s',key[:12])


def export_state(key,owner_id):
    if not owner_id:
        return 'sign_in_required'
    payload = export_payload(key,owner_id)
    digest = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    with db.connect() as c:
        row = c.execute('SELECT digest FROM classification_exports WHERE result_key=? AND owner_id=?',(key,owner_id)).fetchone()
    return 'saved' if row and row['digest']==digest else 'pending'


def register_owner(key,owner_id):
    """Remember verified ownership for retries even after the browser closes."""
    with db.connect(True) as c:
        c.execute('INSERT OR IGNORE INTO classification_exports(result_key,owner_id) VALUES (?,?)',(key,owner_id))


def sweep_exports():
    from . import dataset
    if not dataset.enabled():
        return
    with db.connect() as c:
        owners = c.execute('SELECT result_key,owner_id FROM classification_exports').fetchall()
    for row in owners:
        key,owner = row['result_key'],row['owner_id']
        try:
            with db.connect() as c:
                source = c.execute('SELECT state FROM results WHERE key=?',(key,)).fetchone()
            if source and source['state']=='completed' and db.claim_push(key,owner):
                try:
                    dataset.push_result(owner,key,cfg.DATA/'results'/key)
                except Exception:
                    db.release_push(key,owner)
            state = status(key,owner)
            if state['state'] in ('done','partial','paused') or (state['personal'] and state['personal']['review']):
                sync_to_dataset(key,owner)
        except Exception:
            logging.getLogger(__name__).warning('classification_sweep_failed key=%s',key[:12])
