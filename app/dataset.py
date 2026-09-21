"""Pushes a finished OCR result to sir-data, the central store for sir-labs.

/data here is scratch — results are keyed by PDF hash, shared between users and pruned. What
a user should keep goes to sir-data, owned by the sir-auth user nginx identified, so it
outlives this container and shows up next to their other data.

stdlib only: this module runs in both images, and adding a dependency (plus a lock rebuild)
to make two HTTP calls is not worth it.
"""
import json
import logging
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger('dataset')

URL = os.environ.get('DATA_URL', 'http://sir-data-api-1:8000').rstrip('/')
TOKEN = os.environ.get('DATA_SERVICE_TOKEN', '')
NAME = os.environ.get('DATASET_NAME', 'ocr')
TIMEOUT = 120


def enabled():
    return bool(TOKEN)


def _call(path, owner_id, body=b'', content_type=None, method='POST'):
    request = urllib.request.Request(f'{URL}{path}', data=body, method=method, headers={
        'Authorization': f'Bearer {TOKEN}', 'X-Data-Service': 'sir-ocr',
        'X-On-Behalf-Of': owner_id, **({'Content-Type': content_type} if content_type else {})})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read() or b'null')


def _multipart(filename, content):
    boundary = uuid.uuid4().hex
    media_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: {media_type}\r\n\r\n').encode()
    return head + content + f'\r\n--{boundary}--\r\n'.encode(), f'multipart/form-data; boundary={boundary}'


def _upload(owner_id, dataset_id, path: Path, key):
    body, content_type = _multipart(path.name, path.read_bytes())
    return _call(f'/datasets/{dataset_id}/items?source=sir-ocr&source_job_id={key}',
                 owner_id, body, content_type)


def _annotate(owner_id, item_id, kind, payload):
    _call(f'/items/{item_id}/annotations?kind={kind}', owner_id,
          json.dumps(payload).encode(), 'application/json')


def push_result(owner_id, key, base: Path):
    """The source PDF as the item, the OCR output as annotations, the ZIP alongside it
    (page images only exist inside the ZIP). Raises so the caller can un-mark the push."""
    dataset_id = _call(f'/datasets?name={NAME}', owner_id)['id']
    item = _upload(owner_id, dataset_id, base / 'source.pdf', key)
    document = base / 'document.md'
    if document.exists():
        _annotate(owner_id, item['id'], 'markdown', {'document': document.read_text()})
    manifest = base / 'manifest.json'
    if manifest.exists():
        _annotate(owner_id, item['id'], 'manifest', json.loads(manifest.read_text()))
    archive = base / 'result.zip'
    if archive.exists():
        _upload(owner_id, dataset_id, archive, key)
    log.info('dataset_push key=%s item=%s', key[:12], item['id'])
    return item['id']
