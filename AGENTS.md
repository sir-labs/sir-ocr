# AGENTS.md — sir-ocr

## Durable data belongs to sir-data

`/data` here is **scratch**. Results are keyed by PDF hash, shared between users, and pruned;
nothing in it is a user's to keep. What a user should keep is copied to the central store,
`sir-data` (`~/sir-data`) — see `app/dataset.py` and `save_to_dataset` in `app/api.py`,
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
| API | `sir-data-api-1:8000` |
| Postgres | `sir-data-db-1:5432` |
| Event log (Kafka) | `sir-data-kafka-1:9092` — read `dataset.events` to replay |
| MinIO, RabbitMQ, Redis | **not on sir-net**: internal to sir-data |

Physical storage for all of it lives under `~/.sir-labs` (`$SIR_LABS_DATA`) on the host.

Only three of them are on `sir-server_sir-net` at all, and that is deliberate: the API, the
Postgres that sir-mcp keeps its own database in, and the Kafka log meant to be replayed.
MinIO, RabbitMQ and Redis stay inside sir-data's own network, so writing an object or an
event directly is not merely discouraged — it is unreachable. Push through the API; skipping
it would skip the ownership row that makes an object findable.

`app/dataset.py` is stdlib-only on purpose: it runs in both the api and worker images, and a
new dependency means rebuilding both locks to make two HTTP calls.

## Identity

`X-Auth-User-Id` is set by nginx after sir-auth and must never be trusted from anywhere else.
This service's own capability tokens are unrelated to it and stay as they are. Pushing to
sir-data uses the service door: `Authorization: Bearer $DATA_SERVICE_TOKEN` +
`X-Data-Service: sir-ocr` + `X-On-Behalf-Of: <the user id nginx gave us>`.

A failed push releases its claim in `dataset_pushes` and is retried on the next poll; it must
never fail the job.

sir-auth's own database is deliberately not part of the central store. Leave it alone.
