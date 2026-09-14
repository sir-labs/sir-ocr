"""Live negative/large-upload checks using generated, disposable PDF fixtures."""
import argparse
import json
import time
from pathlib import Path
import httpx
import pymupdf as fitz
p=argparse.ArgumentParser();p.add_argument('--url',required=True);p.add_argument('--pdf',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
with httpx.Client(timeout=180) as c:
    start=time.monotonic()
    with a.pdf.open('rb') as f:
        r=c.post(a.url+'/api/jobs',files={'file':('large.pdf',f,'application/pdf')})
    assert r.status_code==201,r.status_code
    j=r.json();cap=a.output/'capability.json';cap.write_text(json.dumps(j));cap.chmod(0o600)
    evidence={'valid_large_upload_bytes':a.pdf.stat().st_size,'upload_status':r.status_code,'upload_seconds':round(time.monotonic()-start,2)}
    print(json.dumps(evidence),flush=True)
    tests={}
    tests['invalid_pdf']=c.post(a.url+'/api/jobs',files={'file':('invalid.pdf',b'not a PDF','application/pdf')}).status_code
    d=fitz.open();d.new_page()
    encrypted=d.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,user_pw='test',owner_pw='owner')
    tests['encrypted_pdf']=c.post(a.url+'/api/jobs',files={'file':('encrypted.pdf',encrypted,'application/pdf')}).status_code
    tests['oversized_pdf']=c.post(a.url+'/api/jobs',files={'file':('large.pdf',b'x'*(50*1024**2+1),'application/pdf')}).status_code
    many=fitz.open()
    for _ in range(501):many.new_page()
    tests['too_many_pages']=c.post(a.url+'/api/jobs',files={'file':('501.pdf',many.tobytes(),'application/pdf')}).status_code
    tests['wrong_token']=c.get(a.url+'/api/jobs/'+j['id'],headers={'Authorization':'Bearer invalid'}).status_code
    assert tests=={'invalid_pdf':400,'encrypted_pdf':400,'oversized_pdf':413,'too_many_pages':400,'wrong_token':404},tests
    headers={'Authorization':'Bearer '+j['token']}
    for _ in range(150):
        d=c.get(a.url+'/api/jobs/'+j['id'],headers=headers).json()
        if d['state']=='completed':break
        if d['state']=='failed':raise RuntimeError(d)
        time.sleep(2)
    else:raise TimeoutError(d)
    evidence.update({'negative_tests':tests,'large_pdf_completed_pages':d['completed_pages']})
    (a.output/'evidence.json').write_text(json.dumps(evidence,indent=2))
    print(json.dumps(evidence),flush=True)
