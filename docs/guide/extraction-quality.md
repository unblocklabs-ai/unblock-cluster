# Extraction Quality

## Pre-Upload Filtering

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

## What The Input Does To The Output

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

### Reading Your Noise

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

## Iterating On Quality

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
cancelled or finish first, and embedding runs created by an external vector
import are immutable and cannot be deleted. Use these deletes to remove failed
experiments after the tuning loop settles.

## Decomposing A Large Topic

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

# Poll until the focus run succeeds before labeling against it.
until [ "$(curl -sS "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/runs/$FOCUS_RUN_ID" | jq -r .status)" = succeeded ]; do sleep 1; done

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
