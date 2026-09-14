"""Prove Paddle CUDA execution and actual OOM classification, without model output."""
import json
import paddle
from app.inference import error_code
assert paddle.__version__=='3.2.1'
assert paddle.device.is_compiled_with_cuda()
paddle.set_device('gpu:0')
x=paddle.to_tensor([1.,2.,3.])
assert float(paddle.sum(x*x))==14.
paddle.device.cuda.synchronize()
try:
    # Exceeds total memory of the verified 12 GiB GPU: fail allocation immediately.
    paddle.empty([8*1024**3],dtype='float32')
except Exception as e:
    assert error_code(e)=='gpu_out_of_memory',type(e).__name__
    oom=type(e).__name__
else:
    raise RuntimeError('Expected OOM on the acceptance GPU')
print(json.dumps({'paddle':paddle.__version__,'device':paddle.device.cuda.get_device_name(),
                  'gpu_tensor_result':14.0,'actual_oom_exception':oom,'classification':'gpu_out_of_memory'}))
