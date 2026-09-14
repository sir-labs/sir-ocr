import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote

IMAGE_RE = re.compile(r'(!\[[^\]]*\]\()([^\s)]+)(\))|(<img\b[^>]*?\bsrc=["\x27])([^"\x27]+)(["\x27])', re.I)

def atomic_json(path, value):
    tmp = path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w') as f:
        json.dump(value,f,ensure_ascii=False,indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp,path)

def image_refs(text):
    return [m.group(2) or m.group(5) for m in IMAGE_RE.finditer(text)]

def rewrite_images(text, prefix):
    def replace(m):
        if m.group(2) is not None:
            return m.group(1)+prefix+m.group(2)+m.group(3)
        return m.group(4)+prefix+m.group(5)+m.group(6)
    return IMAGE_RE.sub(replace,text)

def normalize_math(text):
    # Keep code spans/fences verbatim; only standard LaTeX math delimiters change.
    parts = re.split(r'(```[\s\S]*?```|`[^`\n]*`)',text)
    for i in range(0,len(parts),2):
        parts[i] = re.sub(r'\\\[([\s\S]*?)\\\]',lambda m:'\n\n$$\n'+m[1].strip()+'\n$$\n\n',parts[i])
        parts[i] = re.sub(r'\\\((.*?)\\\)',lambda m:'$'+m[1].strip()+'$',parts[i],flags=re.S)
    return ''.join(parts)

def validate_images(text, base):
    for ref in image_refs(text):
        path = (base/unquote(ref)).resolve()
        if not path.is_relative_to(base.resolve()) or not path.is_file():
            raise ValueError('Missing or unsafe image reference in OCR output.')

def finish_page(staging, seconds, hashes):
    raw = staging/'raw'
    markdown = list(raw.glob('*.md'))
    json_files = list(raw.glob('*.json'))
    if len(markdown)!=1 or not json_files:
        raise ValueError('OCR did not produce page Markdown and JSON.')
    text = markdown[0].read_text()
    validate_images(text,raw)
    normalized = normalize_math(rewrite_images(text,'raw/'))
    (staging/'page.md').write_text(normalized)
    # Parse JSON now so a malformed result never becomes a completed page.
    data = json.loads(json_files[0].read_text())
    atomic_json(staging/'page.json', data)
    atomic_json(staging/'complete.json',{'seconds':seconds,'model_hashes':hashes,'finished_at':time.time()})
    for path in staging.rglob('*'):
        if path.is_file():
            with path.open('rb') as f:
                os.fsync(f.fileno())
    fd = os.open(staging,os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def package(base, result, pages):
    text=[]
    entries=[]
    hashes=None
    for page in pages:
        n=page['number']
        directory=base/'pages'/f'{n:04d}'
        complete=json.loads((directory/'complete.json').read_text())
        if hashes is not None and hashes != complete['model_hashes']:
            raise ValueError('Model weights changed between pages; refusing mixed-version ZIP.')
        hashes=complete['model_hashes']
        md=(directory/'page.md').read_text()
        validate_images(md,directory)
        text.append(f'<!-- Page {n} -->\n\n'+rewrite_images(md,f'pages/{n:04d}/'))
        entries.append({**dict(page),'model_hashes':complete['model_hashes'],'finished_at':complete['finished_at']})
    document='\n\n---\n\n'.join(text)+'\n'
    validate_images(document,base)
    (base/'document.md').write_text(document)
    manifest={'schema_version':1,'pdf_sha256':result['pdf_hash'],'result_key':result['key'],
              'model_hashes':hashes,'configuration':json.loads(result['config']),
              'created_at':result['created'],'completed_at':time.time(),'status':'completed','pages':entries}
    atomic_json(base/'manifest.json',manifest)
    tmp=base/'result.zip.tmp'
    with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED) as z:
        for name in ['document.md','manifest.json']:
            z.write(base/name,name)
        for path in sorted((base/'pages').rglob('*')):
            if path.is_file() and path.name != 'source.png':
                z.write(path,str(path.relative_to(base)))
    with tmp.open('rb') as f:
        os.fsync(f.fileno())
    os.replace(tmp,base/'result.zip')
