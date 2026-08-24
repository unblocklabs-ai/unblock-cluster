# Pipeline And Data Model

Agents upload already-normalized, redacted records, run the pipeline, and then
share the `vizUrl` or call evidence recipes. The backend does not extract from
source systems and does not redact sensitive data — see [Privacy](privacy.md)
for exactly which flows transmit customer text.

## Minimum Pipeline

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
   `{"representation":"summary"}` — see
   [Summarize-Then-Embed](summarize-then-embed.md).
4. `POST /api/graphs/:gid/embeddings` (body may override any `embedding.*`
   config key, e.g. `requestsPerMinute` / `maxConcurrency` to stay inside the
   API key's rate limits without going serial).
5. `POST /api/graphs/:gid/views/:vid/cluster`.
6. `POST /api/graphs/:gid/views/:vid/layout`.
7. Optional: `POST /api/graphs/:gid/views/:vid/label` — see
   [Labeling](labeling.md).
8. Optional: `POST /api/graphs/:gid/views/:vid/trends`.
9. Open `/?graphId=...&viewId=...` or fetch evidence.

Every pipeline POST from step 3 onward returns a run (graph creation returns
the graph; record upload returns `{created, updated, rejected}` counts). Poll
`GET /api/graphs/:gid/runs/:runId` until `status` is `succeeded` (or `failed`
with `errorText`) before the next step; `progress` reports live counts for
provider-bound runs (embed/summarize/label) and phase markers for
cluster/layout/trend. Run responses use camelCase keys:
`id`, `graphId`, `viewId`, `type`, `status`, `params`, `progress`,
`errorText`, `inputRefs`, `stats`, `createdAt`, `startedAt`, and
`completedAt`. `POST /api/graphs/:gid/runs/:runId/cancel`
cancels queued runs always and embed/summarize/label runs between batches;
running cluster, layout, and trend runs are not interruptible. Cancelling a
running run returns with `status` still `running`; poll until it flips to
`cancelled`.

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

Add `--summarize --representation summary` to run summarization (unchanged
records reuse cached summaries via content-addressing) and build a
summary-backed embedding run. Add `--base-url http://127.0.0.1:8080` to drive
an already-running server; without `--base-url`, the script opens an
in-process app against `--data-dir` and still uses only API endpoints. The
script promotes each successful cluster/layout/label/trend run with
`setDefault:true`, prints run ids, stats, token usage, and the final `vizUrl`.

## Records And Validation

Required fields (each a non-empty string): `recordId`, `sourceType`,
`sourceName`, `sourceRecordId`, `customerText`, `timestamp`. Optional: `title`, `recordUrl`,
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

## Views And Scopes

Graph creation makes an `all_records` view. Additional views are named,
persistent slices created with `POST /api/graphs/:gid/views`:

```json
{
  "name": "december_negative_reviews",
  "scope": {
    "sourceTypes": ["product_review"],
    "sentiments": ["negative"],
    "timeRange": {"start": "2025-12-01T00:00:00Z", "end": "2025-12-31T23:59:59Z"}
  }
}
```

Scope keys: `sourceTypes`, `sourceNames`, `products`, `skus`, `sentiments`,
`ratings {min,max}`, `timeRange {start,end}` (both bounds inclusive — end a
window just before the next boundary, e.g. `2025-12-31T23:59:59Z`), `tagsAny`,
and `metadataEquals` (exact match on custom `metadata` keys). The `scope` key
itself is required; an empty scope (`"scope": {}`) means all active records.
Embeddings are shared across views; each view runs its own cluster / layout /
label / trend runs on demand. Views are post-ingest slices, not a substitute
for pre-upload filtering.

Successful view-scoped runs promote themselves to the view's defaults unless
`setDefault` is explicitly `false` (focus cluster runs are always
non-promoting, and requesting `setDefault: true` on one returns 422). A non-promoting run still persists its
memberships, labels, trend rows, or layout points; it simply leaves
`default*RunId` fields unchanged.

Inspection reads (each resolves the view's default runs, overridable by id;
labels are resolved as the latest label per cluster for the resolved cluster
run, not through `defaultLabelRunId`):

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
- `trend`: temporal aggregation over persisted cluster membership. The math is
  fast and in-process, but the POST still enqueues a run to poll like the
  others.

The artifact endpoint and most evidence recipes are synchronous reads. They
create no runs (evidence calls do insert one `analysis_events` audit row,
which is why read-only mode still allows the evidence POST). The one documented evidence exception is `topic_search` /
`question_evidence`, which makes exactly one question-embedding provider call
using the same embedding provider/model as the resolved embedding run, then
ranks topics in memory without persisting the question vector.

Summarize-run reports are synchronous reads:
`GET /api/graphs/:gid/summarize-runs/:runId/report`.
