# AGENTS.md — sir-ocr

## Durable data belongs to sir-dataset

`/data` here is **scratch**. Results are keyed by PDF hash, shared between users, and pruned;
nothing in it is a user's to keep. What a user should keep is copied to the central store,
`sir-dataset` (`~/sir-dataset`) — see `app/dataset.py` and `save_to_dataset` in `app/api.py`,
which push the source PDF as an item plus `markdown`/`manifest` annotations and the result
ZIP (page images exist only inside it).

Do **not** add a new bind mount or volume for user data.

The push is triggered by the status poll, because that is the only moment both a finished
result and a signed-in user are known here — a result is shared, and the worker knows neither.
A user who closes the tab before the job finishes never triggers it; add a sweep over `jobs`
if that starts to matter.

## Reaching the central service

Over `sir-server_sir-net`, **by container name**, never through a public URL:

| What | Address |
|---|---|
| API | `sir-dataset-api-1:8000` |
| Postgres | `sir-dataset-db-1:5432` |
| Objects (MinIO) | `sir-dataset-minio-1:9000` |
| Events (RabbitMQ) | `sir-dataset-rabbitmq-1:5672` |

Physical storage for all of it lives under `~/.sir-labs` (`$SIR_LABS_DATA`) on the host.

`app/dataset.py` is stdlib-only on purpose: it runs in both the api and worker images, and a
new dependency means rebuilding both locks to make two HTTP calls.

## Identity

`X-Auth-User-Id` is set by nginx after sir-auth and must never be trusted from anywhere else.
This service's own capability tokens are unrelated to it and stay as they are. Pushing to
sir-dataset uses the service door: `Authorization: Bearer $DATASET_SERVICE_TOKEN` +
`X-Dataset-Service: sir-ocr` + `X-On-Behalf-Of: <the user id nginx gave us>`.

A failed push releases its claim in `dataset_pushes` and is retried on the next poll; it must
never fail the job.

sir-auth's own database is deliberately not part of the central store. Leave it alone.
