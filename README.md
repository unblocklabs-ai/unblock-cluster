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

Tunnel and uptime checks can use `HEAD` on read endpoints without downloading
the response body:

```sh
curl -I "http://127.0.0.1:8080/api/health"
curl -I "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/views/$VIEW_ID/artifact"
```

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
free text such as score-only NPS rows. Zero-agent-reply threads are often junk
but can include unanswered complaints; never drop on that signal alone. Combine
it with inbound count and the semantic junk gate. Exclude device/platform relay
notifications such as printer confirmations, marketplace payout notices, and
chargeback status messages unless they include customer-authored substance.

After upload, leftover junk is visible through service-side signals: junk-like
topic labels, `coherent: false` labels, and records surfaced by
`GET /api/graphs/:gid/views/:vid/outliers`. Treat those as feedback for the
next extract, not as a reason to tune clustering around bad input.

### Extraction Quality: What The Input Does To The Output

Representation dominates topic quality. In the pilot, first-message/preview
text produced a generic mega-topic: 1,145 records, 45% of the graph, labeled
"Order status and support requests". Re-extracting the same conversations as
one record per conversation, with `customerText` set to the chronological
concatenation of customer-authored messages and agent replies excluded,
dissolved that blob into concrete operational themes such as 826 warm or
spoiled deliveries and 526 delivery/address-change records. The real threads
were still comfortably below the embedding cap: max about 2.6k tokens, p95
about 1k, and zero at the 8k limit.

Filter at message level before concatenation. Junk rules are representation-
dependent: single-message patterns over-fire on concatenated threads. Quoted
footers, tracking links inside an otherwise real thread, and a customer
mentioning travel are not OOO records. Redact URLs, emails, and phone numbers
inside kept messages instead of dropping the whole record.

Changing representation changes every rendered embedding text, so it requires
a full re-embed. Content-addressed vector reuse cannot help when every text is
new. The cost is cheap in absolute terms for local pilot sizes, but plan for
the rerun.

Make metadata facet-worthy at extraction time. Facet usefulness is gated by
population: the pilot's channel facets were excellent, while product was 100%
`"(none)"` and order SKUs were unused. Map SKUs to product families, and
surface issue-taxonomy fields plus boolean flags such as `hasRefund` in
`metadata` so `facetBy=metadata.<key>` answers operator questions.

Backfill enough history during onboarding. Trend baselines need runway; with
only about one month of data, new/vanishing topic evidence is structurally
empty. Prefer 6-12 months when the source system can provide it.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Largest topic is more than about 30% of the graph with a generic label | Representation is too thin, or the topic needs a drilldown | Re-extract richer `customerText`, or run focus reclustering on that topic |
| Coherent junk topics | Filtering gap | Mine representatives for the next source-filter rule |
| `coherent:false` topics | No-signal records such as score-only rows | Drop or enrich those records before upload |
| Many near-duplicate labels | Over-split clustering | Raise `minClusterSize` and trial with `setDefault:false` |
| Higher noise after enriching text | Expected and honest separation, not a defect; pilot noise rose 0.6% to 6.4% | Inspect outliers, but do not tune away truthful no-fit records |

#### Reading Your Noise

Before declaring a high-noise graph to be "domain long-tail", read a random
sample of noise records. Classify each sampled record as junk that should have
been filtered, a missed theme that should probably be clustered, or a
genuinely individual one-off. Then run one smaller-`minClusterSize` cluster
tuning pass with `setDefault:false` and compare whether coherent small topics
appear without damaging the rest of the graph.

Perelel's second-brand portability run measured 25.8% noise at 10k records.
The 25-record noise read found 0 junk, 17 missed operational long-tail themes,
and 7 individual health questions; lowering `minClusterSize` recaptured only
117 records while doubling topic count, so it was correctly not promoted. At
10k+ records, long-tail noise can be normal. The drill is to read the noise,
use focus reclustering for local drilldowns, and avoid global tuning unless
the sample shows real junk or broad missed themes.

Use representation A/B tests when quality is uncertain: create two graphs from
the same conversations and compare outputs. Embeddings are the only meaningful
cost. Keep a per-brand list of canonical questions and rerun it after
extraction changes. Use focus reclustering inside large topics as both a
drilldown tool and a junk detector.

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
`sentiment`, `rating`, `tags`, `metadata.<key>`, and, for summary-backed
embedding runs, `summary.issue`, `summary.product`,
`summary.desiredResolution`, `summary.sentiment`, or `summary.junkType`. Null
or absent values are bucketed as `"(none)"`; high-cardinality facets return the
top 20 values plus `"(other)"`.

### Summarize-Then-Embed (Optional)

Raw embedding remains the default. Use summarize-then-embed when regex junk
filtering is turning into whack-a-mole, source text length varies wildly, or
the source lacks useful facets such as product family, issue taxonomy, desired
resolution, or semantic junk type. The summarize run makes one structured
`gpt-5.4-nano` extraction call per record against the service-owned schema,
caches results by rendered raw text plus prompt hash, and stores a stable
labeled-line summary representation for optional embedding.

```sh
SUMMARY_RUN_ID=$(
  curl -sS -X POST "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/summarize" \
    -H 'Content-Type: application/json' \
    -d '{"summarization":{"context":"Acme sells supplements and meal delivery. Real support traffic is about orders, spoiled deliveries, subscriptions, refunds, product guidance, and shipping. Choose product from exactly: [Metabolism Super Powder, Detox Water Drops, Daily Fiber, Prenatal Complete, Unknown]."}}' \
    | jq -r .id
)

SUMMARY_EMBED_RUN_ID=$(
  curl -sS -X POST "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/embeddings" \
    -H 'Content-Type: application/json' \
    -d '{"representation":"summary"}' | jq -r .id
)

curl -sS -X POST "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/views/$VIEW_ID/cluster" \
  -H 'Content-Type: application/json' \
  -d '{"embeddingRunId":"'$SUMMARY_EMBED_RUN_ID'","setDefault":false}'
```

`summarization.context` is optional static brand/service background, capped at
4,000 characters. It is appended to the built-in prompt and included in
`promptHash`, so changing context or `summarization.prompt` correctly
invalidates cached summaries. Use it to teach the semantic junk gate what
counts as real support traffic for this business. Also enumerate the brand's
canonical product families here, so `summary.product` maps extracted mentions
to a closed set instead of creating high-cardinality one-off product strings.
Prefer directive wording such as `Choose product from exactly: [A, B, C,
Unknown]`; prose enumeration still produced variants such as combined
`"Product A / Product B"` values in the Perelel run.

The A/B workflow is one graph, two embedding runs, and two cluster runs: keep a
raw embedding run as the control, create a summary embedding run, cluster each
with explicit `embeddingRunId`, then compare the same canonical questions
against `?clusterRunId=` overrides. Watch for suspiciously merged topics; that
is a homogenization smell and means the summary prompt is erasing customer
vocabulary. The built-in prompt explicitly asks for verbatim customer phrases
to preserve that signal.

Summary representation excludes records whose `junkType` is not `"none"` by
default at the embedding boundary. Use `{"representation":"summary",
"includeJunk":true}` only when intentionally inspecting the semantic junk
bucket. Summary facets resolve through the run lineage, so
`facetBy=summary.product` works only for clusters produced from a summary
embedding run; raw clusters return a 422 with the summarize/embed path to run.
Use `summary.product`, `summary.desiredResolution`, `summary.sentiment`, and
`summary.junkType` as aggregate facets. `summary.issue` is intentionally
per-record; use it for spot reads and reports, not as a topic-level aggregate
signal.

Summarization cost and latency are per-record LLM calls. Content-addressing
amortizes reruns: unchanged records with the same effective prompt reuse
stored summaries with zero provider calls, while changed records or prompt/
context changes summarize again. Reuse is per text, not per run: records that
failed in one summarize run are retried by the next run and can self-heal. In
the Sakara pilot, the second run reused 2,443 summaries and made 3 provider
calls to heal first-run misses; that is expected failure isolation, not drift.
Inspect the derived artifacts through
`GET /api/graphs/:gid/summarize-runs/:runId/report`, which reports junk counts
by type, token usage, and per-record summary fields for agent drop/keep
decisions.

For cost planning, the Sakara pilot summarized 2,446 records in about 14
minutes with about 2,500 provider requests. Run stats include
`tokenUsage: {promptTokens, completionTokens, totalTokens}` for summarize and
label runs, and prompt tokens for embedding runs, so actual spend is auditable
after the run completes. Dollar cost is:
`sum(promptTokens / 1_000_000 * input_rate + completionTokens / 1_000_000 *
output_rate)` per run, using the provider's current rate card for that model,
then summed across the runs in your workflow. This repo intentionally does not
maintain a rate card; use `tokenUsage` in run stats and the summarize-run
report, then apply the current provider prices outside the repo.

Receipts stay raw. Topic representatives, topic-record reads, evidence
payloads, and the frontend artifact continue to show raw `customerText`, never
the summary representation. Summaries are derived artifacts for embedding,
faceting, labeling when selected, and the summarize-run report.

### Labeling Configuration And Reports

Label runs default to `labeling.topK: 12` stored representatives per topic.
`labeling.exampleTextLimit` defaults to `700` characters per representative
block and controls the text the label provider sees; stored records are not
truncated. `labeling.promptAppend` accepts up to 2,000 characters and is
appended after either the built-in prompt or a full `labeling.prompt` override.
Both the append text and the example limit are echoed in run params/stats, and
the effective prompt hash changes when the append text changes.

Terse-ticket brands often do better with fewer and shorter examples because
each representative already carries a complete support ask. Heterogeneous
clusters usually need more examples, not longer ones: raise `labeling.topK`
first so the model sees breadth, then adjust `exampleTextLimit` only when
important context is being truncated.

`labeling.textSource` controls which representative text is sent to the label
provider:

- `"auto"` (default): use summary `rendered_text` when the cluster run came
  from a summary-backed embedding run; otherwise use raw `customerText`.
- `"raw"`: always use raw `customerText`; this preserves pre-summary behavior.
- `"summary"`: require summary-backed cluster lineage. Without it, the label
  POST returns 422 naming the summarize endpoint to run first.

When summary text is selected, any representative missing a summary falls back
to raw `customerText`; run stats report `textSource: "summary_rendered_text"`
and `fallbackRawCount`. Raw runs report `textSource: "raw_customer_text"`.

Inspect exactly what a label run would send with:

```sh
curl -sS "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/label-runs/$LABEL_RUN_ID/report"
```

The report recomputes representative blocks from the run's recorded params and
stored representatives. It includes record ids, title presence, per-block text
source, truncation flags, prompt hash, model, duplicate-label groups, near
duplicates, and very-short/generic label flags. Near duplicates are detected
deterministically by lowercasing labels, tokenizing on alphanumerics, and
flagging non-identical label pairs whose shared-token count covers at least
80% of the smaller token set. Prompt inputs are intentionally not persisted.

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
3. Optional: `POST /api/graphs/:gid/summarize`, then embed with
   `{"representation":"summary"}`.
4. `POST /api/graphs/:gid/embeddings` (body may override any `embedding.*`
   config key, e.g. `requestsPerMinute` / `maxConcurrency` to stay inside the
   API key's rate limits without going serial).
5. `POST /api/graphs/:gid/views/:vid/cluster`.
6. `POST /api/graphs/:gid/views/:vid/layout`.
7. Optional: `POST /api/graphs/:gid/views/:vid/label`.
8. Optional: `POST /api/graphs/:gid/views/:vid/trends`.
9. Open `/?graphId=...&viewId=...` or fetch evidence.

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

To rerun the full API pipeline for an existing graph/view:

```sh
.venv/bin/python scripts/rerun_pipeline.py \
  --graph "$GRAPH_ID" \
  --view "$VIEW_ID" \
  --representation raw \
  --config-json '{"cluster":{"hdbscan":{"minClusterSize":20}}}'
```

Add `--summarize --representation summary` to refresh summaries and build a
summary-backed embedding run. Add `--base-url http://127.0.0.1:8080` to drive
an already-running server; without `--base-url`, the script opens an
in-process app against `--data-dir` and still uses only API endpoints. The
script promotes each successful cluster/layout/label/trend run with
`setDefault:true`, prints run ids, stats, token usage, and the final `vizUrl`.

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
- `summarize`: async per-record structured extraction, with summary reuse.
- `cluster`: process-pool UMAP/HDBSCAN or no-reduction clustering.
- `layout`: process-pool UMAP 2D projection.
- `label`: async per-topic LLM labeling, with scripted providers in tests/demo.
- `trend`: synchronous temporal aggregation over persisted cluster membership.

The artifact endpoint and most evidence recipes are synchronous reads. They
create no runs. The one documented evidence exception is `topic_search` /
`question_evidence`, which makes exactly one question-embedding provider call
using the same embedding provider/model as the resolved embedding run, then
ranks topics in memory without persisting the question vector.

Summarize-run reports are synchronous reads:
`GET /api/graphs/:gid/summarize-runs/:runId/report`.

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

Spike scores are gated for integrity: the first 3 buckets of the overall
zero-filled series always carry `spikeScore: 0`, because they do not have enough
history. Late-emerging topics still score their first burst after that point
against the prior zero-filled baseline.

Persisted trend runs are snapshots of the trend math at run time. After
upgrading the service, re-run trends to recompute persisted scores; the UI
warns when a default trend run predates the current math, and
`scripts/rerun_pipeline.py` performs the rerun in one command.
- `topic_evidence`: one topic with label object, source mix, representatives,
  and persisted trend series when present.
- `topic_search`: embed a natural-language `question` once and rank topics by
  cosine similarity to topic centroids in the resolved embedding space. Default
  `topK` is 5, max is 20. If the embedding run used
  `representation: "summary"`, the question is still embedded as-is with the
  same model; it is matched against whatever representation built that space.
- `question_evidence`: run `topic_search`, take the top match above the
  exposed similarity floor, and return full `topic_evidence` plus runner-up
  topics. If nothing clears the floor, the endpoint returns 422 with the
  closest candidates so the agent can decide how to rephrase.
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
customer text can leave the machine through three provider flows: embedding
runs send every record's rendered text, summarize runs (optional) send every
record's rendered raw text to `gpt-5.4-nano`, and label runs (optional — but
whenever one is triggered) send each topic's representative text to
`gpt-5.4-mini`. Label representatives are raw `customerText` by default for
raw clusters, and summary `rendered_text` by default for summary-backed
clusters unless `labeling.textSource` is set explicitly.
Skipping summarization or labeling skips those optional flows; nothing else
transmits customer text. Redact or drop sensitive values before upload. The
demo, default tests, mock embeddings, scripted summarization, artifact reads,
and frontend build make no network calls. Evidence reads make no network calls
except `topic_search` / `question_evidence`, which embed the supplied question
once with the resolved embedding provider.

Support-system PII often hides in structured fields and quoted text, not just
the main message body. Check for requester names in ticket titles, signature
and greeting names, quoted email display names such as `"Jane Doe" <jane@...>`,
phone and address blocks, order/contact forms pasted into replies, and
platform usernames or handles. Redact those patterns in the extraction layer
before upload, especially for health, fertility, finance, or other sensitive
domains.

## Local Automation Notes

When driving the API from local scripts, write multiprocessing workflows to
real `.py` files instead of piping them through stdin or heredocs. On macOS,
process-pool workers cannot re-import the `__main__` module from `<stdin>`,
which breaks cluster/layout runs. Tests that intentionally need in-process CPU
execution can use the `inline_cpu_runs=True` settings seam; agents and the
reviewer have both hit this stdin/process-pool footgun.

## Checks

```sh
.venv/bin/pytest
.venv/bin/pytest -m "not slow"
.venv/bin/ruff check .
npm run check
npm run build
```

The default pytest command runs the full coverage-preserving suite, including
slow gates. Use `-m "not slow"` for the fast inner-loop tier; CI runs the full
suite in parallel with `pytest -n auto`.

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
