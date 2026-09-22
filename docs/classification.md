# Post-OCR classification

Each successful page is queued for a separate CPU classifier. OCR completion,
ZIP downloads, retries, cancellation and the Paddle GPU process do not depend on
classifier availability. Viewing the classification panel also queues completed
pages of older documents. Unviewed historical documents are not backfilled.

`app.classifier_worker` runs pinned OpenThai-SystemOne on CPU (4 threads), with
three option orders and three questions per page: document type, topic, and page
role. No GPU is assigned to this container. Text is capped at 20,000 characters
and 1,800 model-state tokens; truncation is recorded. Very short text is marked
unknown, not treated as a successful confident classification.

## UI and API

- Both the status page and reader show separate classification progress and a
  document-level summary. A completed OCR job remains downloadable during this work.
- The reader filters completed pages by exercise, example, proof, definition,
  explanation, cover, references, other, or unclassified. Its existing Markdown,
  images and math renderer is unchanged.
- The document summary averages page distributions (covers and references excluded
  when substantive pages exist). These scores are not calibrated document accuracy.
- Signed-in users can submit up to 30 relative folder paths with descriptions,
  ask for a suggestion, change document type/topic/destination, and confirm.
  Folder inference samples up to five classified pages across the document.
  Mapping suggestions are snapshots; resubmit after more pages complete to refresh.
- This service does not mount or browse a user's local Obsidian vault and never
  moves files. The path list is supplied by the user; copy the selected path to
  use with their local filing workflow.
- A JSON download contains page predictions, model/taxonomy version, coverage,
  distributions, abstention, truncation and the current user's review, if any.

Endpoints require the existing job capability in `Authorization: Bearer ...`:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/jobs/{id}/classification` | Progress, page labels, summary and own review |
| `POST .../classification/retry` | Retry failed classifications, not OCR |
| `POST .../classification/folders` | Queue private `{"folders":[{"path":"Learning/Algorithms","description":"Algorithms"}]}` |
| `POST .../classification/review` | Confirm `{"document_type":"lecture","topic":"algorithms","destination":"Learning/Algorithms"}` |

Folder/review writes additionally require a user identity set by a trusted proxy
and configured sir-data credentials. Directly spoofing `X-Auth-User-Id` is rejected.
The same verified-identity rule now protects the existing source-document export.

## Storage and recovery

Generic classifications are deduplicated by OCR result, page number and taxonomy
version. Human reviews and folder lists are keyed separately by result and user.
The worker never knows user credentials and has no direct central-store access.

SQLite stores queue state and temporary annotations alongside existing scratch
results. The API exports `classification` annotations through the sir-data API
using the current user's identity and an existing or SHA-deduplicated source item.
No new durable-data mount is introduced. A digest avoids unchanged re-exports;
at-least-once delivery may repeat an annotation after a network timeout. Consumers
can deduplicate with `snapshot_sha256`. Export failures do not affect OCR.

Verified owners are registered when they poll status/classification. A 15-second
API sweep keeps exports moving after their browser closes; wholly anonymous jobs
have no user-specific durable export. Failed exports remain pending and retry.

Worker restarts requeue interrupted page/mapping work under an exclusive process
lock. Failed inference is shown separately and retried explicitly. Cancellation
stops dispatching new generic page classifications; an in-flight CPU call may
finish. A new folder request cannot be overwritten by an older in-flight response.
Stop both `worker` and `classifier` before host maintenance deletion.

## Verification

Unit/integration suite: `.venv/bin/python -m pytest -q` (48 passed, one existing
RabbitMQ-dependent test skipped when no broker is configured).
Coverage includes authentication, two-owner isolation, OCR survival after classifier
failure, retry, deduplication, cancelled work, stale mapping responses and export
retry/sweep after the browser closes.

Build: `docker build --target classifier -t sir-ocr-classifier:review .` and the
existing API target. Compose validation uses the existing production overlay.

Real CPU smoke fixture (never point at production scratch):

```bash
uv venv .venv-classifier --python 3.12
uv pip sync --python .venv-classifier/bin/python \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --index-strategy unsafe-best-match requirements-classifier.lock
demo_dir=$(mktemp -d /tmp/sir-ocr-classify.XXXXXX)
.venv-classifier/bin/python scripts/classification_smoke.py --data-dir "$demo_dir"
.venv-classifier/bin/python scripts/preview_classification.py --data-dir "$demo_dir"
```

The preview is localhost-only with synthetic identity and a fake local central
store; it must never be deployed. For folder inference in the preview, run
`OCR_DATA_DIR="$demo_dir" .venv-classifier/bin/python -m app.classifier_worker`
in another terminal. The production image only copies `app/`, not these scripts.

Measured 2026-09-22: CPU model load 7.40 s; three Thai synthetic pages took
8.16 / 7.93 / 8.03 s (three questions and three option orders per page).
Predicted topics were Algorithms on all three; roles were proof / exercise /
example. Document types varied (research / exercise / book), demonstrating why
review remains necessary. This is a post-OCR smoke test, not OCR validation or a
held-out classification accuracy benchmark. The earlier 31 ms CUDA benchmark
used a different, much smaller request and is not comparable.
