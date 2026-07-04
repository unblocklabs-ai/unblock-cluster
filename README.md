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

Open the `vizUrl` printed by `scripts/demo_seed.py`. Note that the seed script
deletes and recreates its data directory (`output/demo-data` by default) on
every run. The API binds to `127.0.0.1:8080` by default. Set `DATAGRAPH_PORT`
or `DATA_GRAPH_PORT` to override the port, and `DATAGRAPH_DATA_DIR` or
`DATA_GRAPH_DATA_DIR` to choose the SQLite data directory — see `.env.example`
for the complete configuration surface. Developed and tested
on Python 3.14.

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

Read-only mode:

```sh
DATAGRAPH_READ_ONLY=1 DATAGRAPH_DATA_DIR=data .venv/bin/python -m datagraph.main
```

Read-only mode is for sharing a finished graph without letting users mutate it.
GETs, static assets, and `POST /api/graphs/:gid/evidence` remain available; all
other `POST`, `PATCH`, `PUT`, and `DELETE` endpoints return `403`.

## Agent Contract

Agents upload already-normalized, redacted records, run the pipeline, and then
share the `vizUrl` or call evidence recipes. The backend does not extract from
source systems and does not redact sensitive data.

### Pre-Upload Filtering

Filter source exports before upload. Do not send records that are only
company-authored or outbound messages, including OOO replies and
autoresponders. Exclude transactional sends and automated campaign records such
as newsletters, tracking-pixel bodies, and URL-dominant bodies. Exclude
vendor/PR outreach, records with zero customer-authored messages (use inbound
message counts when the source provides them), and records with no meaningful
free text such as score-only NPS rows. Exclude device/platform relay
notifications such as printer confirmations, marketplace payout notices, and
chargeback status messages unless they include customer-authored substance.

After upload, leftover junk is visible through service-side signals: junk-like
topic labels, `coherent: false` labels, and records surfaced by
`GET /api/graphs/:gid/views/:vid/outliers`. Treat those as feedback for the
next extract, not as a reason to tune clustering around bad input.

### Iterating On Quality

The cheap quality loop is: upload, cluster, label, review topics/coherence/
outliers, tighten source filters, delete the graph or re-upload, and repeat.
Embeddings are content-addressed by rendered text, so reruns are cheap: only
changed texts are re-embedded; unchanged records reuse stored vectors.

For parameter tuning, view-scoped run POSTs accept `setDefault` (boolean,
default `true`) on `cluster`, `layout`, `label`, and `trends`. Use
`{"setDefault": false}` to run experiments that persist outputs without
repointing the view defaults. Promote a winner by rerunning it with
`{"setDefault": true}` (or omitting the key). Clustering reruns reuse the
existing embedding run, so promotion is cheap.

Cleanup is explicit. `DELETE /api/graphs/:gid/views/:vid` deletes a non-
`all_records` view and its view-scoped runs, leaving records and shared
embeddings intact. `DELETE /api/graphs/:gid/runs/:runId` deletes only terminal
runs that are not referenced by any view default; queued/running runs must be
cancelled or finish first. Use these deletes to remove failed experiments after
the tuning loop settles.

### Decomposing A Large Topic

If one topic swallows a large share of the graph after source filtering, use a
focus cluster run to drill into that topic instead of promoting a global tuning
run. Focus runs recluster only the selected topic's member records and are
inspection-only; they never become the view default because the artifact and
layout still cover the full view.

```sh
FOCUS_RUN_ID=$(
  curl -sS -X POST "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/views/$VIEW_ID/cluster" \
    -H 'Content-Type: application/json' \
    -d '{"focus":{"clusterId":12}}' | jq -r .id
)

curl -sS -X POST "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/views/$VIEW_ID/label" \
  -H 'Content-Type: application/json' \
  -d '{"clusterRunId":"'$FOCUS_RUN_ID'","setDefault":false}'

curl -sS "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/views/$VIEW_ID/topics?clusterRunId=$FOCUS_RUN_ID"
curl -sS "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/views/$VIEW_ID/topics/0/records?clusterRunId=$FOCUS_RUN_ID"
```

For global granularity experiments, `cluster.hdbscan.clusterSelectionMethod:
"leaf"` can split EOM's one-large-cluster signature, and
`cluster.hdbscan.clusterSelectionEpsilon` can merge nearby leaf clusters back
together. Run those with `setDefault:false` first, then promote only if the
whole view improves. UMAP's clustering guide suggests `cluster.space.minDist:
0.0` to pack points densely, but the real-embedding evaluation over-fragmented
feedback topics (`ARI 0.860` at `0.1` to `0.697` at `0.0`), so the default
stays `0.1`. Use `0.0` only as another per-run experiment with
`setDefault:false`.

Use facets to explain a large or surprising topic by record fields:

```sh
curl -sS "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/views/$VIEW_ID/topics?facetBy=metadata.groundTruthTopicId"

curl -sS -X POST "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/evidence" \
  -H 'Content-Type: application/json' \
  -d '{"viewId":"'$VIEW_ID'","recipe":"topic_evidence","topicId":12,"facetBy":"sourceType"}'
```

Allowed `facetBy` values are `sourceType`, `sourceName`, `product`, `sku`,
`sentiment`, `rating`, `tags`, and `metadata.<key>`. Null or absent values are
bucketed as `"(none)"`; high-cardinality facets return the top 20 values plus
`"(other)"`.

Minimum pipeline:

1. `POST /api/graphs` with `embedding.textFields`. Minimal payload:

   ```json
   {
     "name": "acme-supplements",
     "config": {
       "embedding": {"textFields": ["title", "customerText", "product", "tags"]}
     }
   }
   ```

   All other config keys have documented defaults and are filled into the
   response. It includes an auto-created `all_records` view — its id is the
   `:vid` used below.
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
`status` is `succeeded` (or `failed` with `errorText`) before the next step;
`progress` reports live counts. Run responses use camelCase keys:
`id`, `graphId`, `viewId`, `type`, `status`, `params`, `progress`,
`errorText`, `inputRefs`, `stats`, `createdAt`, `startedAt`, and
`completedAt`. `POST /api/graphs/:gid/runs/:runId/cancel`
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

Successful view-scoped runs promote themselves to the view's defaults unless
`setDefault` is explicitly `false`. A non-promoting run still persists its
memberships, labels, trend rows, or layout points; it simply leaves
`default*RunId` fields unchanged.

Inspection reads (all resolve the view's default runs, overridable by id):

- `GET /api/graphs/:gid/views/:vid/topics` — labels, size, source mix, trend
  snapshots, noise summary; add `?facetBy=sourceType` or another allowed facet
  for per-topic breakdowns.
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
  "warnings": [],
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

Artifact responses are served with gzip when the client advertises it and use
`Cache-Control: no-cache` plus a strong ETag. Repeating the same request with
`If-None-Match` returns `304` until the resolved run refs or view records
change. Response floats are rounded to 4 decimal places for transport only.

`warnings` is always present on artifact and topics responses. It is empty for
a healthy view. It names the label endpoint when the resolved cluster run has
no labels, and it calls out label/trend default mismatches when a view points
at a cluster run different from the one that produced the default labels or
trend snapshots.

## UI Modes

The frontend supports:

- Map mode: OpenLayers point map, cluster colors, muted noise, outlier outlines,
  and low-probability de-emphasis.
- Topic panel: topics sorted by size with labels, summaries, source mix,
  coherence flags, and spike badges.
- Topic selection: highlights/filter points and shows stored representatives.
- Time filter: client-side date range over record timestamps.
- List mode: searchable/filterable record table with topic and source filters,
  initially capped at 500 rows with explicit show-more pagination.
- Picker: when no `graphId`/`viewId` query params are present, the app lists
  available graph views.

Record list endpoints omit the bulky `normalized` object by default:
`GET /api/graphs/:gid/records?include=normalized` and
`GET /api/graphs/:gid/views/:vid/records?include=normalized` restore it.
Single-record reads still include `normalized`.

409 artifact errors are rendered as actionable messages instead of a blank map.

At 100k records, the expected frontend path is still a single artifact load:
gzip should keep the wire artifact under the 12 MB budget, the map should first
render within 15 seconds on a local machine, and list mode remains capped at 500
rows with explicit show-more pagination. Evidence latency measured about 0.9s
warm and about 2.2s truly cold at 100k, depending on OS page cache state. Use
the scale benchmark below to
measure the exact artifact/evidence/UI numbers on the target machine before a
large customer demo.

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
customer text leaves the machine at two points: embedding runs send every
record's rendered text, and label runs (optional — but whenever one is
triggered) send each topic's representative record text to `gpt-5.4-mini`.
Skipping labeling skips that second flow; nothing else transmits customer
text. Redact or drop sensitive values before upload. The demo, tests, mock
embeddings, evidence reads, artifact reads, and frontend build make no
network calls.

## Checks

```sh
.venv/bin/pytest
.venv/bin/ruff check .
npm run check
npm run build
```

Manual scale benchmark:

```sh
.venv/bin/python scripts/bench_scale.py \
  --size 100000 \
  --data-dir output/bench-scale \
  --json-out output/bench-scale/metrics.json
```

The benchmark is offline and deterministic by default. It seeds a fresh data
directory, uploads records through the API in 1000-record batches, runs mock
1536-dimensional embeddings, cluster, layout, scripted labels, and trends, then
reports stage timings, cluster/layout phase durations, artifact cold/warm
compose times, gzip wire bytes, evidence latency, peak RSS, and final DB size.
Pass `--no-seed` only when intentionally exploring non-reproducible variation.
