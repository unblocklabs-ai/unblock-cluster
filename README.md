# Data Graph

Local-first data graph service for creating data sinks, ingesting JSON rows, and
viewing grouped records in the browser.

The local deployment uses SQLite for metadata and the filesystem for raw batches
and processed artifacts.

## Start

```sh
npm install
npm run build
printf "DATA_GRAPH_API_TOKEN=%s\n" "$(openssl rand -hex 32)" > .env
npm run serve
```

The server binds to `127.0.0.1:8080` by default.

Runtime files are written to `local-data/` unless these environment variables are
set:

```sh
DATA_GRAPH_STORAGE=/path/to/private/storage
DATA_GRAPH_DB=/path/to/private/storage/data-graph.sqlite3
DATA_GRAPH_PUBLIC_ROOT=/path/to/dist
DATA_GRAPH_HOST=127.0.0.1
DATA_GRAPH_PORT=8080
DATA_GRAPH_MAX_BODY_BYTES=8388608
DATA_GRAPH_PROCESS_DEBOUNCE_SECONDS=2.0
```

The server loads `.env` from the project root automatically. Set
`DATA_GRAPH_ENV=/path/to/env-file` to use a different env file.

## Create A Data Sink

Agents can discover the API shape first:

```sh
curl http://127.0.0.1:8080/api/help
curl http://127.0.0.1:8080/api/status
```

```sh
export DATA_GRAPH_API_TOKEN="$(grep '^DATA_GRAPH_API_TOKEN=' .env | cut -d= -f2-)"

curl -X POST http://127.0.0.1:8080/api/data-sink \
  -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "config": {
      "name": "Book Clusters",
      "dataSchema": {
        "bookName": "String",
        "genre": "String",
        "summary": "String"
      },
      "groupingFields": ["genre"],
      "titleField": "bookName",
      "detailField": "summary"
    }
  }'
```

The response includes:

```json
{
  "dataSinkId": "ds_...",
  "viewUrl": "/clusters/ds_...",
  "ingestUrl": "/api/data-sink/ds_.../data"
}
```

## Ingest Data

Ingested rows are appended immediately as a raw batch. The latest view artifact is
rebuilt after a short debounce window, so multiple quick append requests are
processed together.

```sh
curl -X POST http://127.0.0.1:8080/api/data-sink/ds_REPLACE_ME/data \
  -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "data": [
      {
        "bookName": "Dune",
        "genre": "Science Fiction",
        "summary": "A desert planet power struggle."
      }
    ]
  }'
```

The response includes `status: "processing"` and `processAfterSeconds`. Wait for
that window, then refresh the cluster UI or fetch the latest artifact.

Agents can inspect the exact expected schema and current processing state:

```sh
curl http://127.0.0.1:8080/api/data-sink/ds_REPLACE_ME/help
curl http://127.0.0.1:8080/api/data-sink/ds_REPLACE_ME/status
```

To stress-test debounced parallel appends:

```sh
PARALLEL_REQUESTS=10 DEBOUNCE_WAIT_SECONDS=3 ./scripts/test_parallel_ingest.sh
```

## Clear Data

This keeps the sink schema/configuration and removes all ingested rows.

```sh
curl -X DELETE http://127.0.0.1:8080/api/data-sink/ds_REPLACE_ME/data \
  -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN"
```

Open the cluster UI at:

```txt
http://127.0.0.1:8080/clusters/ds_REPLACE_ME
```

## Security Notes

- Write endpoints require `Authorization: Bearer <token>`.
- The server binds to localhost by default. Put a reverse proxy with TLS in front
  of it before exposing it outside the Mac mini.
- Keep `local-data/` outside web roots and backed up.
- The server validates `groupingFields`, `titleField`, and `detailField` against
  `dataSchema` before creating or updating a sink.
- Raw data and processed artifacts are never served by direct file path; the API
  reads artifacts by sink ID.
