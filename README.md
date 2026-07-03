# Data Graph

Local-first FastAPI + Vite application for turning agent-prepared feedback
records into run-based topic maps. The backend stores normalized records,
embeddings, clusters, layouts, labels, trends, evidence calls, and composed UI
artifacts in SQLite. The frontend reads one artifact endpoint and renders a
map/list inspection UI.

## Quickstart

```sh
npm install
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm run build
.venv/bin/python scripts/demo_seed.py
DATAGRAPH_DATA_DIR=output/demo-data .venv/bin/python -m datagraph.main
```

Open the `vizUrl` printed by `scripts/demo_seed.py`. The API binds to
`127.0.0.1:8080` by default. Set `DATAGRAPH_PORT` or `DATA_GRAPH_PORT` to
override the port, and `DATAGRAPH_DATA_DIR` or `DATA_GRAPH_DATA_DIR` to choose
the SQLite data directory. This repository's current `.venv` runs Python 3.14.

The demo above is fully offline. Running the real pipeline (OpenAI embeddings
and topic labeling) requires `OPENAI_API_KEY` in the server's environment.

## Serve Modes

Production/local backend mode:

```sh
npm run build
DATAGRAPH_DATA_DIR=data .venv/bin/python -m datagraph.main
```

The backend serves `dist/index.html` at `/`, static assets at `/assets/*`, and
the API under `/api/*`. Phase 6 `vizUrl` values use:

```txt
http://127.0.0.1:{port}/?graphId={graphId}&viewId={viewId}
```

Development mode:

```sh
DATAGRAPH_DATA_DIR=data .venv/bin/python -m datagraph.main
npm run dev
```

Vite serves the frontend on `127.0.0.1:4173` and proxies `/api` to the backend.

## Agent Contract

Agents upload already-normalized, redacted records, run the pipeline, and then
share the `vizUrl` or call evidence recipes. The backend does not extract from
source systems and does not redact sensitive data.

Minimum pipeline:

1. `POST /api/graphs` with `embedding.textFields`. The response includes an
   auto-created `all_records` view — its id is the `:vid` used below.
2. `POST /api/graphs/:gid/records` in batches of at most 1000.
3. `POST /api/graphs/:gid/embeddings` (body may override any `embedding.*`
   config key, e.g. `requestsPerMinute` / `maxConcurrency` to stay inside the
   API key's rate limits without going serial).
4. `POST /api/graphs/:gid/views/:vid/cluster`.
5. `POST /api/graphs/:gid/views/:vid/layout`.
6. Optional: `POST /api/graphs/:gid/views/:vid/label`.
7. Optional: `POST /api/graphs/:gid/views/:vid/trends`.
8. Open `/?graphId=...&viewId=...` or fetch evidence.

Every POST above returns a run. Poll `GET /api/graphs/:gid/runs/:runId` until
`status` is `succeeded` (or `failed` with `error_text`) before the next step;
`progress` reports live counts. `POST /api/graphs/:gid/runs/:runId/cancel`
cancels queued runs always and embed/label runs between batches; a running
CPU job (cluster/layout) is not interruptible.

Missing prerequisite runs return actionable 409 messages naming the endpoint to
trigger.

## Views And Scopes

Graph creation makes an `all_records` view. Additional views are named,
persistent slices created with `POST /api/graphs/:gid/views`:

```json
{
  "name": "december_negative_reviews",
  "scope": {
    "sourceTypes": ["product_review"],
    "sentiments": ["negative"],
    "timeRange": {"start": "2025-12-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"}
  }
}
```

Scope keys: `sourceTypes`, `sourceNames`, `products`, `skus`, `sentiments`,
`ratings {min,max}`, `timeRange {start,end}`, `tagsAny`, and `metadataEquals`
(exact match on custom `metadata` keys). Empty scope means all records.
Embeddings are shared across views; each view runs its own cluster / layout /
label / trend runs on demand. Views are post-ingest slices, not a substitute
for pre-upload filtering.

Inspection reads (all resolve the view's default runs, overridable by id):

- `GET /api/graphs/:gid/views/:vid/topics` — labels, size, source mix, trend
  snapshots, noise summary.
- `GET /api/graphs/:gid/views/:vid/topics/:tid/records` — stored
  representatives with text.
- `GET /api/graphs/:gid/views/:vid/outliers` — highest outlier scores + noise.
- `GET /api/graphs/:gid/views/:vid/trends` — per-topic bucket series + window
  summary.

## Run Model

All expensive work is represented in the uniform `runs` table:
`queued`, `running`, `succeeded`, `failed`, or `cancelled`.

Run types:

- `embed`: async OpenAI or deterministic mock embeddings, with vector reuse.
- `cluster`: process-pool UMAP/HDBSCAN or no-reduction clustering.
- `layout`: process-pool UMAP 2D projection.
- `label`: async per-topic LLM labeling, with scripted providers in tests/demo.
- `trend`: synchronous temporal aggregation over persisted cluster membership.

The artifact and evidence endpoints are synchronous reads. They create no runs
and make no provider calls.

## Artifact Endpoint

The UI consumes one composed endpoint:

```sh
curl -sS "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/views/$VIEW_ID/artifact"
```

Shape:

```json
{
  "graphId": "grf_...",
  "viewId": "view_...",
  "config": {"embedding": {"model": "text-embedding-3-small", "dimensions": 1536}},
  "runRefs": {
    "embeddingRunId": "run_...",
    "clusterRunId": "run_...",
    "layoutRunId": "run_...",
    "labelRunId": "run_...",
    "trendRunId": "run_..."
  },
  "layout": {"method": "umap", "params": {}},
  "noise": {"noiseCount": 12, "noiseRatio": 0.02},
  "topics": [],
  "data": []
}
```

Topic entries include label, summary, coherence, size, probability, source mix,
representative record ids, and an optional trend snapshot. Record entries
include truncated `customerText`, source fields, timestamp, x/y coordinates,
cluster probability, outlier score, and noise status.

## UI Modes

The frontend supports:

- Map mode: OpenLayers point map, cluster colors, muted noise, outlier outlines,
  and low-probability de-emphasis.
- Topic panel: topics sorted by size with labels, summaries, source mix,
  coherence flags, and spike badges.
- Topic selection: highlights/filter points and shows stored representatives.
- Time filter: client-side date range over record timestamps.
- List mode: searchable/filterable record table with topic and source filters.
- Picker: when no `graphId`/`viewId` query params are present, the app lists
  available graph views.

409 artifact errors are rendered as actionable messages instead of a blank map.

## Evidence Recipes

Agents can answer common questions with one synchronous REST call:

```sh
curl -sS -X POST "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/evidence" \
  -H 'Content-Type: application/json' \
  -d '{
    "viewId": "'$VIEW_ID'",
    "recipe": "surprising_topics",
    "timeRange": {"start": "2025-12-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"},
    "topK": 10
  }'
```

Recipes:

- `surprising_topics`: topics ranked by max spike score in the window.
- `new_topics`: topics whose first nonzero bucket falls in the window.
- `vanishing_topics`: topics with zero window count after a healthy baseline.
- `rising_topics`: topics ranked by positive mean-share delta.

Baseline-dependent sections (`vanishing_topics`, `rising_topics`, and trend
summaries generally) compare the window against the 8 buckets before it, so
they are empty when the window starts at the beginning of the data's time
span — narrow the window to enable them.
- `topic_evidence`: one topic with label object, source mix, representatives,
  and persisted trend series when present.
- `compare_periods`: topics ranked by absolute share delta between two windows.

Every successful evidence response includes `runRefs`, `freshness`, and
`vizUrl`, and inserts one `analysis_events` audit row.

## Records And Validation

Required fields: `recordId`, `sourceType`, `sourceName`, `sourceRecordId`,
`customerText` (non-empty), `timestamp`. Optional: `title`, `recordUrl`,
`product`, `sku`, `rating`, `sentiment`, `tags`, and open `metadata` for
brand-specific context. `sourceType` is an open vocabulary. Explicit `null`
is treated as absent for every optional field; unknown top-level keys are
rejected (use `metadata` for custom fields).

Timestamps are ISO 8601 strings (offset-aware converted to UTC, naive and
date-only treated as UTC; epoch numbers rejected), stored as canonical UTC
plus epoch milliseconds.

Batch uploads are atomic by default: any invalid record rejects the whole
batch with per-record errors (`{"detail": {"rejected": [...]}}`). Pass
`"onInvalid": "skip"` to ingest the valid subset and get the rejects back.
Re-uploading a `recordId` upserts; duplicates within one batch resolve
last-write-wins. Unchanged text is never re-embedded.

## Privacy

Extraction, pre-filtering, aggregation, and redaction belong to agents before
upload — this service has no redaction pipeline. With `provider: "openai"`,
customer text is sent to OpenAI twice: rendered record text for embeddings,
and representative record text for topic labeling (`gpt-5.4-mini`). Redact or
drop sensitive values before upload. The demo, tests, mock embeddings,
evidence reads, artifact reads, and frontend build make no network calls.

## Checks

```sh
.venv/bin/pytest
.venv/bin/ruff check .
npm run check
npm run build
```
