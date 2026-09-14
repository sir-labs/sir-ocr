import hashlib
import json
import os
from pathlib import Path

DATA = Path(os.getenv('OCR_DATA_DIR', '/data'))
CACHE = Path(os.getenv('PADDLE_PDX_CACHE_HOME', '/cache/paddlex'))
MAX_BYTES = int(os.getenv('OCR_MAX_BYTES', 50 * 1024**2))
MAX_PAGES = int(os.getenv('OCR_MAX_PAGES', 500))
MIN_FREE = int(os.getenv('OCR_MIN_FREE_BYTES', 10 * 1024**3))
MAX_QUEUE = int(os.getenv('OCR_MAX_QUEUE', 20))
MAX_IP = int(os.getenv('OCR_MAX_IP', 2))
IDLE_SECONDS = int(os.getenv('OCR_IDLE_SECONDS', 300))
GPU_FREE_MIB = int(os.getenv('OCR_GPU_FREE_MIB', 9216))
AMQP_URL = os.getenv('OCR_AMQP_URL', '')  # empty: no broker, the worker polls SQLite
POLL_SECONDS = int(os.getenv('OCR_POLL_SECONDS', 30))
PAGE_TIMEOUT = int(os.getenv('OCR_PAGE_TIMEOUT', 180))
MODEL_TIMEOUT = int(os.getenv('OCR_MODEL_TIMEOUT', 1800))
GPU_WAIT_TIMEOUT = int(os.getenv('OCR_GPU_WAIT_TIMEOUT', 900))
JOB_BASE_SECONDS = int(os.getenv('OCR_JOB_BASE_SECONDS', 120))
JOB_PAGE_SECONDS = int(os.getenv('OCR_JOB_PAGE_SECONDS', 60))
STALL_SECONDS = int(os.getenv('OCR_STALL_SECONDS', 300))
CHILD_STDERR = os.getenv('OCR_CHILD_STDERR', '')
CONFIG = {
    'model': 'PaddleOCR-VL-1.6-0.9B', 'pipeline_version': 'v1.6',
    'paddle': '3.2.1', 'paddleocr': '3.7.0', 'paddlex': '3.7.2',
    'layout_model': 'PP-DocLayoutV3', 'dpi': int(os.getenv('OCR_DPI', 200)),
    'max_new_tokens': 4096, 'use_doc_orientation_classify': False,
    'use_doc_unwarping': False, 'use_ocr_for_image_block': True,
    'use_chart_recognition': False, 'use_queues': False,
    'export_version': 2,
}
CONFIG_JSON = json.dumps(CONFIG, sort_keys=True, separators=(',', ':'))

def result_key(pdf_hash):
    return hashlib.sha256((pdf_hash + '\n' + CONFIG_JSON).encode()).hexdigest()
