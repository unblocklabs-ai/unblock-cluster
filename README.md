# Data Graph

Local-first FastAPI data graph service for creating data graphs, ingesting JSON
rows, and viewing grouped records in the browser.

The local deployment uses SQLite for metadata and the filesystem for raw batches
and processed artifacts.

Ingested rows are processed with PaCMAP for 2D layout and HDBSCAN for cluster
labels after the debounce window. Text features use local TF-IDF by default, or
OpenAI embeddings when explicitly configured.

## Start

```sh
npm install
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm run build
cp .env.example .env
python3 - <<'PY'
from pathlib import Path
import secrets
path = Path(".env")
text = path.read_text()
text = text.replace("replace-with-a-random-token", secrets.token_hex(32))
path.write_text(text)
PY
npm run serve
```

The server binds to `127.0.0.1:8080` by default.

Runtime files are written to `local-data/` unless these environment variables are
set:

```sh
DATA_GRAPH_STORAGE=/path/to/private/storage
DATA_GRAPH_DB=/path/to/private/storage/data-graph.sqlite3
DATA_GRAPH_PUBLIC_ROOT=/path/to/dist
DATA_GRAPH_PUBLIC_BASE_URL=https://your-tunnel.example.com
DATA_GRAPH_HOST=127.0.0.1
DATA_GRAPH_PORT=8080
DATA_GRAPH_MAX_BODY_BYTES=8388608
DATA_GRAPH_PROCESS_DEBOUNCE_SECONDS=2.0
DATA_GRAPH_TEXT_FEATURE_METHOD=tfidf
OPENAI_API_KEY=
DATA_GRAPH_EMBEDDING_MODEL=text-embedding-3-small
DATA_GRAPH_EMBEDDING_DIMENSIONS=
DATA_GRAPH_EMBEDDING_BATCH_SIZE=64
DATA_GRAPH_EMBEDDING_TIMEOUT_SECONDS=30
```

The server loads `.env` from the project root automatically. Set
`DATA_GRAPH_ENV=/path/to/env-file` to use a different env file.

Set `DATA_GRAPH_PUBLIC_BASE_URL` to your Cloudflare Tunnel URL so API responses
and agent help endpoints return full public URLs.

## Text Feature Backends

Data Graph supports two text feature backends:

- `tfidf`: local scikit-learn TF-IDF, the default.
- `embedding`: OpenAI embeddings from the server-side `OPENAI_API_KEY`.

Server defaults come from `.env`, but each graph can override them under
`config.cluster`. Precedence is graph config, then environment default, then the
safe local default (`tfidf`). API keys must only live in `.env` or the process
environment; they are not accepted in graph config, help responses, status
responses, or artifacts.

Example embedding config:

```json
{
  "config": {
    "name": "Book Clusters",
    "dataSchema": {
      "bookName": "String",
      "genre": "String",
      "summary": "String",
      "rating": "Number"
    },
    "groupingFields": ["genre"],
    "titleField": "bookName",
    "detailField": "summary",
    "cluster": {
      "textFeatureMethod": "embedding",
      "embeddingProvider": "openai",
      "embeddingModel": "text-embedding-3-small",
      "embeddingDimensions": 512,
      "featureFields": ["bookName", "summary"],
      "numericFields": ["rating"],
      "minClusterSize": 3
    }
  }
}
```

When embedding mode is requested and `OPENAI_API_KEY` is missing or OpenAI
returns an error, processing fails explicitly and any existing latest artifact is
left in place. The graph status moves to `error` for asynchronous rebuilds.

Embeddings are cached in the private SQLite database by provider, model,
dimensions, and normalized text hash. Rebuilds reuse cached vectors for
unchanged text and only request missing vectors.

## Persistent Run

Use the service wrapper to run Data Graph in the background with a PID file and
logs:

```sh
./scripts/data_graph_service.sh start
./scripts/data_graph_service.sh status
./scripts/data_graph_service.sh logs
./scripts/data_graph_service.sh restart
./scripts/data_graph_service.sh stop
```

Runtime logs are written under `local-data/runtime/` by default. Set
`DATA_GRAPH_RUNTIME_DIR=/path/to/runtime` to override that location.

## Create A Data Graph

Agents can discover the API shape first:

```sh
curl -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN" \
  http://127.0.0.1:8080/api/help

curl -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN" \
  http://127.0.0.1:8080/api/status
```

```sh
export DATA_GRAPH_API_TOKEN="$(grep '^DATA_GRAPH_API_TOKEN=' .env | cut -d= -f2-)"

curl -X POST http://127.0.0.1:8080/api/data-graph \
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
  "dataGraphId": "dg_...",
  "viewUrl": "/clusters/dg_...",
  "ingestUrl": "/api/data-graph/dg_.../data"
}
```

## Ingest Data

Ingested rows are appended immediately as a raw batch. The latest view artifact is
rebuilt after a short debounce window, so multiple quick append requests are
processed together.

```sh
curl -X POST http://127.0.0.1:8080/api/data-graph/dg_REPLACE_ME/data \
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
curl -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN" \
  http://127.0.0.1:8080/api/data-graph/dg_REPLACE_ME/help

curl -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN" \
  http://127.0.0.1:8080/api/data-graph/dg_REPLACE_ME/status
```

To stress-test debounced parallel appends:

```sh
PARALLEL_REQUESTS=10 DEBOUNCE_WAIT_SECONDS=3 ./scripts/test_parallel_ingest.sh
```

## Clear Data

This keeps the data graph schema/configuration and removes all ingested rows.

```sh
curl -X DELETE http://127.0.0.1:8080/api/data-graph/dg_REPLACE_ME/data \
  -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN"
```

Open the cluster UI at:

```txt
http://127.0.0.1:8080/clusters/dg_REPLACE_ME?token=YOUR_TOKEN
```

The browser stores the token in `sessionStorage` and removes it from the visible
URL after the first load.

## Security Notes

- All `/api/*` endpoints require `Authorization: Bearer <token>`.
- The server binds to localhost by default. Put a reverse proxy with TLS in front
  of it before exposing it outside the Mac mini.
- Keep `local-data/` outside web roots and backed up.
- The server validates `groupingFields`, `titleField`, and `detailField` against
  `dataSchema` before creating or updating a data graph.
- Raw data and processed artifacts are never served by direct file path; the API
  reads artifacts by data graph ID.
