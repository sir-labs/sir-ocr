# SIR OCR

Upload a PDF at **https://ocr.sir-labs.com**, follow page-level progress, then download Markdown, JSON and images in one ZIP. No account is required. Every upload receives a separate capability token, including duplicate uploads. Keep the job link private.

## Run

Requires Linux, Docker Compose, NVIDIA Container Toolkit, an NVIDIA GPU with **9 GiB free VRAM**, and at least **10 GiB free disk** in the data filesystem. Initial model and runtime downloads need additional space. The tested GPU is an RTX 5070 12 GB.

```sh
cp .env.example .env
# Set absolute storage paths and create them with the configured UID/GID.
docker compose build
docker compose up -d --wait
```

The base Compose file binds the API to `127.0.0.1:8000`. Model weights download on the first job into the writable cache, never during image build. To use an existing PaddleX cache, set `OCR_MODEL_CACHE` to its root (the directory containing `official_models`). Nothing imports historical documents.

Pinned runtime: Python 3.12, Paddle GPU 3.2.1 / CUDA 12.9, PaddleOCR 3.7.0, PaddleX 3.7.2. Pipeline: PaddleOCR-VL-1.6-0.9B with PP-DocLayoutV3, 200 DPI, 4096 output tokens, image-block OCR on, chart recognition / orientation / unwarping / queues off. [Official pipeline documentation](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PaddleOCR-VL.html).

## API

- `POST /api/jobs`: multipart form with one `file`; returns `{id, token, reused}`.
- `GET /api/jobs/{id}`: progress, page status, queue position and worker availability.
- `GET /api/jobs/{id}/download`: ZIP, only once every page succeeds.
- `POST /api/jobs/{id}/retry`: queue failed work again, retaining completed pages.

Use `Authorization: Bearer <token>` for all per-job endpoints. An authorized status response also sets a one-day HttpOnly, SameSite=Strict cookie scoped only to that job's download path (Secure in production), enabling native streaming downloads without holding the full ZIP in browser memory. Tokens are hashed in SQLite. The browser stores capabilities locally; shared links put the token in the URL fragment, which is not sent to HTTP servers or proxy access logs. There is no public list or delete endpoint. API access logging is disabled; worker logs contain job-hash prefixes, page numbers, timing and error codes, never PDF text or tokens.

Limits: 50 MiB, 500 pages, 2 unfinished distinct documents/IP, 20 unfinished GPU jobs globally, 10 create/retry requests/minute/IP. The 50 MiB limit is checked both while receiving the body and while reading the file. Damaged/encrypted PDFs are rejected. PDF validation runs in isolated processes (at most two concurrently), each bounded to 768 MiB address space, 20 CPU seconds and 30 seconds wall time, keeping MuPDF out of API threads. Pages above 40 megapixels at the configured DPI are rejected explicitly, rather than silently downsampled. New uploads and retries stop below 10 GiB free space. Existing originals/results remain until administrator deletion.

## Queue and recovery

SQLite WAL and `BEGIN IMMEDIATE` serialize admission/deduplication. The key is SHA-256 of the PDF hash plus canonical model/configuration JSON. A lifetime OS file lock prevents multiple workers. The supervisor writes a heartbeat every five seconds, serially processes FIFO documents, and restarts interrupted work after acquiring the lock. Each page is fsynced and atomically renamed before its SQLite completion record. Completed page markers reconcile a crash between rename and database commit.

The GPU lives in a spawned child, keeping API/heartbeat responsive during initialization. The child loads once, handles one page at a time, and exits after five idle minutes. Before loading it waits for 9216 MiB free VRAM. An OOM terminates the child and retries that page once at the same resolution. Other failures preserve completed pages; the user can retry the remainder. Model initialization times out after 30 minutes; page inference after 10 minutes. The model/config cannot be changed mid-document: restore its original configuration to resume.

ZIP layout:

```text
document.md
manifest.json
pages/0001/page.md
pages/0001/page.json
pages/0001/complete.json
pages/0001/raw/*.md
pages/0001/raw/*_res.json
pages/0001/raw/imgs/*
```

Raw outputs stay separate. Export normalizes `\(...\)` and `\[...\]` outside code spans, prefixes page-relative image paths, validates referenced image files, preserves numeric page order, and records exact weight-file hashes, configuration, timestamps and page states. ZIP creation fails if page weights differ. Original PDFs remain on the host and are not included in downloads.

## Production and maintenance

`compose.sir.yaml` adds routing on `sir-server_sir-net`. The shared watcher needs support for `proxy.max_body_size`; sir-server commit `b10de81` adds it. Only the OCR host receives a 51 MiB multipart request allowance; the application file limit remains 50 MiB.

Pushes to `main` and manual dispatch run tests, build, then deploy using `[self-hosted, sir-labs]`. There is deliberately **no pull-request trigger** on the host runner. Repository maintainers with push access can execute host code; restrict that access. CI holds the shared `/tmp/sir-deploy.lock` while deploying and checks container health plus public HTTPS health. Builds complete before replacing services. Data/cache paths come from the host-owned `$HOME/.config/sir-ocr/deploy.env`, with defaults outside the checkout. `scripts/deploy.sh` derives exact current nginx/tunnel IPs; rerun deployment if either proxy's IP changes.

```sh
scripts/deploy.sh build
scripts/deploy.sh deploy
# With the same Compose environment selected:
docker compose logs --tail 100 worker
docker compose exec api python -m app.admin list
docker compose stop worker
docker compose exec api python -m app.admin delete RESULT_KEY --confirm RESULT_KEY
docker compose start worker
```

Deletion removes all tokens that refer to that shared document. Back up the entire data directory with both services stopped, or use SQLite's online backup API plus a consistent filesystem snapshot. Never store runtime data inside an Actions checkout. See [verification](docs/verification.md) for measured acceptance evidence and remaining operational constraints.

## Test

```sh
uv venv --python 3.12
uv pip install -r requirements-test.lock
.venv/bin/python -m pytest -q
```
