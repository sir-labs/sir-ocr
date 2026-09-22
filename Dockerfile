FROM python:3.12-slim-bookworm AS common
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 OCR_DATA_DIR=/data PIP_DEFAULT_TIMEOUT=180 PIP_RETRIES=8
WORKDIR /app
COPY requirements-api.lock ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements-api.lock
RUN useradd --uid 1000 --create-home ocr

FROM common AS worker-deps
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 libgl1 && rm -rf /var/lib/apt/lists/*
COPY requirements-worker.lock ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements-worker.lock

FROM common AS api
COPY app ./app
USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log"]

FROM worker-deps AS worker
ENV PADDLE_PDX_CACHE_HOME=/cache/paddlex HF_HOME=/cache/huggingface PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True OMP_NUM_THREADS=8 NVIDIA_VISIBLE_DEVICES=0 NVIDIA_DRIVER_CAPABILITIES=compute,utility
COPY app ./app
USER 1000:1000
CMD ["python", "-m", "app.worker"]

FROM common AS classifier
RUN apt-get update && apt-get install -y --no-install-recommends git libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements-classifier.lock ./
RUN --mount=type=cache,target=/root/.cache/pip pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements-classifier.lock
ENV HF_HOME=/cache/huggingface CUDA_VISIBLE_DEVICES="" CLASSIFIER_THREADS=4
COPY app ./app
USER 1000:1000
CMD ["python", "-m", "app.classifier_worker"]
