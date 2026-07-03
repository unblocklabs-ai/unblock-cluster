# Data Graph Semantic Topic Intelligence Plan

## Purpose

This document captures the current code review findings and target direction for
evolving Data Graph from a 2D clustering visualization service into an
embedding-first topic intelligence system.

The first and main use case is supplement direct-to-consumer brands. These
customers need to understand themes across consumer support tickets, reviews,
social comments, and similar customer-feedback sources. Visualization can remain
local-first for now.

The operational model is OpenClaw-agent-first:

1. An OpenClaw agent exports source data from a brand's support, ecommerce,
   review, or social platform.
2. The agent filters and normalizes source records into a simple JSON template.
3. The agent creates or updates a data graph from normalized JSON records.
4. The service embeds selected customer-text fields.
5. The service clusters semantically similar records, labels topics, computes
   temporal trend signals, and renders a human visualization.
6. A human can ask an OpenClaw agent follow-up questions such as:
   "Was there a surprising topic in December 2025?"
7. The agent answers using structured evidence from stored views, cluster runs,
   labels, representative records, outliers, trend metrics, and visualization
   links.

The service should stay simple and useful. It should store, embed, cluster,
label, lay out, and return structured evidence. OpenClaw agents should own
source-specific cleanup, brand-specific context, and final reasoning.

## Desired UX

The first UX should be agent-native, not dashboard-admin-heavy.

1. The OpenClaw agent prepares normalized JSON for a brand.
2. The agent creates a graph and uploads records.
3. The service builds reusable embeddings and creates an `all_records` view.
4. The agent triggers clustering, labeling, and layout for that view.
5. The human opens the local visualization.
6. The agent inspects available views, topics, source mix, outliers, and top
   representative records through APIs.
7. The agent creates additional views for slices such as source type, product,
   sentiment, date range, or review-only data.
8. The agent reclusters a view only when the scoped population needs its own
   topic structure.
9. For natural-language user questions, the OpenClaw agent calls evidence APIs
   and writes the final answer itself.

## Non-Goals For The Initial Build

The initial build should intentionally avoid these responsibilities:

- Direct CSV ingestion.
- Direct connectors to support, ecommerce, review, or social platforms.
- Source-specific cleanup logic.
- Pre-embedding inclusion audits for excluded records.
- Generic in-service chat.
- Brand-specific supplement classifiers.
- Automatic PII, health-data, or sensitive-value redaction.

Those jobs belong to the OpenClaw agent until the product has a concrete reason
to move any one of them into this service.

## Current State

The current implementation is a local-first FastAPI service backed by SQLite and
filesystem artifacts. Raw batches are appended to disk, then the whole graph is
reprocessed into a latest JSON artifact after a debounce window.

The current processor flow is:

1. Select text and numeric feature fields.
2. Build either TF-IDF text features or OpenAI embedding features.
3. Concatenate text and numeric feature arrays.
4. Reduce all features to 2D with PaCMAP.
5. Run HDBSCAN on those 2D layout coordinates.
6. Attach `x`, `y`, `clusterId`, `clusterLabel`, and `groupValue` to each row.

There is no UMAP implementation today. `requirements.txt` includes `pacmap` and
`hdbscan`, but not `umap-learn`.

## Validated Findings

### HDBSCAN Runs On Visualization Coordinates

The processor builds a feature matrix, projects it to 2D with PaCMAP, and then
runs HDBSCAN on the 2D points. This makes cluster membership dependent on the
display layout rather than the original semantic vector space or a separate
cluster-optimized embedding space.

This is fragile for support-ticket and feedback analysis because 2D manifold
projections are useful for visualization but can distort density, split
continuous manifolds, and create layout artifacts. Clustering should be computed
in a dedicated clustering space, while 2D coordinates should be generated
separately for the UI.

### UMAP Is Not Implemented

The code and dependency set do not contain UMAP. Any `cluster.method` value that
suggests UMAP support would be misleading until the dependency, configuration,
processor path, and tests exist.

### `cluster.method` Is Accepted But Ignored

The API accepts `config.cluster.method`, but the processor always runs the same
PaCMAP plus HDBSCAN path. Values like `UMAP+HDBSCAN` or arbitrary strings are
accepted by validation but do not change behavior.

The target system should either remove this field or strictly validate it
against implemented methods.

### Defaults Are Unsafe For Arbitrary Feedback JSON

The current defaults include:

- Every String, Boolean, Object, and Array field as text input.
- Every Number field as numeric input.
- TF-IDF as the default text feature method.

For customer feedback, this can accidentally include ticket IDs, emails,
customer names, timestamps, epoch values, URLs, raw objects, and operational
metadata in similarity calculations.

The target system should require agents to explicitly select the JSON properties
used for embedding text.

### Time Currently Has No First-Class Semantics

The current code has no first-class concept of:

- Timestamp fields.
- Time buckets.
- Trend baselines.
- Topic spikes.
- Cluster lineage across runs.
- Time-window-specific cluster runs.

Temporal fields can also accidentally enter numeric feature vectors today, which
can split one semantic issue into separate clusters by time. For the intended
workflow, time should usually be a filter or analysis dimension, not part of the
semantic vector.

### HDBSCAN Outputs Are Underused

The current code only keeps final labels. It does not persist:

- Cluster membership probability.
- Outlier scores.
- Clusterer parameters.
- Cluster selection method.
- Condensed tree or useful debugging metadata.
- Representative points.

For topic intelligence, these outputs matter because agents and humans need to
know whether a topic is stable, weak, noisy, or outlier-heavy.

## Product Boundaries

### OpenClaw Agent Responsibilities

OpenClaw agents should handle source-specific work before data reaches this
service.

Agent responsibilities:

- Export data from customer systems.
- Normalize platform-specific data into the service's JSON template.
- Pre-filter records that should not participate in embedding or clustering.
- Aggregate one support ticket/conversation into one record.
- Choose which text fields should be embedded.
- Include optional custom metadata fields when brand-specific context matters.
- Trigger embedding, view creation, clustering, labeling, layout, and trend runs
  through APIs.

Examples of records agents should exclude before upload:

- Outbound-only company messages.
- Transactional messages.
- Automated campaign sends.
- Records where customer message count is zero.
- Records outside the desired brand, source, date range, or analysis scope.

This boundary should be documented prominently in the README so agents do not
expect this service to audit imported-but-excluded data.

### Service Responsibilities

The service should not encode brand-specific or support-platform-specific rules.
It should expose a clear contract and useful primitives.

Service responsibilities:

- Validate normalized JSON records.
- Store normalized records and custom metadata.
- Build and reuse embeddings.
- Persist views and run outputs.
- Cluster, label, lay out, and package evidence.
- Return structured evidence for OpenClaw agents to reason over.

The service should not directly ingest arbitrary CSV/API payloads in the initial
build. JSON is enough. Agents can convert CSV or API exports to JSON outside this
codebase.

## Normalized Record Template

The initial system should prefer one feedback item per record:

- One support ticket or conversation.
- One Shopify review.
- One Amazon review.
- One Google review.
- One Instagram comment.
- One Facebook comment.

For support platforms, OpenClaw agents should pre-aggregate a
conversation/ticket into one normalized record rather than asking this service
to reason over individual messages.

Recommended required fields:

```json
{
  "recordId": "SUP-123",
  "sourceType": "support_ticket",
  "sourceName": "gorgias",
  "sourceRecordId": "123",
  "title": "Cannot cancel subscription",
  "customerText": "Customer says the subscription portal errors when canceling.",
  "timestamp": "2025-12-08T14:30:00Z"
}
```

Recommended optional fields:

```json
{
  "recordUrl": "https://example.test/tickets/123",
  "product": "Sleep Gummies",
  "sku": "sleep-gummies-60ct",
  "rating": 2,
  "sentiment": "negative",
  "tags": ["subscription", "portal"],
  "metadata": {
    "platformStatus": "closed",
    "orderId": "ORDER-123"
  }
}
```

The schema should allow custom metadata so agents can preserve useful
brand-specific context without requiring this codebase to know every supplement
DTC edge case.

## Agreed Product Direction

### Explicit Embedding Text Selection

Agents should select the JSON properties used to construct embedding text.
There should be no broad default that silently embeds every string-like field.

Example:

```json
{
  "cluster": {
    "embeddingTextFields": ["title", "customerText", "product", "tags"],
    "embeddingTextTemplate": "{title}\n\n{customerText}\n\nProduct: {product}"
  }
}
```

If no template is provided, the service can concatenate selected fields in a
stable, labeled format:

```text
title: Cannot cancel subscription
customerText: Customer says the subscription portal errors when canceling.
product: Sleep Gummies
tags: subscription, portal
```

For the first implementation, the happy path can require agents to put the full
customer-authored semantic text into `customerText` and use
`embeddingTextFields: ["title", "customerText", "product", "tags"]`.

### Embeddings As The Normal Route

The default and ideally only production route should be vector embeddings, not
TF-IDF. TF-IDF can remain only as a local development fallback or be removed from
the production surface.

Recommended defaults:

- Embedding provider: `openai`
- Embedding model: `text-embedding-3-small`
- Optional dimensions override: supported, but not required initially
- API key source: server environment, reused from existing `OPENAI_API_KEY`

### Separate Clustering And Layout Spaces

The target pipeline should compute:

- A semantic embedding vector for each record.
- A clustering representation for HDBSCAN.
- A separate 2D layout representation for browser visualization.

The service should not run HDBSCAN on the 2D visualization coordinates.

Recommended clustering options:

1. For smaller or cleaner datasets, run HDBSCAN directly on normalized
   embeddings with a cosine-compatible strategy.
2. For larger/noisier datasets, reduce embeddings to a clusterable space with
   UMAP, such as 10-50 dimensions, then run HDBSCAN.
3. Independently compute a 2D UMAP or PaCMAP projection for display.

### Better HDBSCAN Configuration

Expose and persist the meaningful HDBSCAN configuration:

- `minClusterSize`
- `minSamples`
- `metric`
- `clusterSelectionMethod`
- `allowSingleCluster`

Persist useful outputs:

- `clusterId`
- `clusterProbability`
- `outlierScore`
- noise label `-1`
- cluster size
- representative record IDs

### LLM Topic Labeling

Use LLM labeling instead of only heuristic labels or c-TF-IDF labels.

The service should reuse the same OpenAI API key used for embeddings. Config
should include:

```json
{
  "labeling": {
    "provider": "openai",
    "model": "gpt-5.4-mini",
    "topK": 12,
    "prompt": ""
  }
}
```

`gpt-5.4-mini` is the desired default and fallback for labeling. If the prompt is
empty or missing, the code should fall back to a built-in prompt.

Suggested default prompt:

```text
You are labeling a cluster of consumer feedback records for a supplement
direct-to-consumer brand.

Given representative records from one semantic cluster, produce:
1. A short topic label, 3-8 words.
2. A concise summary of the common customer issue or theme.
3. Key symptoms, phrases, or product references that justify the label.
4. Suggested tags.

Avoid overfitting to one record. Prefer labels that a support operations or
customer insights lead would understand in a dashboard. If the examples are
incoherent or weakly related, say so.
```

The service should send the top K representative records for a topic. The
representative selection should prefer high-confidence, central records and
include enough text for the model to understand the issue.

### Temporal Analysis After Semantic Clustering

Time should be a first-class analysis dimension, not part of the default
semantic vector.

Config should include:

```json
{
  "time": {
    "timestampField": "timestamp",
    "bucket": "week"
  }
}
```

Supported buckets should start with:

- day
- week
- month

For each cluster run, compute:

- Per-bucket record counts.
- Baseline count and rate.
- Delta versus baseline.
- Spike score or z-score.
- New topic detection.
- Vanishing topic detection.
- Top changed topics for a time window.

The default question strategy should be:

- Cluster the base `all_records` view first.
- Analyze trends locally.
- Recluster a time window only when the question requires local structure.

This avoids unstable topic IDs and makes longitudinal comparison cleaner.

### Views As First-Class Objects

Views should become persistent, agent-addressable objects. A view is a named
scope over a graph plus the runs that make that scope inspectable.

Examples:

- `all_records`
- `support_tickets_only`
- `shopify_reviews_sleep_gummies`
- `december_2025`
- `negative_reviews_last_90_days`

A view should be able to reference:

- included source types
- post-ingest scope filters
- optional time window
- embedding run
- cluster run
- layout run
- label run
- trend run

Agents should be able to list available views, inspect which runs back a view,
and ask questions against a selected view.

View filters are not a replacement for pre-embedding cleanup. They are a way to
define reusable analysis scopes over records that the agent already decided were
valid enough to upload.

Embeddings should be reused across views whenever the embedding text and model
are unchanged. Reclustering should happen on demand for a view when its scoped
population meaningfully differs from the base population or when the agent
explicitly requests it.

### Multi-Source Feedback In One Semantic Space

The service should support one graph containing multiple normalized source
types, such as support tickets, Shopify reviews, Amazon reviews, Google reviews,
Instagram comments, and Facebook comments.

Source fields should remain filterable and visible so a topic can show where its
evidence came from. For example, a topic might be 60 percent support tickets, 25
percent Amazon reviews, and 15 percent Instagram comments.

The service should not hard-code supplement-specific issue categories. If an
agent wants fields for adverse events, product efficacy, taste complaints,
shipping damage, subscription problems, or refund risk, it can include those as
custom fields or metadata.

### Privacy Boundary

The initial implementation can send customer text to OpenAI for embeddings and
labeling. The README should state this clearly and instruct agents to redact or
remove sensitive values before upload when needed.

This service should not silently promise PII or health-data redaction unless a
real redaction pipeline is implemented.

### Vector Indexing For 5k-100k Records

Use SQLite as the source of truth and add FAISS for local vector indexing.

Rationale:

- The intended scale is 5k-100k records.
- The app is local-first today.
- FAISS avoids a new service dependency.
- FAISS is well-suited for dense vector search and nearest-neighbor retrieval.
- SQLite can remain the durable metadata and run store.

Use normalized vectors and an inner-product index to approximate cosine
similarity. FAISS indexes should be rebuildable from SQLite records and
embeddings.

If the product later needs multi-user service deployment or remote filtering at
larger scale, Qdrant or Postgres with pgvector can be reconsidered.

## Proposed Storage Model

Keep existing graph concepts, but add normalized records, views, and
run-oriented tables.

### `records`

Stores normalized records uploaded by the agent.

Candidate fields:

- `id`
- `graph_id`
- `record_key`
- `normalized_json`
- `source_type`
- `source_name`
- `source_record_id`
- `record_url`
- `title`
- `customer_text`
- `timestamp_value`
- `metadata_json`
- `created_at`
- `updated_at`

### `views`

Stores named scopes that agents and humans can inspect.

Candidate fields:

- `id`
- `graph_id`
- `name`
- `description`
- `scope_json`
- `record_count`
- `default_embedding_run_id`
- `default_cluster_run_id`
- `default_layout_run_id`
- `default_trend_run_id`
- `created_at`
- `updated_at`

### `embedding_runs`

Tracks embedding configuration and lifecycle.

Candidate fields:

- `id`
- `graph_id`
- `provider`
- `model`
- `dimensions`
- `text_fields_json`
- `text_template`
- `record_count`
- `status`
- `created_at`
- `completed_at`

### `record_embeddings`

Stores one vector per record per embedding run.

Candidate fields:

- `embedding_run_id`
- `record_id`
- `text_hash`
- `embedding_json` or binary vector blob
- `created_at`

### `cluster_runs`

Tracks clustering configuration and output lifecycle.

Candidate fields:

- `id`
- `graph_id`
- `view_id`
- `embedding_run_id`
- `scope_json`
- `algorithm`
- `cluster_space_method`
- `cluster_params_json`
- `record_count`
- `cluster_count`
- `outlier_count`
- `status`
- `created_at`
- `completed_at`

The same embedding run can back many cluster runs. Cluster runs should generally
be associated with a view, either directly or by scope compatibility.

### `cluster_memberships`

Stores per-record cluster assignments for each run.

Candidate fields:

- `cluster_run_id`
- `record_id`
- `cluster_id`
- `probability`
- `outlier_score`
- `is_noise`

### `cluster_labels`

Stores LLM-generated labels and summaries.

Candidate fields:

- `id`
- `cluster_run_id`
- `cluster_id`
- `labeling_model`
- `prompt_hash`
- `top_k`
- `representative_record_ids_json`
- `label`
- `summary`
- `tags_json`
- `rationale_json`
- `created_at`

### `layout_runs`

Stores visualization coordinates for a cluster run.

Candidate fields:

- `id`
- `cluster_run_id`
- `method`
- `params_json`
- `status`
- `created_at`
- `completed_at`

### `layout_points`

Stores 2D coordinates.

Candidate fields:

- `layout_run_id`
- `record_id`
- `x`
- `y`

### `trend_runs`

Stores temporal analysis for a cluster run and optional scope.

Candidate fields:

- `id`
- `cluster_run_id`
- `timestamp_field`
- `bucket`
- `scope_json`
- `baseline_json`
- `results_json`
- `created_at`

### `agent_analysis_events`

Stores agent-triggered analysis evidence.

Candidate fields:

- `id`
- `graph_id`
- `view_id`
- `question`
- `run_refs_json`
- `evidence_json`
- `created_at`

## Proposed API Surface

Expose agent-native endpoints that create and reuse stored runs.

### Records

- `POST /api/data-graph/:id/records`
- `GET /api/data-graph/:id/records`
- `GET /api/data-graph/:id/records/:record_id`

### Embedding

- `POST /api/data-graph/:id/embeddings/build`
- `GET /api/data-graph/:id/embeddings/runs`
- `GET /api/data-graph/:id/embeddings/runs/:run_id`

### Views

- `POST /api/data-graph/:id/views`
- `GET /api/data-graph/:id/views`
- `GET /api/data-graph/:id/views/:view_id`
- `POST /api/data-graph/:id/views/:view_id/cluster`
- `POST /api/data-graph/:id/views/:view_id/label`
- `POST /api/data-graph/:id/views/:view_id/layout`
- `POST /api/data-graph/:id/views/:view_id/trends`
- `GET /api/data-graph/:id/views/:view_id/records`
- `GET /api/data-graph/:id/views/:view_id/topics`
- `GET /api/data-graph/:id/views/:view_id/outliers`
- `GET /api/data-graph/:id/views/:view_id/topics/top-records`
- `GET /api/data-graph/:id/views/:view_id/topics/:topic_id/top-records`

### Clustering

- `POST /api/data-graph/:id/cluster-runs`
- `GET /api/data-graph/:id/cluster-runs`
- `GET /api/data-graph/:id/cluster-runs/:run_id`
- `GET /api/data-graph/:id/cluster-runs/:run_id/topics`
- `GET /api/data-graph/:id/cluster-runs/:run_id/topics/:topic_id/evidence`

### Labeling

- `POST /api/data-graph/:id/cluster-runs/:run_id/label`
- `POST /api/data-graph/:id/cluster-runs/:run_id/topics/:topic_id/label`

### Layout

- `POST /api/data-graph/:id/cluster-runs/:run_id/layout`
- `GET /api/data-graph/:id/layout-runs/:layout_run_id/artifact`

### Trends

- `POST /api/data-graph/:id/cluster-runs/:run_id/trends`
- `GET /api/data-graph/:id/trend-runs/:trend_run_id`

### Analysis Evidence API

- `POST /api/data-graph/:id/analysis/evidence`

This should not be a generic opaque chat endpoint and should not try to be the
brand-aware reasoning layer. It should return a structured evidence payload for
the OpenClaw agent to reason over:

- referenced view IDs
- referenced cluster run IDs
- referenced trend run IDs
- topic IDs
- labels
- source mix
- outlier counts
- spike scores
- time buckets
- representative record IDs
- visualization URLs

Suggested request shape:

```json
{
  "viewId": "view_december_2025",
  "recipe": "surprising_topics",
  "timeRange": {
    "start": "2025-12-01T00:00:00Z",
    "end": "2026-01-01T00:00:00Z"
  },
  "topK": 10
}
```

Suggested response shape:

```json
{
  "viewId": "view_december_2025",
  "recipe": "surprising_topics",
  "evidence": [
    {
      "topicId": "3",
      "label": "Subscription cancellation errors",
      "spikeScore": 3.8,
      "sourceMix": {
        "support_ticket": 113,
        "amazon_review": 44
      },
      "representativeRecordIds": ["SUP-123", "SUP-456"]
    }
  ],
  "runRefs": {
    "clusterRunId": "cluster_...",
    "trendRunId": "trend_..."
  }
}
```

## Agent Question Flow

Example question:

```text
Was there a surprising topic in December 2025?
```

Recommended agent behavior:

1. Inspect graph config, available views, and timestamp field.
2. Select an existing view or create a December 2025 view.
3. Reuse the latest compatible embedding run or trigger one if missing.
4. Reuse the view's cluster run or trigger reclustering on demand.
5. Run or reuse trend analysis scoped to December 2025.
6. Label unlabeled high-signal topics.
7. If the base view clusters do not explain the December-only pattern, trigger a
   December-only cluster run.
8. Return structured evidence to the OpenClaw agent, including topic IDs,
   labels, top records, source mix, time buckets, and visualization links.

Default behavior should be to trend the selected view's semantic clusters first.
Time-window reclustering should be opt-in or question-driven.

## Artifact Shape Direction

The current artifact shape is too thin for topic intelligence. Future artifacts
should include run metadata, view metadata, and topic metadata.

Recommended top-level shape:

```json
{
  "config": {},
  "runs": {
    "viewId": "...",
    "embeddingRunId": "...",
    "clusterRunId": "...",
    "layoutRunId": "...",
    "trendRunId": "..."
  },
  "layout": {
    "method": "umap-2d",
    "params": {}
  },
  "topics": [
    {
      "clusterId": 0,
      "label": "Subscription cancellation errors",
      "summary": "Customers report errors when trying to cancel subscriptions.",
      "size": 184,
      "outlierCount": 0,
      "representativeRecordIds": ["SUP-123", "SUP-456"],
      "sourceMix": {
        "support_ticket": 113,
        "amazon_review": 44,
        "instagram_comment": 27
      },
      "trend": {
        "bucket": "week",
        "spikeScore": 3.8,
        "topBucket": "2025-12-08"
      }
    }
  ],
  "data": [
    {
      "x": 12.3,
      "y": -4.5,
      "clusterId": 0,
      "clusterProbability": 0.94,
      "outlierScore": 0.03
    }
  ]
}
```

## Implementation Phases

### Phase 1: Template, Config, And Validation

- Define and document the normalized JSON record template.
- Validate required record fields.
- Add explicit `cluster.embeddingTextFields`.
- Add optional `cluster.embeddingTextTemplate`.
- Flip default text feature method to embeddings or require embeddings for new
  graphs.
- Validate and reject unsupported `cluster.method` values.
- Add `labeling` config with model, top K, and prompt.
- Add `time` config with timestamp field and bucket.
- Document that extraction, pre-filtering, and normalization belong to OpenClaw
  agents.
- Document that customer text is sent to OpenAI unless agents redact before
  upload.

### Phase 2: Embedding Store

- Add embedding run tables.
- Persist embeddings separately from artifacts.
- Add FAISS index build/rebuild path.
- Add embedding build/reuse APIs.
- Keep existing embedding cache behavior only if it fits the new schema.

### Phase 3: Views And Clustering Runs

- Add persistent views.
- Add APIs to list, create, and inspect views.
- Treat view filters as post-ingest scopes, not as source cleanup.
- Split feature construction, clustering, and layout.
- Add HDBSCAN config and metadata persistence.
- Store membership probability and outlier score.
- Add base-view cluster run API.
- Add view-scoped cluster run API.

### Phase 4: LLM Labeling

- Add OpenAI labeling client using the same API key.
- Add default prompt and config override.
- Select representative records per topic.
- Persist labels and prompt/model metadata.
- Add topic relabel API.

### Phase 5: Temporal Analytics

- Add time bucket parsing and validation.
- Compute per-topic counts over time.
- Compute baseline and spike metrics.
- Add trend run API.
- Add topic evidence API for agent use.

### Phase 6: Agent-Native Analysis Evidence API

- Add structured evidence endpoint.
- Implement reusable evidence recipes such as:
  - surprising topics in a time period
  - new topics in a time period
  - fastest-rising topics
  - topic evidence lookup
  - compare two periods
- Persist analysis evidence.
- Keep narrative reasoning in the OpenClaw agent; this service returns evidence
  and optional lightweight summaries.

### Phase 7: UI Evolution

- Update visualization to consume run-based artifacts.
- Show topic labels, summaries, confidence, outliers, source mix, and trend
  indicators.
- Add time filter controls.
- Link topic details to representative records and evidence.
- Preserve current map/list modes where useful.

## Recommended Initial Build Target

The first meaningful milestone should be:

1. Document and validate a simple normalized JSON template for one feedback item
   per record.
2. Require the OpenClaw agent to pre-filter, normalize, and upload JSON.
3. Require explicit embedding text fields, with `customerText` as the common
   primary semantic field.
4. Run embedding-first processing with OpenAI embeddings.
5. Persist embeddings so they can be reused across views.
6. Add first-class views and APIs to list, create, inspect, and select them.
7. Create a base `all_records` view for all normalized records.
8. Cluster views on demand using HDBSCAN on the clustering space, not on 2D
   layout coordinates.
9. Generate LLM topic labels with `gpt-5.4-mini`.
10. Expose topic inspection APIs: topics, top K records per topic, top K records
    across topics, outliers, source mix, and representative evidence.
11. Add a run-based artifact that the existing UI can still render.

This milestone would preserve the current human visualization while creating the
backend needed for reliable agent-native analysis.

## Proposed Happy Path

The first implementation should optimize for this sequence:

1. An OpenClaw agent extracts records from a customer platform.
2. The agent removes non-customer-feedback records before upload.
3. The agent normalizes each included item into the JSON template.
4. The agent creates a graph with explicit embedding fields and optional custom
   metadata.
5. The service stores records and builds or reuses embeddings.
6. The service creates a default `all_records` view.
7. The agent triggers clustering for that view.
8. The agent triggers LLM labeling for that view.
9. The agent triggers a 2D layout artifact for local visualization.
10. The human opens the local visualization.
11. The agent lists views and topics through API calls.
12. The agent fetches top K representative records, outliers, and source mix.
13. If a new slice is needed, the agent creates a new view and triggers
    reclustering on demand.
14. For time questions, the agent asks for trend evidence against a view and
    only reclusters the time window if needed.

This keeps the codebase simple: it stores, embeds, clusters, labels, and returns
evidence. The OpenClaw agent owns source-specific cleanup and brand-specific
reasoning.

## Remaining Open Questions

- Should TF-IDF remain as a hidden local fallback, or be removed from the public
  config entirely?
- Should UMAP replace PaCMAP for the 2D layout, or should PaCMAP remain the UI
  layout method while UMAP is used only for clusterable reduction?
- What should the first supported timestamp formats be beyond ISO 8601 strings?
- Should view-scoped cluster runs inherit labels from the base `all_records` run
  when clusters are similar enough?
- Should topic lineage be computed by nearest centroid, overlap of member
  records, representative embedding similarity, or a combination?
- How strict should the normalized record template be in validation versus
  allowing sparse records with custom metadata?
