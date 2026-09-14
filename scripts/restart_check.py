"""Destructive restart test for an explicitly named disposable worker container."""
import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
import httpx
p=argparse.ArgumentParser()
p.add_argument('--url',required=True);p.add_argument('--pdf',type=Path,required=True)
p.add_argument('--container',required=True);p.add_argument('--data',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
if not a.container.startswith('sir-ocr-preflight-'):
    raise SystemExit('This script only restarts sir-ocr-preflight-* test containers.')
a.output.mkdir(parents=True,exist_ok=True)
with httpx.Client(timeout=120) as c:
    r=c.post(a.url+'/api/jobs',files={'file':('restart.pdf',a.pdf.read_bytes(),'application/pdf')});r.raise_for_status();j=r.json()
    path=a.output/'capability.json';path.write_text(json.dumps(j));path.chmod(0o600)
    headers={'Authorization':'Bearer '+j['token']}
    baseline=None
    start=time.monotonic()
    while time.monotonic()-start<1200:
        r=c.get(a.url+'/api/jobs/'+j['id'],headers=headers);r.raise_for_status();d=r.json()
        if d['state']=='failed':raise RuntimeError(d)
        if baseline is None and 1<=d['completed_pages']<d['page_count']:
            baseline={str(p.relative_to(a.data)):hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in a.data.glob('results/*/pages/*/complete.json')}
            completed_before=d['completed_pages']
            subprocess.run(['docker','kill','--signal','KILL',a.container],check=True)
            subprocess.run(['docker','start',a.container],check=True)
            print(json.dumps({'forced_kill_after_pages':completed_before,'preserved_markers':len(baseline)}),flush=True)
        if d['state']=='completed':
            assert baseline is not None,'PDF completed before a restart could be tested'
            for name,digest in baseline.items():
                assert hashlib.sha256((a.data/name).read_bytes()).hexdigest()==digest
            evidence={'status':'passed','pages':d['page_count'],'completed_before_kill':completed_before,
                      'completed_after_restart':d['completed_pages'],'completed_markers_unchanged':True,
                      'attempts':[p['attempts'] for p in d['pages']]}
            (a.output/'evidence.json').write_text(json.dumps(evidence,indent=2))
            print(json.dumps(evidence),flush=True)
            break
        time.sleep(.5)
    else:raise TimeoutError('Restart test timed out')
