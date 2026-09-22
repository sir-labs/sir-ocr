"""Local synthetic preview with a fake identity/store; NEVER a production entrypoint."""
import argparse
import json
import os
import sys
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args()
    data=args.data_dir.resolve()
    fixture=json.loads((data/'fixture.json').read_text())
    if fixture.get('synthetic') is not True or not str(data).startswith('/tmp/sir-ocr-classify.'):
        parser.error('Only a synthetic classification_smoke fixture in /tmp is allowed.')
    os.environ['OCR_DATA_DIR']=str(data)
    os.environ['OCR_MIN_FREE_BYTES']='0'
    os.environ['OCR_TRUSTED_PROXIES']='127.0.0.1/32'
    os.environ['OCR_PUBLIC_ORIGIN']=f'http://127.0.0.1:{args.port}'
    os.environ['DATA_SERVICE_TOKEN']=''
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from app.api import app
    from app import dataset
    import uvicorn
    dataset.enabled=lambda:True
    dataset.push_result=lambda *args:1
    dataset.source_item=lambda *args:{'id':1}
    def annotate(owner,item,kind,payload):
        (data/'preview-annotation.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2))
    dataset._annotate=annotate
    @app.middleware('http')
    async def fixture_identity(request,call_next):
        request.scope['headers']=[(k,v) for k,v in request.scope['headers'] if k.lower()!=b'x-auth-user-id']+[(b'x-auth-user-id',b'synthetic-preview-user')]
        return await call_next(request)
    job=fixture['job']
    print(f"SYNTHETIC PREVIEW: http://127.0.0.1:{args.port}/view#job={job['id']}&token={job['token']}",flush=True)
    uvicorn.run(app,host='127.0.0.1',port=args.port,proxy_headers=False,access_log=False)


if __name__=='__main__':main()
