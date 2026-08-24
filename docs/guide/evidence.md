# Evidence Recipes

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
- `new_topics`: topics whose first nonzero bucket falls in the window. The
  first nonzero bucket must not be the opening bucket of the overall series,
  and the topic needs at least 5 records inside the window.
- `vanishing_topics`: topics with zero window count after a healthy baseline.
- `rising_topics`: topics ranked by positive mean-share delta.
- `topic_evidence`: one topic with label object, source mix, representatives,
  and persisted trend series when present. Accepts `facetBy` — see the allowed
  values in [Extraction Quality](extraction-quality.md).
- `topic_search`: embed a natural-language `question` once and rank topics by
  cosine similarity to per-topic centroids in the resolved embedding space.
  Centroids are the mean of each topic's stored representative vectors,
  falling back to all member vectors when representative vectors are missing.
  Default `topK` is 5, max is 20. If the embedding run used
  `representation: "summary"`, the question is still embedded as-is with the
  same model; it is matched against whatever representation built that space.
  Embedding runs imported as external vectors have no provider to embed the
  question and return 422 for question recipes.
- `question_evidence`: run `topic_search`, take the top match above the
  exposed similarity floor, and return full `topic_evidence` plus runner-up
  topics. If nothing clears the floor, the endpoint returns 422 with the
  closest candidates so the agent can decide how to rephrase.
- `compare_periods`: topics ranked by absolute share delta between two windows.

Baseline-dependent sections (`vanishing_topics` and `rising_topics`) compare
the window against up to 8 buckets immediately before it (a single prior
bucket is enough to activate the baseline), so they are empty only when the
window starts at the very beginning of the data's time span — narrow the
window to enable them. `surprising_topics` and `new_topics` are not baseline-gated.

Spike scores are gated for integrity: the first 3 buckets of the overall
zero-filled series always carry `spikeScore: 0`, because they do not have enough
history. Late-emerging topics still score their first burst after that point
against the prior zero-filled baseline.

Persisted trend runs are snapshots of the trend math at run time. After
upgrading the service, re-run trends to recompute persisted scores; the UI
warns when a default trend run predates the current math, and
`scripts/rerun_pipeline.py` performs the rerun in one command.

Every successful evidence response includes `runRefs`, `freshness`, and
`vizUrl`, and inserts one `analysis_events` audit row. The `question_evidence`
no-match 422 also records an audit row.
