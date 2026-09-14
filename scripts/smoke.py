"""Upload duplicate real PDFs, wait, download and validate ZIPs without logging tokens."""
import argparse
import concurrent.futures
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path
import httpx
from app.artifacts import image_refs
p=argparse.ArgumentParser()
p.add_argument('--url',required=True)
p.add_argument('--pdf',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
blob=a.pdf.read_bytes()
def upload():
    with httpx.Client(timeout=120) as c:
        r=c.post(a.url+'/api/jobs',files={'file':('acceptance.pdf',blob,'application/pdf')})
        r.raise_for_status()
        return r.json()
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    jobs=list(pool.map(lambda _:upload(),range(2)))
private=a.output/'capabilities.json'
private.write_text(json.dumps(jobs));private.chmod(0o600)
print(json.dumps({'jobs':[j['id'] for j in jobs],'reused':[j['reused'] for j in jobs]}),flush=True)
j=jobs[0];headers={'Authorization':'Bearer '+j['token']}
start=time.monotonic();previous=None
with httpx.Client(timeout=120) as c:
    while time.monotonic()-start<2400:
        r=c.get(a.url+'/api/jobs/'+j['id'],headers=headers);r.raise_for_status();status=r.json()
        summary={k:status[k] for k in ['state','completed_pages','page_count','worker_online']}
        if summary!=previous:
            print(json.dumps(summary),flush=True);previous=summary
        if status['state']=='failed':
            raise RuntimeError(status)
        if status['state']=='completed':
            break
        time.sleep(2)
    else:
        raise TimeoutError('OCR job did not finish')
    downloaded=[]
    for j in jobs:
        r=c.get(a.url+'/api/jobs/'+j['id']+'/download',headers={'Authorization':'Bearer '+j['token']})
        r.raise_for_status();downloaded.append(r.content)
    assert downloaded[0]==downloaded[1]
    r=c.get(a.url+'/api/jobs/'+jobs[0]['id'],headers={'Authorization':'Bearer '+jobs[1]['token']})
    assert r.status_code==404
archive=a.output/'result.zip';archive.write_bytes(downloaded[0])
with zipfile.ZipFile(io.BytesIO(downloaded[0])) as z:
    assert z.testzip() is None
    manifest=json.loads(z.read('manifest.json'))
    assert manifest['pdf_sha256']==hashlib.sha256(blob).hexdigest()
    assert len(manifest['pages'])==status['page_count']
    assert all(p['state']=='done' for p in manifest['pages'])
    document=z.read('document.md').decode()
    positions=[document.index(f'<!-- Page {n} -->') for n in range(1,status['page_count']+1)]
    assert positions==sorted(positions)
    refs=image_refs(document)
    assert all(ref in z.namelist() for ref in refs)
    for n in range(1,status['page_count']+1):
        json.loads(z.read(f'pages/{n:04d}/page.json'))
    # Generated fixture has chart images; assert actual image extraction occurred.
    assert refs, 'Expected at least one extracted chart/image'
    z.extractall(a.output/'extracted')
evidence={'url':a.url,'status':'passed','page_count':status['page_count'],'image_references':len(refs),
          'pdf_sha256':manifest['pdf_sha256'],'zip_sha256':hashlib.sha256(downloaded[0]).hexdigest(),
          'duplicate_uploads_share_identical_zip':True,'cross_job_token_rejected':True,
          'page_attempts':[p['attempts'] for p in status['pages']],
          'wall_seconds':round(time.monotonic()-start,2)}
(a.output/'evidence.json').write_text(json.dumps(evidence,indent=2))
print(json.dumps(evidence),flush=True)
