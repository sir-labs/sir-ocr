"""Spawn-only GPU child; no tokens or recognized content are emitted to logs."""
import hashlib
import os
import time
from pathlib import Path
from . import config as cfg
from .artifacts import finish_page

def model_hashes():
    hashes={}
    for name in ['PaddleOCR-VL-1.6','PP-DocLayoutV3']:
        directory=cfg.CACHE/'official_models'/name
        for path in sorted(directory.rglob('*')):
            if path.is_file() and not any(p.startswith('.') for p in path.relative_to(directory).parts):
                with path.open('rb') as f:
                    hashes[f'{name}/{path.relative_to(directory)}']=hashlib.file_digest(f,'sha256').hexdigest()
    if not any(k.endswith('model.safetensors') for k in hashes) or not any(k.endswith('inference.pdiparams') for k in hashes):
        raise RuntimeError('Model cache is incomplete.')
    return hashes

def error_code(error):
    text=str(error).lower()
    if 'out of memory' in text or 'resourceexhausted' in text or 'cuda_error_out_of_memory' in text:
        return 'gpu_out_of_memory'
    return 'inference_error'

def child(connection):
    # Paddle may print generated strings in third-party diagnostic messages.
    # Keep model stdout/stderr out of container logs; communicate only structured states.
    null=os.open(os.devnull,os.O_WRONLY)
    os.dup2(null,1)
    os.dup2(null,2)
    os.close(null)
    try:
        import paddle
        from paddleocr import PaddleOCRVL
        if paddle.__version__ != cfg.CONFIG['paddle'] or not paddle.device.is_compiled_with_cuda():
            raise RuntimeError('Unexpected Paddle GPU runtime.')
        model=PaddleOCRVL(
            pipeline_version=cfg.CONFIG['pipeline_version'],vl_rec_model_name=cfg.CONFIG['model'],
            device='gpu:0',use_doc_orientation_classify=False,use_doc_unwarping=False,
            use_ocr_for_image_block=True,use_chart_recognition=False,use_queues=False)
        hashes=model_hashes()
        connection.send({'state':'ready','model_hashes':hashes})
        while True:
            command=connection.recv()
            if command is None:
                break
            try:
                start=time.monotonic()
                staging=Path(command['staging'])
                raw=staging/'raw'
                raw.mkdir()
                count=0
                for result in model.predict(command['source'],max_new_tokens=cfg.CONFIG['max_new_tokens']):
                    result.save_to_json(str(raw))
                    result.save_to_markdown(str(raw))
                    count+=1
                if count != 1:
                    raise RuntimeError('Unexpected prediction count.')
                seconds=time.monotonic()-start
                finish_page(staging,seconds,hashes)
                connection.send({'state':'done','seconds':seconds,'model_hashes':hashes})
            except Exception as e:
                connection.send({'state':'error','error':error_code(e),'exception_type':type(e).__name__})
                if error_code(e)=='gpu_out_of_memory':
                    break
    except BaseException as e:
        try:
            connection.send({'state':'error','error':error_code(e),'exception_type':type(e).__name__})
        except (BrokenPipeError,OSError):
            pass
    finally:
        connection.close()
