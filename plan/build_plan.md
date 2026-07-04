# Data Graph v2 — Build Plan

Companion to `initial_draft.md`. That document defines the product direction;
this one defines the concrete build: decisions, libraries, schema, pipeline
design, API surface, and phased implementation.

## Locked Constraints (from product owner)

- Clean break. The existing codebase is unused; no migration path, no
  backward-compatible artifacts. Old data can be discarded.
- Local-first, single machine, no auth. Remote sharing, if ever needed, happens
  via a Cloudflare tunnel outside this codebase.
- Plain REST over localhost. No MCP surface in v1.
- Design for 100k records from day one.
- OpenAI-only providers for embeddings and labeling (one API key).
- Async runs with agent-settable throughput (requests-per-minute and
  concurrency caps) so embedding jobs neither swamp the device nor run serially.
- Real customer data lives only on OpenClaw devices; this repo tests against a
  generated synthetic dataset.

## Greenfield Strategy

Build a new Python package (`datagraph/`) in this repo. Do not refactor
`server.py` / `processor.py` — they share nothing with the new design.

- **Phase 0 deletes the legacy backend** (`server.py`, `processor.py`,
  `scripts/data_graph_*`, python tests, `local-data/`). Git history preserves
  everything; keeping dead code around only invites confusion.
- **The frontend survives.** `src/app.js`, `src/uiState.js`, `src/styles.css`,
  the OpenLayers map/list UI, and the Vite setup are the one reusable asset.
  They are frozen until Phase 7, then adapted to the new run-based artifact.
- Same repo, not a new one: the UI port is much easier with both halves in one
  tree, and the repo name/tooling (ruff, vite, node tests) carry over.

---

## Library and Tool Decisions

### The stack

| Concern | Choice | Version pin strategy |
|---|---|---|
| Web framework | FastAPI + uvicorn | keep current pins |
| Validation | Pydantic v2 | comes with FastAPI |
| Storage | SQLite via stdlib `sqlite3`, WAL mode | stdlib |
| Vectors | NumPy float32 blobs + brute-force cosine | `numpy` |
| Clustering-space reduction | `umap-learn` | latest 0.5.x |
| Clustering | `hdbscan` (McInnes package) | latest 0.8.x |
| 2D layout | `umap-learn` (again, different params) | — |
| Embeddings + labeling | `openai` official SDK (async client) | latest 1.x |
| Token counting/truncation | `tiktoken` | latest |
| Testing | `pytest` (backend), `node --test` (frontend, unchanged) | — |

Removed relative to today: `pacmap`, TF-IDF path, raw-HTTP OpenAI calls.
Never added: `faiss`.

Environment (verified in Phase 0–3): the venv runs **Python 3.14**. numba,
UMAP, and HDBSCAN are functional on it (verified by real fits, not just
imports). uvicorn is pinned to `http="h11", loop="asyncio"` because its
standard transports reset HTTP requests under 3.14 on this machine — do not
"clean up" that pin without re-verifying serving.

### Rationale and alternatives considered

**`umap-learn` + `hdbscan` directly, not BERTopic.**
BERTopic validates exactly this pipeline (embed → UMAP → HDBSCAN → label) and
is the strongest argument that the architecture is sound. But BERTopic owns its
pipeline state as an in-memory model object; we need every stage persisted as
an independent, re-runnable, DB-backed run with its own config and outputs,
plus views, embedding reuse, and evidence APIs. Wrapping BERTopic would mean
fighting its object model everywhere. The raw components are ~200 lines of
pipeline code we fully control. Same reasoning excludes `top2vec`.

**The `hdbscan` package, not `sklearn.cluster.HDBSCAN`.**
The sklearn port lacks GLOSH outlier scores (`outlier_scores_`) and soft
cluster membership. The plan requires per-record `probability` and
`outlierScore` persisted for every cluster run — the original package provides
both (`probabilities_`, `outlier_scores_`). If 100k-point performance ever
becomes a problem, `fast_hdbscan` is a drop-in fallback for low-dimensional
input (which is exactly what we feed it post-UMAP); note it and move on.

**UMAP for both clustering space and 2D layout; drop PaCMAP.**
One manifold library instead of two, with different params per role
(see Pipeline). PaCMAP arguably preserves global structure slightly better for
display, but not enough to justify a second heavy numba dependency. Layout
params live in config, so swapping the layout method later is a contained
change. This resolves open question #2 from the draft.

**Brute-force NumPy instead of FAISS (or sqlite-vec, Qdrant, pgvector).**
At the ceiling — 100k × 1536-dim float32 — the full matrix is ~614 MB and a
normalized matrix–vector cosine scan takes tens of milliseconds. Approximate
indexes buy nothing at this scale except recall loss and index-maintenance
code. SQLite stays the single source of truth; the in-memory matrix is
rebuilt from it on demand. Revisit only if the product goes hosted
multi-tenant (then pgvector/Qdrant, per the draft).

**stdlib `sqlite3`, not SQLAlchemy.**
~14 tables, one process, local file. A thin data-access module with explicit
SQL and a tiny versioned-migration runner (`PRAGMA user_version`) is easier to
debug than an ORM layer and keeps the dependency set lean.

**`openai` official SDK, not raw HTTP.**
The current code hand-rolls the embeddings endpoint. The SDK gives async
support, retries, typed errors, and the chat/structured-output surface needed
for labeling — all things we'd otherwise reimplement.

**`tiktoken`** because embedding inputs are capped at 8,191 tokens each and
~300k tokens per batched request; long support tickets must be truncated
deterministically, and batches must be packed token-aware.

---

## Rulings on the Draft's Open Questions

1. **TF-IDF**: removed entirely — not a hidden fallback, not in public config.
   For offline dev and CI, an internal `mock` embedding provider generates
   deterministic hash-seeded vectors (same shape/dtype as real ones). It
   exercises the full pipeline without network or cost, and is clearly labeled
   as producing semantically meaningless clusters.
2. **Layout method**: UMAP 2D. PaCMAP dependency dropped. See above.
3. **Timestamp formats**: ISO 8601 strings only. Offset-aware values are
   converted to UTC; naive values are interpreted as UTC (accepted, since
   agents will inevitably send them). Date-only ISO strings are accepted as
   midnight UTC. Epoch numbers are rejected — the agent converts. Stored as
   both canonical UTC ISO string and `timestamp_ms` integer column for range
   queries and bucketing.
4. **Label inheritance across cluster runs**: none in v1. Every cluster run is
   labeled independently — with `gpt-5.4-mini` at ~30 clusters per run this
   costs cents, and independent labels avoid silently-wrong inherited names.
5. **Topic lineage**: deferred, deliberately unblocked. Because every run's
   full membership table is persisted, lineage between any two cluster runs is
   computable offline at any time. When built: primary signal = member-record
   Jaccard overlap (exact, cheap), tiebreak = centroid cosine similarity.
   Nothing in v1 depends on it; the default "trend the base view's clusters"
   strategy avoids needing it.
6. **Validation strictness**: strict on a small core, open beyond it.
   Required: `recordId`, `sourceType`, `sourceName`, `sourceRecordId`,
   `customerText` (non-empty), `timestamp` (valid per #3). **`title` becomes
   optional** — a deliberate divergence from the draft, because Instagram/
   Facebook comments and many reviews have no title. Everything else
   (`recordUrl`, `product`, `sku`, `rating`, `sentiment`, `tags`, `metadata`)
   is optional and typed when present; `metadata` is an open object; explicit
   `null` is treated as absent for every optional field. Batch uploads are
   atomic by default with a per-record error report (returned as
   `{"detail": {"rejected": [...]}}`); an explicit `"onInvalid": "skip"`
   accepts the valid subset and returns the rejects. Re-uploading an existing
   `recordId` upserts; duplicate recordIds within one batch resolve
   last-write-wins.

---

## Package Layout

```
datagraph/
  __init__.py
  main.py                 # app factory, startup (migrations, run recovery), uvicorn entry
  settings.py             # env config (OPENAI_API_KEY, data dir, port)
  db.py                   # sqlite connection mgmt, WAL, migration runner
  migrations/             # 001_initial.sql, ...
  models.py               # pydantic: record template, graph config, run params, responses
  api/
    graphs.py             # graph CRUD
    records.py            # batch upsert, list, get
    views.py              # view CRUD + view-scoped actions and reads
    runs.py               # uniform run polling/list/cancel
    evidence.py           # recipe endpoint
    artifact.py           # composed visualization artifact
  runs/
    executor.py           # run queue, statuses, process pool, startup recovery
    embed.py              # embedding run
    cluster.py            # cluster run (UMAP reduce + HDBSCAN)
    layout.py             # 2D layout run
    label.py              # LLM labeling run
    trends.py             # temporal analysis run
  core/
    embedding_text.py     # field selection, template rendering, labeled-lines fallback
    openai_client.py      # async embeddings + chat, token bucket, retries
    vectors.py            # blob pack/unpack, L2 normalize, cosine top-k
    representatives.py    # per-cluster representative selection
    trend_math.py         # buckets, baselines, spike scores (pure functions)
    recipes.py            # evidence recipes over persisted runs
scripts/
  gen_synthetic.py        # deterministic synthetic supplement-DTC dataset
  quality_eval.py         # optional real-embedding quality scoring (needs API key)
src/                      # existing frontend, adapted in Phase 7
tests/                    # pytest + existing node tests
```

Single process. Uvicorn serves the API; a `ProcessPoolExecutor(max_workers=1)`
executes CPU-heavy runs (UMAP/HDBSCAN) so numba jobs never block the event
loop; IO-heavy runs (embedding, labeling) run as asyncio tasks in-process.

---

## Storage Schema

SQLite, WAL mode, one file per install (`data/datagraph.sqlite3`). All ids are
prefixed ULIDs (`grf_…`, `run_…`, `view_…`) — sortable, copy-paste friendly.

Key change from the draft: **one uniform `runs` table for all run types**
instead of per-type run tables. Every expensive operation shares lifecycle
mechanics (queue → running → succeeded/failed, progress, params, error), so
the executor and the polling API are written once. Type-specific *outputs*
get their own tables.

```sql
graphs            (id, name, config_json, created_at, updated_at)

records           (id, graph_id, record_key,            -- record_key = agent recordId, unique per graph
                   source_type, source_name, source_record_id,
                   title, customer_text, record_url,
                   product, sku, rating, sentiment, tags_json,
                   timestamp_utc, timestamp_ms,
                   metadata_json, normalized_json,
                   created_at, updated_at)
                   UNIQUE(graph_id, record_key)

views             (id, graph_id, name, description, scope_json,
                   default_embedding_run_id, default_cluster_run_id,
                   default_layout_run_id, default_label_run_id,
                   default_trend_run_id, created_at, updated_at)
                   UNIQUE(graph_id, name)

runs              (id, graph_id, view_id NULL, type,    -- embed|cluster|layout|label|trends
                   status,                              -- queued|running|succeeded|failed|cancelled
                   params_json, progress_json, error_text,
                   input_refs_json,                     -- e.g. {"embeddingRunId": ..., "clusterRunId": ...}
                   stats_json,                          -- counts, durations, cluster_count, outlier_count...
                   created_at, started_at, completed_at)

embedding_vectors (model, dimensions, text_hash, vector BLOB, created_at)
                   PRIMARY KEY(model, dimensions, text_hash)
embedding_items   (run_id, record_id, text_hash, status)   -- ties a run to its per-record work

cluster_memberships (run_id, record_id, cluster_id, probability, outlier_score, is_noise)
cluster_summaries   (run_id, cluster_id, size, mean_probability,
                     representative_record_ids_json, source_mix_json)

cluster_labels    (id, label_run_id, cluster_run_id, cluster_id,
                   model, prompt_hash, top_k,
                   label, summary, key_signals_json, tags_json,
                   coherent, created_at)

layout_points     (run_id, record_id, x, y)

trend_results     (run_id, cluster_id, bucket_start, count, share, spike_score)
trend_summaries   (run_id, summary_json)               -- new/vanishing/top-changed topics

analysis_events   (id, graph_id, view_id, recipe, params_json,
                   run_refs_json, evidence_json, created_at)
```

Design notes:

- **Embeddings are content-addressed.** `embedding_vectors` is keyed by
  `(model, dimensions, text_hash)` where `text_hash = sha256(rendered
  embedding text)`. Each unique text is embedded and stored exactly once,
  ever. A new embedding run first joins against this table and only calls
  OpenAI for missing hashes — embedding reuse across views, re-uploads, and
  re-runs falls out for free. Vectors are stored L2-normalized float32 blobs.
- **Memberships are always persisted per run**, never overwritten — this is
  what makes deferred topic lineage, run comparison, and reproducible evidence
  possible.
- Indexes: `records(graph_id, timestamp_ms)`, `records(graph_id, source_type)`,
  `cluster_memberships(run_id, cluster_id)`, `trend_results(run_id, cluster_id)`.
- On startup, any run left in `running` is marked `failed` with
  `error_text = "interrupted by restart"`; agents re-trigger.

---

## Graph Config

Stored on the graph, overridable per run request. Full default shape:

```json
{
  "embedding": {
    "provider": "openai",
    "model": "text-embedding-3-small",
    "dimensions": null,
    "textFields": ["title", "customerText", "product", "tags"],
    "textTemplate": null,
    "requestsPerMinute": 500,
    "maxConcurrency": 4,
    "maxInputTokens": 8000
  },
  "cluster": {
    "space": { "method": "umap", "nComponents": 25, "nNeighbors": 15, "metric": "cosine" },
    "hdbscan": {
      "minClusterSize": null,
      "minSamples": null,
      "clusterSelectionMethod": "eom",
      "clusterSelectionEpsilon": 0.0,
      "allowSingleCluster": false
    },
    "seed": 42
  },
  "layout": { "method": "umap", "nNeighbors": 30, "minDist": 0.1 },
  "labeling": { "provider": "openai", "model": "gpt-5.4-mini", "topK": 12, "prompt": null },
  "time": { "timestampField": "timestamp", "bucket": "week" }
}
```

- `embedding.provider` accepts `"openai"` (production) and `"mock"` — a
  deterministic, network-free test/dev provider whose vectors are semantically
  meaningless. Documented as test-only in the README.
- `embedding.textFields` is **required at graph creation** — there is no
  implicit "embed every string field" default. Rendering without a template
  produces stable labeled lines (`title: …\ncustomerText: …`), skipping absent
  fields. `tags` renders comma-joined.
- `cluster.space.method` accepts `"umap"` (default) or `"none"` (HDBSCAN
  directly on normalized embeddings — only sensible for small/clean sets;
  rejected above 20k records). Anything else is a 422. No field is accepted
  and ignored.
- `minClusterSize` default when null: `min(150, max(15, round(0.005 * n)))`,
  and `minSamples` default when null: `min(10, minClusterSize)` — retuned
  2026-07-03 after the real-embedding eval showed the original 0.2%/floor-10
  default (with minSamples tracking minClusterSize) over-splitting 20 planted
  topics into 80 pure fragments at 5k. The 150 cap preserves emerging-topic
  detection at 100k scale. Effective values are echoed in cluster-run stats
  as `effectiveHdbscan`.
- `seed` fixed by default for reproducibility (this forces single-threaded
  UMAP; set `"seed": null` to trade reproducibility for parallel speed).

---

## Pipeline Design

### Embedding run

1. Resolve target records (whole graph). Render embedding text per record;
   truncate to `maxInputTokens` with tiktoken; compute `text_hash`.
2. Diff against `embedding_vectors` — already-known hashes are done instantly.
3. Pack missing texts into batches: ≤ 512 inputs and ≤ 200k tokens per request
   (under OpenAI's 2,048-input / ~300k-token request limits).
4. Dispatch batches through the async OpenAI client behind two controls the
   agent can set per run: a token-bucket rate limiter (`requestsPerMinute`)
   and a concurrency semaphore (`maxConcurrency`). Retry 429/5xx with
   exponential backoff, honoring `Retry-After`.
5. L2-normalize, store blobs, update `progress_json`
   (`{"embedded": 41200, "total": 100000, "reused": 12000}`) as batches land.

100k fresh records ≈ 200–400 requests ≈ minutes at default throttle; a re-run
after adding 2k records embeds only the 2k new texts.

### Cluster run

Inputs: a view + an embedding run (defaults to the view's / latest complete).

1. Load the view's record ids (scope filter applied in SQL), assemble the
   normalized embedding matrix from blobs (~seconds at 100k).
2. Reduce: UMAP `n_components=25, n_neighbors=15, metric="cosine"` → the
   clustering space. (`"none"` skips this for small sets.)
3. Cluster: HDBSCAN on the reduced space, `metric="euclidean"`, with the
   configured `minClusterSize` / `minSamples` / `clusterSelectionMethod` /
   `clusterSelectionEpsilon` / `allowSingleCluster`.
4. Persist per record: `cluster_id` (noise = -1), `probability`
   (`probabilities_`), `outlier_score` (GLOSH `outlier_scores_`).
5. Per cluster: size, mean probability, source mix, and representatives —
   centroid computed in the *original* embedding space over the
   probability ≥ 0.7 members (falling back to all members when the filter
   empties a cluster), members ranked by cosine-to-centroid, top 20 ids
   stored in order.
6. Cluster ids are **canonicalized** before persistence: clusters are
   renumbered 0..n by their smallest member record id. HDBSCAN's raw numeric
   labels are not stable across equivalent re-runs; canonicalization makes
   identical inputs + seed yield identical persisted memberships.
7. Stats: cluster count, noise count/ratio, per-phase durations
   (loading/reducing/clustering/persisting, mirrored live in progress_json),
   params echo.

Runs in the process pool; the API stays responsive; one CPU job at a time,
FIFO.

### Layout run

Independent UMAP 2D projection of the same embedding matrix
(`n_neighbors=30, min_dist=0.1, metric="cosine"`), persisted to
`layout_points`. Never used for clustering — this is the core fix of the
whole redesign. Tied to (view, embedding run), reusable across cluster runs
on the same population.

### Label run

For each non-noise cluster in a cluster run:

1. Take `topK` (default 12) representatives; truncate each record's text to
   ~700 chars.
2. One chat call to `gpt-5.4-mini` with the built-in prompt (the draft's
   supplement-DTC prompt, verbatim) unless `labeling.prompt` overrides it.
3. Structured output (JSON schema): `{label, summary, keySignals[], tags[],
   coherent}` — `coherent: false` is the model's escape hatch for junk
   clusters, surfaced in the API so agents can distrust weak topics.
4. Persist with model + prompt_hash + representative ids. Concurrency 4,
   same retry/backoff machinery as embeddings; schema-invalid model output is
   retried once, then counted as that cluster's failure.

Failure policy: per-cluster failures are isolated — the run succeeds if at
least one cluster labels, with `failedClusterIds` in stats; it fails only if
every target fails. Relabeling is the same endpoint with `clusterIds: [...]`
(that list IS the topic-relabel API). Labels are never overwritten: reads
resolve the NEWEST `cluster_labels` row per (cluster run, cluster id), and
older rows remain as history.

~30 clusters ≈ 30 small calls ≈ cents.

### Trend run

Inputs: cluster run + `time` config + optional analysis window. Pure
SQL + `trend_math.py` (no ML, runs inline, fast):

- Buckets: `day` / `week` (ISO, Monday start) / `month`, all UTC, from
  `timestamp_ms`.
- Series are **zero-filled** across the population's full time span (min
  bucket → max bucket, no gaps) — unfilled gaps would silently corrupt
  baselines. Bucket totals include noise records (totals are the volume
  denominator); per-topic series exist only for non-noise clusters.
- Per (cluster, bucket): `count`, `share = count / bucket_total` (share
  controls for overall volume shifts, e.g. an agent uploading a new source).
- Baseline per (cluster, bucket): mean and population std over the prior 8
  buckets of the zero-filled series (fewer at series start; empty → 0/0).
- `spike_score = (count − mean) / max(std, sqrt(mean), 1)` — a variance-floored
  z-score that behaves for small counts.
- Window summary (window defaults to the full span): `surprisingTopics` =
  ranked by max spike score in window; `newTopics` = first-ever nonzero
  bucket falls in the window, is NOT the first bucket of the whole series,
  and window count ≥ 5; `vanishingTopics` = zero count in window with a
  healthy pre-window baseline (mean ≥ 1.0 over the 8 buckets before it);
  `rising`/`fallingTopics` = delta of mean share, window vs the 8 prior
  buckets. Sections requiring a pre-window baseline are empty when the window
  starts at the series start.

All math in `trend_math.py` as pure functions over count tables →
deterministic unit tests with planted fixtures.

### Evidence recipes

Synchronous reads over persisted runs (no new computation). If a required run
is missing/stale, the response is an actionable 409: *"view has no trend run
for this window; POST /views/:id/trends"* — the agent, not the service,
decides to spend compute. Recipes v1:

| Recipe | Returns |
|---|---|
| `surprising_topics` | topics ranked by max spike score in window |
| `new_topics` | topics first appearing in window |
| `rising_topics` / `vanishing_topics` | ranked share deltas |
| `topic_evidence` | one topic: label, summary, trend series, source mix, representatives with text + URLs |
| `compare_periods` | per-topic share deltas between two windows |

Every response carries `runRefs` (embedding/cluster/label/trend run ids),
source mix, representative record ids, and a visualization URL. Each call is
persisted to `analysis_events`.

---

## API Surface

Simplified namespace (greenfield): `/api/graphs`, not `/api/data-graph`.

```
Graphs
  POST   /api/graphs                          {name, config}  → graph + auto-created "all_records" view
  GET    /api/graphs                          list
  GET    /api/graphs/:gid                     config, counts, views summary
  PATCH  /api/graphs/:gid                     config updates
  DELETE /api/graphs/:gid

Records
  POST   /api/graphs/:gid/records             batch ≤1000, upsert by recordId, atomic | onInvalid:"skip"
  GET    /api/graphs/:gid/records             paged; filters: sourceType, time range, product, sentiment
  GET    /api/graphs/:gid/records/:rid

Runs (uniform lifecycle)
  GET    /api/graphs/:gid/runs                ?type=&status=
  GET    /api/graphs/:gid/runs/:runId         status, progress, params, stats, error
  POST   /api/graphs/:gid/runs/:runId/cancel  queued: always; embed/label: between batches/calls; running CPU jobs: not interruptible (numba)

Embedding
  POST   /api/graphs/:gid/embeddings          optional overrides (model, throttle) → run

Views
  POST   /api/graphs/:gid/views               {name, description, scope}
  GET    /api/graphs/:gid/views               each with default run ids + freshness (records added since run)
  GET    /api/graphs/:gid/views/:vid
  POST   /api/graphs/:gid/views/:vid/cluster  → run   (params optional; embedding run defaults to latest complete)
  POST   /api/graphs/:gid/views/:vid/layout   → run
  POST   /api/graphs/:gid/views/:vid/label    → run   (requires completed cluster run)
  POST   /api/graphs/:gid/views/:vid/trends   → run
  GET    /api/graphs/:gid/views/:vid/records
  GET    /api/graphs/:gid/views/:vid/topics             labels + size + source mix + trend snapshot + coherence
  GET    /api/graphs/:gid/views/:vid/topics/:tid
  GET    /api/graphs/:gid/views/:vid/topics/:tid/records   STORED representatives in stored order (topK default 12, max 50)
  GET    /api/graphs/:gid/views/:vid/outliers              high outlier_score + noise records
  GET    /api/graphs/:gid/views/:vid/trends                default trend run (?trendRunId= override): summary + per-topic series
  GET    /api/graphs/:gid/views/:vid/artifact              composed visualization JSON (see draft's shape)

Evidence
  POST   /api/graphs/:gid/evidence            {viewId, recipe, timeRange?, topK?} → structured payload + runRefs
```

View `scope_json` filter language (post-ingest scoping only, per the draft's
boundary): `sourceTypes[]`, `sourceNames[]`, `products[]`, `skus[]`,
`sentiments[]`, `ratings {min,max}`, `timeRange {start,end}`, `tagsAny[]`,
plus `metadataEquals {key: value}` for agent-defined custom fields. Compiled
to SQL against the typed columns / `metadata_json`.

The artifact endpoint returns the draft's proposed shape (config, runRefs,
layout, topics with labels/sourceMix/trend, per-record x/y/cluster/probability/
outlierScore) composed from the view's default runs — this is the only thing
the UI consumes.

---

## Synthetic Dataset and Quality Verification

Real data never leaves OpenClaw devices, so the repo ships its own ground truth.

`scripts/gen_synthetic.py` — deterministic (seeded), no API calls:

- ~20 planted supplement-DTC topics (subscription cancellation errors, gummy
  melting in shipping, taste complaints, efficacy questions, refund friction,
  adverse-event mentions, …) with hand-written phrase pools and combinatorial
  paraphrasing.
- 6 source types with realistic field presence (comments lack titles/ratings).
- Sizes: 5k / 50k / 100k. Timestamps spread over 2025 with planted patterns:
  a December 2025 spike topic, a topic that first appears in November, and a
  topic that dies mid-year — so the flagship question ("was there a surprising
  topic in December 2025?") has a known correct answer.
- Ground-truth topic id stored in each record's `metadata`.

Three test tiers:

1. **CI / offline (mock embedding provider)**: pipeline plumbing correctness —
   validation, hashing/reuse, run lifecycle, persistence shapes, trend math
   against planted counts, evidence recipes, artifact shape. No network, no
   cost, no assertions about semantic cluster quality.
2. **CI / offline (structured mock provider)** — added in Phase 3 and now the
   load-bearing correctness tier: a test provider returns
   `unit_normalize(topic_centroid + seeded noise)` per rendered text, keyed
   off each synthetic record's ground-truth topic, injected through the
   executor's provider-factory seam. The REAL pipeline (matrix assembly →
   UMAP → HDBSCAN → persistence → trends) must recover the planted topics at
   ARI ≥ 0.8 and the planted temporal patterns, deterministically, offline.
3. **`scripts/quality_eval.py` (real API key, run manually)**: embeds the 5k
   set with `text-embedding-3-small` (~$0.05), runs the real pipeline, scores
   cluster assignments against planted topics (adjusted Rand index / NMI),
   labels with `gpt-5.4-mini`, and checks the December spike surfaces in
   `surprising_topics`. Gate before calling the pipeline "working": ARI ≥ 0.5
   on the 5k set and the planted spike ranked #1.

### Real-key acceptance ledger — COMPLETED 2026-07-03

All checks run with a real `OPENAI_API_KEY`; results:

- Phase 2 — **passed**. Real 5k embed succeeded (batched, progress observed);
  a re-embed reported 100% reuse with ZERO provider requests. Throttle note:
  at this scale (~10 requests/run) the 500-RPM default is never approached,
  so rate-bounding is verified by the fake-clock unit tests, not live load.
- Phase 3 — **passed**. `quality_eval.py` on real embeddings: ARI 0.719
  (gate ≥ 0.5), NMI 0.864, noise 3.0%. Observation: HDBSCAN produced 80
  clusters against 20 planted topics — pure-but-split subclusters (high NMI).
  The default `minClusterSize` (0.2% → 10 at 5k) over-splits; real datasets
  will likely want larger values. Tuning input for the 100k pass.
- Phase 4 — **passed** on label quality: real `gpt-5.4-mini` labels are
  human-recognizable and map cleanly to planted topics ("Heat-Damaged
  Shipping Products", "Energy wear-off by afternoon", "Adverse Reactions:
  Headache/Nausea", "Denied return/refund follow-ups"); 80/80 and 17/17
  labeled with zero failures across two runs. Caveat: no genuine junk
  cluster occurred in these runs, so `coherent: false` from the real model
  remains exercised only by the offline plumbing tests.
- Phase 5 — **passed**. On real embeddings, the planted December spike ranks
  #1 in `surprising_topics` with spike score 69.0 (25x the runner-up),
  topBucket 2025-12-01.

Both opt-in real-API pytest tests pass. (The Phase 2 one had a latent
fixture bug — 10 identical texts correctly dedupe to 1 content-addressed
vector — fixed to use distinct texts; first real execution caught it.)

Performance acceptance: full 100k pipeline (embed cached → cluster → layout →
label → trends) completes in under ~30 minutes on an M-series laptop, API
responsive throughout.

---

## Implementation Phases

Each phase lands with tests and README updates, and each leaves the service
in a usable state.

### Phase 0 — Scaffold and clean break
- Delete legacy backend (`server.py`, `processor.py`, `scripts/data_graph_*`,
  python tests, `local-data/`); freeze `src/` untouched.
- `datagraph/` package skeleton, settings, sqlite + migration runner, full
  initial schema, uniform `runs` table + executor (queue, statuses, process
  pool, startup recovery, cancel).
- Mock embedding provider. `gen_synthetic.py`. pytest wiring, ruff config.
- **Done when**: app boots, migrations apply, a no-op run moves
  queued → running → succeeded via the poll API.

### Phase 1 — Graphs, records, views
- Graph CRUD with config validation (reject unknown cluster-space methods,
  require `embedding.textFields`).
- Record template validation per ruling #6; batch upsert (atomic +
  `onInvalid: "skip"`); timestamp parsing per ruling #3.
- Views CRUD, scope compilation to SQL, auto `all_records` view.
- **Done when**: synthetic 5k set uploads cleanly; bad records produce precise
  per-record errors; scoped views return correct record counts.

### Phase 2 — Embedding runs
- Embedding-text rendering (fields + template + truncation), content-addressed
  vector store, token-aware batching, async OpenAI client with RPM token
  bucket + concurrency semaphore + retries, progress reporting.
- **Done when**: 5k set embeds with a real key; immediate re-run reports 100%
  reused and makes zero API calls; throttle settings observably bound request
  rate.

### Phase 3 — Cluster and layout runs
- UMAP reduction, HDBSCAN, membership/summary/representative persistence,
  layout run, topics/outliers/records read APIs.
- **Done when**: quality_eval passes its ARI gate on the 5k set; noise and
  outlier scores persisted; layout coordinates provably not used for
  clustering (separate runs, separate tables).

### Phase 4 — LLM labeling
- Label run with structured outputs, default prompt, `coherent` flag,
  relabel-single-topic support via run params.
- **Done when**: synthetic topics get labels a human recognizes; junk cluster
  fixture yields `coherent: false`.

### Phase 5 — Trend runs
- `trend_math.py`, trend run + persistence, trend snapshot merged into the
  topics read API.
- **Done when**: planted December spike, new topic, and vanishing topic are
  each detected with correct bucket attribution (deterministic tests).

### Phase 6 — Evidence recipes
- Five recipes, actionable 409s for missing runs, `analysis_events`
  persistence.
- **Done when**: the flagship question is answerable end-to-end by REST calls
  alone: upload → embed → cluster → label → trends →
  `evidence(surprising_topics, Dec 2025)` returns the planted topic first,
  with runRefs and representatives.

### Phase 7 — UI port and cleanup
- Adapt `src/` to the artifact endpoint: topic labels/summaries in the panel,
  trend badges (spike/new), source-mix display, time filter, outlier styling,
  probability-aware point rendering; keep map/list modes.
- Delete remaining dead frontend paths; README final pass.
- **Done when**: `npm run dev` renders the 5k synthetic graph with labeled,
  trend-annotated topics from a fresh pipeline run.

### README contract (written in Phase 1, maintained throughout)
- The normalized record template and validation rules.
- The agent/service boundary: extraction, pre-filtering, aggregation, and
  redaction belong to OpenClaw agents; imported-but-excluded data is not
  audited here.
- **Privacy statement**: customer text is sent to OpenAI for embeddings and
  labeling; agents must redact sensitive values before upload. No redaction
  pipeline exists in this service.
- The happy-path curl sequence (the draft's 14-step flow, concretized).

---

## Risks and Watch Items

- **UMAP at 100k, seeded**: reproducibility forces single-threaded layout;
  if runtimes annoy, expose `seed: null` guidance or precompute layout less
  often (layout is reusable across cluster runs).
- **HDBSCAN parameter sensitivity**: `minClusterSize` heuristics may need
  tuning per dataset shape; the quality_eval script is the feedback loop, and
  all params are per-run overridable so agents can iterate without redeploys.
- **OpenAI request limits drift**: batch-size and token caps are config, not
  constants; verify current limits at implementation time.
- **SQLite write contention**: single-process + WAL + one CPU worker keeps
  writers serial; embedding batch commits are chunked. If it ever bites,
  batch inserts per transaction (already the plan).
- **SQLite connections must be explicitly closed** (resolved in Phase 2):
  `with sqlite3.connect(...)` commits but does not close; relying on GC
  exhausted macOS's default `ulimit -n 256` during the 5k embed run. `db.py`
  now uses a closing connection factory + `busy_timeout` — keep that pattern.
- **tiktoken fetches `cl100k_base` over the network on first-ever use** per
  machine (then caches). A fully offline fresh install fails at import of the
  embedding-text module. Relevant if OpenClaw devices are network-restricted;
  vendoring the encoding file is the fix if it ever bites.
- **`coherent: false` topics**: surfaced but not auto-hidden; agents decide.
  Revisit if noisy labels confuse users.

---

## Backlog

Ordered; top item is the next phase candidate.

1. **Summarization runs (Phase 14 — decided 2026-07-04).** Optional
   service-side summarize-then-embed: per-record `gpt-5.4-nano` structured
   extraction with a service-owned strict schema (issue, product,
   desiredResolution, sentiment, verbatim keyCustomerPhrases, junkType
   gate), an optional agent-supplied `context` string (static brand/service
   background injected into the summarizer prompt — better spam judgment;
   part of the prompt hash), content-addressed summaries
   (model, promptHash, textHash), `representation: "raw"|"summary"` on
   embedding runs (A/B = two runs on one graph), junk gating at the embed
   boundary, `summary.<key>` facets, receipts stay raw customer text.
   Solves round 3's leftovers: semantic junk filtering (regex whack-a-mole
   retired), facet starvation, length variance. Acceptance: Sakara A/B vs
   the concat graph, watching for homogenization (suspiciously merged
   topics).
2. Embedding-matrix load optimization (94s of the 100k cluster run).
3. Demand-gated: post-hoc topic merging; event annotations; second-brand
   portability test of the playbook/recipe split.

## Amendments

**2026-07-04 (f)** (Phase 13). Extraction-quality guidance from the pilots
is now standing README agent contract (representation dominates, message-
level filtering, facet-worthy metadata, history backfill, diagnostic table,
iteration practices); HEAD supported on all routes via app-level
HEAD-as-GET middleware (tunnel health checks). Summarization-runs decision
recorded in the new Backlog section above.

**2026-07-04 (e)** (pilot round 3 — concat rerun + Phase 11 real-data
acceptance). The agent re-extracted with chronologically concatenated
customer-authored messages (its own round-1 finding) and the round-2
mega-topic dissolved AT THE INPUT LAYER: "Order status and support
requests" (1,145, 45%) became "Warm or spoiled deliveries" (826) +
"Delivery issues and address changes" (526) + concrete operational themes.
Core lesson: EMBEDDING-TEXT QUALITY DOMINATES TOPIC QUALITY — thin
first-message/preview text produces vague mega-topics no clustering
parameter can fix; the boundary held again (the fix was entirely
agent-side). Token profile: max 2,569 / p95 960 / zero at the 8k cap.
Noise rose 0.59% → 6.42% with richer texts (expected, more honest).
Phase 11 acceptance on real data: focus reclustering PASSED — cluster 1's
default focus split produced six genuinely routeable children (spoiled
meals / address changes / cancel delivery / missing deliveries), focus
runs took 0.7–1.2s, and focus doubled as a JUNK DETECTOR (a 120-record
sales-pitch pocket hiding inside the largest topic); leaf-inside-focus
over-fragments (~50% to noise) — eom default is the right operator view.
facetBy: feature correct, usefulness gated by metadata population —
channel facets excellent; product/SKU dominated by "(none)"/"(other)";
primaryTag is VIP/LTV not issue taxonomy. Agent guidance: map issue-
taxonomy fields into metadata at extraction time. New filter lesson:
junk rules are REPRESENTATION-DEPENDENT — single-message rules over-fire
on concatenated threads (footers, quoted text, customer travel context);
filter at message level before concatenation. Phase 10 verified on the
pilot machine (gzip + ETag + 304). Backlog from round 3: HEAD support on
read endpoints (artifact returns 405 to curl -I / uptime checks); README
facet-metadata guidance; agent-side: next OOO rule + wellness-flavored
sales-pitch rule (playbook updated). Prior backlog still open: embedding
matrix load optimization (94s of the 100k cluster run).

**2026-07-04 (d)** (pilot round 2 + Phase 10 + topics research). Round 2
(filtered re-run, same Sakara data): junk share 41% → 2.7%, 12 topics /
0.59% noise, surprising_topics 5/5 genuine, coherent:false 0 fires (as
predicted on clean input), setDefault tuning workflow and mismatch warnings
behaved exactly as designed, zero human interventions again. The agent
versioned its filter rules as `sakara-kustomer-filter-recipe-v1` (10
reason-bucketed rules). New quality frontier: a 45% mega-topic ("Order
status and support requests", 1,145 records) that global minClusterSize
tuning provably cannot split (8→13 clusters, 30→9; the blob persists).
Phase 10 shipped tunnel-readiness: gzip (artifact 3.23MB → 0.42MB wire),
artifact ETag/304 + bounded compose cache (review caught a stale-ETag bug —
label-table freshness must be an ETag input because labels merge
newest-wins across ALL runs, not just the default), 4dp transport floats,
records-list `normalized` now opt-in, immutable asset caching, debounced
search, cached OL styles, 500-row list cap.
Topics research (UMAP/HDBSCAN/BERTopic docs): our clustering-space UMAP
used the visualization default `min_dist=0.1` — UMAP's own clustering guide
says 0.0 ("pack points densely… cleaner separations"); the mega-topic is
EOM's documented signature ("one or two large clusters plus many small
ones"); hdbscan's BranchDetector is explicitly not a re-clustering tool;
BERTopic has no drill-down either (merge-only hierarchy). Phase 11
decisions: expose `cluster.space.minDist` (OUTCOME: the UMAP-docs-recommended
0.0 default was falsified by the real-key eval — ARI 0.697 vs 0.860, 64 vs
43 clusters, over-fragmentation per the docs' own "false tears" warning —
so the default stays 0.1 with 0.0 documented as a setDefault:false
experiment; our empirical evals outrank generic library guidance for this
data regime); `focus: {clusterRunId, clusterId}` reclustering — local UMAP
re-spreads variance the global projection compressed; focus runs are
inspection runs (setDefault forced false, read via ?clusterRunId=
overrides); `facetBy` breakdowns on topics/evidence reads (generalizing
sourceMix to any record field or metadata key — double-confirmed by both
pilot rounds); README guidance for leaf/epsilon escape hatches. Real-data
acceptance deferred to the OpenClaw: focus-recluster the Sakara mega-topic
and judge whether the children are actionable.

**2026-07-03 (c)** (after the first real-data pilot — Sakara Kustomer/NPS,
3,509 records, full OpenAI pipeline). Findings: the retuned HDBSCAN defaults
validated on real language (26 topics; the agent's 0.5x/2x sweep bracketed
the default as best); embedding reuse, freshness, evidence loop, and
`coherent: false` (6/6 correct on score-only NPS clusters) all worked; zero
validation rejects, zero 409s, zero human interventions in the API flow. The
two real findings: (1) ~41% of the graph was non-support junk (promo/OOO/
tracking/score-only records) — the agent-boundary exclusion list in this doc
predicted it exactly but was never surfaced in the README agent contract;
(2) a design bug — every successful run promotes itself to the view default,
so tuning cluster runs silently repointed /artifact at an unlabeled run.
Phase 8 decisions: `setDefault` request param (default true) on all
view-scoped run POSTs; explicit `warnings` on artifact/topics when resolved
runs mismatch (no silent label/trend drops); run responses normalized to
camelCase (the API's only snake_case surface — a pilot DX complaint);
`providerRetries` in embed stats; README gains a pre-upload filtering
checklist and the iterate-on-quality workflow. Deferred: `facetBy`
breakdowns (any record/metadata key, generalizing sourceMix) — the next
feature candidate, driven by the pilot's unanswerable questions.

**2026-07-03 (b)** (post-ledger). Retuned HDBSCAN defaults per the ledger's
over-splitting finding — minClusterSize 0.5%/floor-15/cap-150, minSamples
decoupled at min(10, minClusterSize) — and added `effectiveHdbscan` to
cluster-run stats. Alternatives considered (clusterSelectionEpsilon, larger
UMAP n_neighbors, minClusterSize sweep on the cached reduction, post-hoc
centroid merging) deferred pending real OpenClaw data; the sweep is the first
candidate for the 100k pass.

**2026-07-03** (after Phases 0–4 closed; Phase 5 in flight). Folded
implementation-settled decisions back into this document so it stays
authoritative: mock embedding provider as a validated config value; cluster-id
canonicalization; representative-selection details (filtered centroid +
fallback); label-run failure policy, relabel-via-clusterIds, and
newest-label-wins merge; pinned trend semantics (zero-fill, noise-in-totals,
new/vanishing/rising definitions, window defaults); record-contract
clarifications (date-only timestamps, null-as-absent, dup-in-batch
last-write-wins, reject envelope); topic-records = stored representatives;
GET trends endpoint; CPU runs not cancellable once running; Python 3.14 +
h11 environment notes; three-tier test strategy (structured mock provider);
deferred real-key acceptance ledger. No architectural changes — the
embed → reduce → cluster → label → trend pipeline, run model, and schema have
held as designed.
