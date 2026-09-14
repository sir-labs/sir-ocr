# Acceptance verification — 2026-09-14

Verified on the actual RTX 5070 host, with generated test PDFs only. Historical Week 1–8 documents were not imported.

| Check | Evidence |
| --- | --- |
| Automated API/queue/artifact tests | 20 passed, including concurrent deduplication, changed configuration, bad tokens, admission/rate limits, low-space simulation, chunked-size limit, page recovery, OOM retry, validation timeout and job-scoped native-download authorization |
| GPU container | Paddle GPU 3.2.1, CUDA runtime 12.9, RTX 5070; tensor computation returns 14.0 |
| Actual OOM | A test-only 32 GiB allocation raises `MemoryError`, correctly classified as `gpu_out_of_memory`; retry transitions are tested with fault injection |
| Existing model cache | Full 3-page pipeline succeeds; page times 3.30 / 2.31 / 2.29 seconds |
| No duplicate GPU work | Concurrent identical uploads receive separate tokens, share a byte-identical ZIP and record page attempts `[1,1,1]` |
| Crash recovery | Forced SIGKILL after page 1 of a 12-page PDF; resumes to 12/12, existing completion markers unchanged; attempts `[1,2,1,1,1,1,1,1,1,1,1,1]` |
| GPU admission | A test-only impossible free-memory threshold produces `waiting_gpu`, live heartbeat and no inference child; restoring 9216 MiB permits loading |
| Idle VRAM release | Child exits **300.46 seconds** after the final job; GPU memory becomes available again |
| Empty model cache | Starts with zero files, downloads 2,062,676,249 bytes, completes 3 pages; preparing-to-completed 93.351 seconds; both weight hashes match the existing cache |
| HTTPS pipeline | `https://ocr.sir-labs.com`: 3 pages, 3 valid image links, ordered Markdown and parseable JSON; upload/status/ZIP completes in **15.13 seconds** |
| Large HTTPS upload | **51,332,057 bytes** accepted (about 49 MiB), upload 14.21 seconds, OCR completes 1/1 page |
| Live negative inputs | Invalid PDF 400, encrypted PDF 400, 501 pages 400, 50 MiB + 1 byte 413, wrong token 404 |
| Proxy spoofing | Arbitrary `X-Forwarded-For` is not accepted as the client IP; a spoofed duplicate still reuses existing output |
| Repository/image contents | No user PDFs or model weights tracked; GPU image inspected for model-weight files and application PDFs: none |
| Browser upload/download | Chrome selects a fresh 2-page PDF through the real file chooser, uploads it, shows 2/2 completed, and emits the native ZIP download event after clicking the download link; copy-link also succeeds |

ZIP checks cover archive CRC, PDF SHA-256, page order, every referenced image, all per-page JSON files, matching output for duplicate subscriptions and rejection of cross-job tokens. Export version 2 trims recognized inline-math whitespace, preserves code/currency and raw OCR, and participates in the result key. OCR text itself can still contain recognition errors; this is functional acceptance, not an accuracy benchmark.

The service stores production data outside the Actions checkout. Container inspection confirmed writable mounts for the host data directory and existing PaddleX cache. Logs use structured status/error codes and omit tokens/PDF content. PDF validation now runs in bounded subprocesses; [PyMuPDF documents that multithreaded use is unsupported](https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html).

## Deployment evidence

- Gateway: [`sir-server` b10de81](https://github.com/sir-labs/sir-server/commit/b10de81), [successful Actions run 34805852096](https://github.com/sir-labs/sir-server/actions/runs/34805852096). Enables `proxy.max_body_size`; only OCR uses `51m`.
- Initial service: [successful Actions run 34806753028](https://github.com/sir-labs/sir-ocr/actions/runs/34806753028).
- Export version 2: [successful Actions run 34806972224](https://github.com/sir-labs/sir-ocr/actions/runs/34806972224).
- Native browser download: [successful Actions run 34807981411](https://github.com/sir-labs/sir-ocr/actions/runs/34807981411). Downloads stream directly using a job-path-scoped Secure/HttpOnly/SameSite cookie issued only after bearer authorization.
- Runner actually reports name `sir-labs`, labels `[self-hosted, sir-labs]`.
- Both API/worker healthchecks and public HTTPS health passed. Workflow only runs on `main` pushes or manual dispatch, never pull requests.

## Evidence and reproduction

Host-local acceptance evidence is retained under `/tmp/sir-ocr-*-evidence*` and `/tmp/sir-ocr-*.log`. Those paths are temporary and may disappear after reboot. Capability files are private and are deliberately excluded from Git. Synthetic originals and full OCR outputs remain in the separate test data directories; production test jobs follow normal administrator-controlled retention.

```sh
python -m scripts.make_fixture /tmp/acceptance.pdf
python -m scripts.smoke --url https://ocr.sir-labs.com --pdf /tmp/acceptance.pdf --output /tmp/ocr-evidence
```

Use `scripts/restart_check.py` only with explicitly named `sir-ocr-preflight-*` containers and separate test storage. Override the inherited `com.docker.compose.project` image label on test containers so production Compose does not discover them. Queue-full, low-disk and OOM retry transitions use controlled tests rather than filling production storage or deliberately crashing production OCR.

Browser file selection succeeded after the user enabled the Chrome extension's file-URL permission. Native download is confirmed by the browser download event; archive contents and integrity were independently verified through the HTTPS API. Download history is not used as evidence.

## Processing timeline update

The isolated browser fixture verified a running page, increasing elapsed clocks, persistent stage history, and terminal completion with a stopped clock and download link. This UI check used a fake OCR engine; earlier GPU inference evidence above remains separate. Tests cover event ordering, private token access, shared history for deduplicated uploads, restart events, the 200-event response limit and pre-existing databases.
