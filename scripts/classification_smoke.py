"""Synthetic post-OCR fixture + real CPU inference (does not run OCR itself)."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import classification as cl, config as cfg, db
from app.artifacts import finish_page, package
from app.classifier_worker import Classifier, process_one
import pymupdf


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    args=parser.parse_args()
    if (args.data_dir/'queue.sqlite3').exists():
        parser.error('Use a fresh temporary directory, never production OCR data.')
    cfg.DATA=args.data_dir.resolve();cfg.MIN_FREE=0;db.init()
    texts=[
        '# บทพิสูจน์ NP-Completeness\n\nพิสูจน์ว่า Vertex Cover เป็น NP-complete โดยแสดงว่าอยู่ใน NP และทำ polynomial-time reduction จาก Clique ให้ G มี n vertices จะมี clique ขนาด k ก็ต่อเมื่อ complement graph มี vertex cover ขนาด n-k การตรวจสอบ certificate ทำได้ในเวลาพหุนาม จึงสรุปว่า Vertex Cover เป็น NP-complete.',
        '# แบบฝึกหัด Algorithms\n\nข้อ 1 จงพิสูจน์ว่า 3-SAT เป็นปัญหาใน NP โดยอธิบายวิธีตรวจสอบ certificate ข้อ 2 จงสร้าง polynomial-time reduction จาก 3-SAT ไปยัง Clique ข้อ 3 จงวิเคราะห์ time complexity ของอัลกอริทึมที่ออกแบบ พร้อมอธิบายความแตกต่างระหว่าง P, NP และ NP-hard.',
        '# ตัวอย่าง Dynamic Programming\n\nตัวอย่างการหา Fibonacci ด้วย dynamic programming กำหนด F(0)=0 และ F(1)=1 จากนั้นคำนวณ F(2)=1, F(3)=2, F(4)=3, F(5)=5 เก็บคำตอบในตารางเพื่อหลีกเลี่ยงการคำนวณซ้ำ ได้ time complexity O(n) และลด space เป็น O(1) โดยเก็บเฉพาะสองค่าล่าสุด.',
    ]
    document=pymupdf.open()
    for i in range(len(texts)):
        document.new_page().insert_text((72,72),f'Synthetic classification fixture, page {i+1}')
    source=cfg.DATA/'incoming'/'fixture.pdf';source.write_bytes(document.tobytes())
    import hashlib
    job=db.register(source,hashlib.sha256(source.read_bytes()).hexdigest(),len(texts),'fixture')
    key=db.result_key(job['id'],job['token']);base=cfg.DATA/'results'/key
    for i,text in enumerate(texts,1):
        page=base/'pages'/f'{i:04d}';raw=page/'raw';raw.mkdir(parents=True)
        (raw/'fixture.md').write_text(text)
        (raw/'fixture.json').write_text('{}')
        finish_page(page,0.01,{'fixture':'synthetic-not-ocr'})
        with db.connect(True) as c:
            c.execute("UPDATE pages SET state='done',seconds=.01 WHERE result_key=? AND number=?",(key,i))
    with db.connect() as c:
        row=dict(c.execute('SELECT * FROM results WHERE key=?',(key,)).fetchone())
        pages=[dict(p) for p in c.execute('SELECT * FROM pages WHERE result_key=?',(key,))]
    package(base,row,pages)
    with db.connect(True) as c:
        c.execute("UPDATE results SET state='completed',updated=? WHERE key=?",(time.time(),key))
    cl.enqueue(key)
    started=time.perf_counter();model=Classifier();load=time.perf_counter()-started
    timings=[]
    for _ in texts:
        started=time.perf_counter();process_one(model);timings.append(time.perf_counter()-started)
    result=cl.status(key)
    assert result['counts']['done']==len(texts),result
    report={'synthetic':True,'device':'cpu','load_seconds':load,'seconds_per_page':timings,
            'result':result,'job':job}
    (cfg.DATA/'fixture.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'load_seconds':load,'seconds_per_page':timings,
        'labels':[{k:p['prediction'][k]['label'] for k in cl.CATALOG} for p in result['pages']]},ensure_ascii=False),flush=True)
    print('Fixture:',cfg.DATA/'fixture.json',flush=True)


if __name__=='__main__':main()
