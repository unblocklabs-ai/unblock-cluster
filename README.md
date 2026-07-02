# Data Graph

Local-first FastAPI service for agents that need to turn structured records into
human-readable cluster visualizations. The service accepts JSON rows, validates
them against an explicit schema, builds a 2D cluster artifact, and serves a
browser UI for humans to inspect clusters, records, filters, and details.

The local deployment uses SQLite for metadata and the filesystem for raw batches
and processed artifacts. Raw batches stay private on disk; processed artifacts
are read through authenticated API routes.

## Agent Quick Path

Use this path when an agent is preparing a visualization for a human reviewer:

1. Inspect the source rows and decide which fields should be visible to humans.
2. Define `config.dataSchema` with exact field types: `String`, `Number`,
   `Boolean`, `Object`, or `Array`.
3. Choose `recordIdField` for stable shareable record URLs.
4. Choose `titleField` and `detailField` for readable record cards and hover
   previews.
5. Choose `groupingFields` for the human category shown around clusters.
6. Choose `cluster.featureFields` and `cluster.numericFields` so clustering uses
   semantic content, not noisy IDs, emails, timestamps, or huge SKU lists.
7. Create the graph, ingest rows, then poll status until the graph is `ready`.
8. Give the human the `viewUrl`, optionally with `?record=<id>` or
   `?cluster=<cluster-label>` for a focused starting point.

The viewer is built for large datasets: it caps the inline legend, exposes a
searchable cluster browser, supports map/list modes, groups detail fields in the
right drawer, and clamps long text/token fields behind `View more`.

## Start

```sh
npm install
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm run build
cp .env.example .env
./scripts/data_graph_token.py --print-token
npm run serve
```

The server binds to `127.0.0.1:8080` by default.

Server runtime variables:

```sh
DATA_GRAPH_API_TOKEN=replace-with-a-random-token
DATA_GRAPH_ENV=/path/to/env-file
DATA_GRAPH_STORAGE=/path/to/private/storage
DATA_GRAPH_DB=/path/to/private/storage/data-graph.sqlite3
DATA_GRAPH_PUBLIC_ROOT=/path/to/dist
DATA_GRAPH_PUBLIC_BASE_URL=https://your-tunnel.example.com
DATA_GRAPH_HOST=127.0.0.1
DATA_GRAPH_PORT=8080
DATA_GRAPH_MAX_BODY_BYTES=8388608
DATA_GRAPH_PROCESS_DEBOUNCE_SECONDS=2.0
DATA_GRAPH_RUNTIME_DIR=/path/to/runtime
```

Script/client variables:

```sh
DATA_GRAPH_BASE_URL=http://127.0.0.1:8080
DATA_GRAPH_API_TOKEN=replace-with-a-random-token
```

Text feature variables:

```sh
DATA_GRAPH_TEXT_FEATURE_METHOD=tfidf
DATA_GRAPH_EMBEDDING_PROVIDER=openai
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

## Designing A Useful Config

Minimum graph config:

```json
{
  "name": "Support Ticket Clusters",
  "description": "June support tickets clustered for customer operations review.",
  "source": "support_export_2026_06.jsonl",
  "dataSchema": {
    "ticketId": "String",
    "issueType": "String",
    "subject": "String",
    "summary": "String",
    "hasRefund": "Boolean",
    "hasCancellation": "Boolean",
    "messageCount": "Number",
    "customerTags": "Array"
  },
  "groupingFields": ["issueType"],
  "recordIdField": "ticketId",
  "titleField": "subject",
  "detailField": "summary",
  "cluster": {
    "featureFields": ["issueType", "subject", "summary"],
    "numericFields": ["messageCount"],
    "minClusterSize": 5,
    "labelStrategy": "labelField",
    "labelField": "issueType",
    "labelOverrides": {
      "-1": "Needs review"
    }
  }
}
```

Config design guidance:

- `dataSchema` should include fields humans need to inspect, plus any fields
  needed for clustering or filtering.
- `recordIdField` should be stable across exports, such as `ticketId`,
  `sourceTicketId`, `issueId`, or `key`.
- `titleField` should be short and recognizable. `detailField` should carry the
  human summary, transcript excerpt, or description.
- `groupingFields` should be low-cardinality fields that make sense as human
  categories, such as issue type, team, category, product, plan, or region.
- `cluster.featureFields` should favor semantic fields. Exclude raw IDs, emails,
  timestamps, URLs, and long unstructured SKU/tag dumps unless those values are
  genuinely meaningful for similarity.
- `cluster.numericFields` can add quantitative signals such as message count,
  spend, age, rating, or priority. Only `Number` schema fields are allowed.
- `cluster.labelStrategy` can be `groupingField`, `labelField`, or `clusterId`.
  Use `labelField` when a domain field makes better cluster names than the full
  grouping value.

For support-ticket datasets, the UI automatically recognizes these fields when
present:

- Issue type filter/coloring: `issueType`, `issuetype`, `issue_type`,
  `category`, or `type`.
- Refund filter/outcome coloring: `hasRefund`, `hasrefund`, or `refund`.
- Cancellation filter/outcome coloring: `hasCancellation`, `hascancellation`,
  `cancellation`, `cancelled`, or `canceled`.
- Message-count coloring: `messageCount`, `messagecount`,
  `inboundMessageCount`, or `inboundmessagecount`.

## Schema And Validation Rules

The API is intentionally strict so agents fail early instead of creating broken
human-facing views.

- `config.dataSchema` is required and must be non-empty.
- Allowed schema types are `String`, `Number`, `Boolean`, `Object`, and `Array`.
- Schema field names must be non-empty strings and cannot start with `__`.
- `groupingFields` is required, must be non-empty, and every grouping field must
  exist in `dataSchema`.
- Optional `titleField`, `detailField`, `imageField`, and `recordIdField` must
  exist in `dataSchema` when provided.
- Unknown top-level config keys, cluster config keys, and pipeline keys are
  rejected.
- Every transformed row must contain every schema field and each value must
  match its declared type.
- Config objects must not contain API keys, bearer tokens, or OpenAI keys. Put
  secrets only in `.env` or process environment variables.

Raw rows are accepted first, then optional pipeline transforms/filters run, then
the transformed rows are validated against the schema.

## Pipeline, Record IDs, And Labels

Rows can be normalized before validation and clustering with `config.pipeline`.
Raw batches stay unchanged on disk, so schema or pipeline updates can rebuild
from the original input.

Supported transform types:

- `copyField`: copy `from` to schema field `to`.
- `renameField`: move `from` to schema field `to`.
- `setField`: set a schema `field` to a constant `value`.
- `trim`, `lowercase`, `uppercase`: normalize a schema string field.

Supported filter ops:

- `equals`, `notEquals`
- `contains`, `notContains`
- `exists`, `notExists`

Example:

```json
{
  "config": {
    "name": "Support Tickets",
    "dataSchema": {
      "ticketId": "String",
      "title": "String",
      "team": "String",
      "summary": "String",
      "archived": "Boolean"
    },
    "groupingFields": ["team"],
    "recordIdField": "ticketId",
    "titleField": "title",
    "detailField": "summary",
    "pipeline": {
      "transforms": [
        { "type": "copyField", "from": "source_ticket_id", "to": "ticketId" },
        { "type": "trim", "field": "title" }
      ],
      "filters": [
        { "field": "archived", "op": "notEquals", "value": true }
      ]
    },
    "cluster": {
      "labelStrategy": "labelField",
      "labelField": "team",
      "labelOverrides": {
        "-1": "Needs review"
      }
    }
  }
}
```

`recordIdField` controls shareable record URLs and API search identity. The
viewer and API also fall back to common fields such as `id`, `ticketId`,
`sourceTicketId`, `sourceId`, `issueId`, and `key`.

## Processing Lifecycle

Ingested rows are processed with PaCMAP for 2D layout and HDBSCAN for cluster
labels after the debounce window. Text features use local TF-IDF by default, or
OpenAI embeddings when explicitly configured.

The normal artifact rebuild is asynchronous:

1. Rows are appended as a raw batch.
2. Graph status moves to `processing`.
3. Processing is scheduled after `DATA_GRAPH_PROCESS_DEBOUNCE_SECONDS`.
4. When processing succeeds, status moves to `ready`.
5. When async processing fails, status moves to `error` and any previous latest
   artifact is left in place.

The processor uses deterministic fallback layouts when there are fewer than
three rows, when no feature values are available, or when local PaCMAP/HDBSCAN
processing fails. Embedding-mode failures fail explicitly instead of falling
back silently.

Use the graph status endpoint before sending humans to the visualization:

```sh
curl -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN" \
  http://127.0.0.1:8080/api/data-graph/dg_REPLACE_ME/status
```

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

## Create A Data Graph

Agents can discover the API shape first:

```sh
curl -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN" \
  http://127.0.0.1:8080/api/help

curl -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN" \
  http://127.0.0.1:8080/api/status
```

Create an empty graph:

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
  "ingestUrl": "/api/data-graph/dg_.../data",
  "latestArtifactUrl": "/api/data-graph/dg_.../artifact/latest"
}
```

You can also include initial rows in the create payload with `"data": [...]`.
Initial rows are persisted as a raw batch and processed after the debounce
window.

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
that window, then poll status until `ready`, refresh the cluster UI, or fetch the
latest artifact.

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

## Update Schema Or Pipeline

Use `PATCH /api/data-graph/:id/schema` to refine the config after seeing a weak
cluster result. The server rebuilds from the stored raw batches, so agents do not
need to re-upload the original rows.

```sh
curl -X PATCH http://127.0.0.1:8080/api/data-graph/dg_REPLACE_ME/schema \
  -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"config": { "...": "full replacement config" }}'
```

The payload must contain a full replacement `config`, not a partial patch.

## Latest Artifact Contract

Fetch the latest artifact with:

```sh
curl -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN" \
  http://127.0.0.1:8080/api/data-graph/dg_REPLACE_ME/artifact/latest
```

The artifact shape is:

```json
{
  "config": {},
  "layout": {
    "method": "PaCMAP",
    "clusterMethod": "HDBSCAN",
    "recordCount": 3209,
    "fallbackUsed": false,
    "featureFields": ["issueType", "subject", "summary"],
    "numericFields": ["messageCount"],
    "textFeatureMethod": "tfidf"
  },
  "data": []
}
```

Processed records keep the original schema fields and add layout fields used by
the viewer:

- `x`, `y`: 2D point coordinates.
- `clusterId`: numeric cluster identifier.
- `clusterLabel`: human-readable cluster label.
- `groupValue`: display grouping value derived from `groupingFields`.

Do not define source schema fields beginning with `__`; those names are reserved
for viewer/runtime metadata.

## Search And Share URLs

The browser search box scans all visible non-system record fields, including
nested object and array values. Press Enter to focus the first matching record.

The API search endpoint is narrower: it searches record identity fields,
`titleField`, and `detailField`, and ranks exact identity matches first.

```sh
curl -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN" \
  "http://127.0.0.1:8080/api/data-graph/dg_REPLACE_ME/records/search?q=TICKET-123"
```

Shareable record URLs use `recordIdField` when configured:

```txt
http://127.0.0.1:8080/clusters/dg_REPLACE_ME?record=TICKET-123
```

Shareable cluster URLs use the selected cluster label:

```txt
http://127.0.0.1:8080/clusters/dg_REPLACE_ME?cluster=Delivery%20%26%20Shipping
```

Open the cluster UI with an API token:

```txt
http://127.0.0.1:8080/clusters/dg_REPLACE_ME?token=YOUR_TOKEN
```

The browser stores the token in `sessionStorage` and removes it from the visible
URL after the first load.

## Viewer Capabilities For Large Datasets

The browser UI is optimized for humans reviewing thousands of records:

- Map mode shows clustered points and labels.
- List mode provides a scannable grouped record list.
- The inline legend shows the first visible clusters and exposes a `+N more`
  control when there are many clusters.
- The cluster browser lets humans search and select among many clusters.
- Search filters records and clusters in place.
- Filters appear automatically for recognized issue, refund, cancellation, and
  message-count fields.
- The right drawer groups detail fields into Customer, Ticket, Order, Details,
  and System sections based on field names.
- Long text fields are clamped with `View more`.
- Array fields and comma-separated values render as token chips with overflow.
- Record IDs and common identifiers can be copied from the drawer.

Agents should prefer concise field values and explicit summaries. Very long raw
transcripts, huge SKU arrays, and dense metadata are still available in the
drawer, but they make human review slower unless summarized or moved behind a
compact field.

## Import And Export

Create a graph from a config file and JSON/JSONL/CSV data:

```sh
./scripts/data_graph_import.py \
  --base-url http://127.0.0.1:8080 \
  --env .env \
  --config config.json \
  --data tickets.jsonl \
  --format jsonl
```

Append to an existing graph:

```sh
./scripts/data_graph_import.py \
  --graph-id dg_REPLACE_ME \
  --data more-tickets.csv \
  --format csv
```

Import flags:

- `--base-url`: API origin. Must be `http://` or `https://`. Defaults to
  `DATA_GRAPH_BASE_URL` or `http://127.0.0.1:8080`.
- `--env`: env file to load before reading `DATA_GRAPH_API_TOKEN`. Defaults to
  `.env`.
- `--token`: bearer token override.
- `--graph-id`: append to an existing graph. Omit to create a new graph.
- `--config`: config JSON file. Required when creating a new graph.
- `--data`: input data file.
- `--format`: `auto`, `json`, `jsonl`, or `csv`.

CSV import coerces values against the graph schema. `Number` fields must be
numeric, `Boolean` fields accept true/false/1/0/yes/no, and `Object`/`Array`
fields must contain JSON strings. Missing required schema fields are not
fabricated; the server still rejects rows that do not satisfy `dataSchema`.

Export config, latest artifact, or both:

```sh
./scripts/data_graph_export.py dg_REPLACE_ME --kind config --output config.json
./scripts/data_graph_export.py dg_REPLACE_ME --kind artifact --output artifact.json
./scripts/data_graph_export.py dg_REPLACE_ME --kind bundle --output bundle.json
```

Export supports the same `--base-url`, `--env`, and `--token` options as import.

## Clear Data

This keeps the data graph schema/configuration and removes all ingested rows.

```sh
curl -X DELETE http://127.0.0.1:8080/api/data-graph/dg_REPLACE_ME/data \
  -H "Authorization: Bearer $DATA_GRAPH_API_TOKEN"
```

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

## Security Notes

- All `/api/*` endpoints require `Authorization: Bearer <token>`.
- The browser view can receive `?token=YOUR_TOKEN`; the token is stored in
  `sessionStorage` and removed from the visible URL.
- The server binds to localhost by default. Put a reverse proxy with TLS in
  front of it before exposing it outside the local machine.
- Keep `local-data/` outside web roots and backed up.
- Raw data and processed artifacts are never served by direct file path; the API
  reads artifacts by data graph ID.
- Image rendering is allowlisted. Same-origin images must live under `/assets/`
  or `/sample-data/` with a supported image extension. External images must be
  HTTPS and from an allowed host.
- Keep API keys and tokens out of graph config, raw rows, logs, and exported
  bundles whenever possible.

## Troubleshooting Poor Clusters

If the visualization is not useful for a human reviewer:

- Check `GET /api/data-graph/:id/status` for `status`, row count, artifact count,
  and processor settings.
- Fetch `artifact/latest` and inspect `layout.fallbackUsed`,
  `layout.fallbackReason`, `layout.featureFields`, and `layout.numericFields`.
- Remove noisy fields from `cluster.featureFields`.
- Add a concise human summary field and include it in `cluster.featureFields`.
- Raise or lower `cluster.minClusterSize` for datasets with too many tiny
  clusters or too many outliers.
- Use `cluster.labelField` or `labelOverrides` when cluster names are not clear.
- Use `PATCH /api/data-graph/:id/schema` to rebuild from stored raw rows after
  config changes.
