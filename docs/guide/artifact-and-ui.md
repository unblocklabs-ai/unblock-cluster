# Artifact Endpoint And UI

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
  "representation": "raw",
  "runRefs": {
    "embeddingRunId": "run_...",
    "clusterRunId": "run_...",
    "layoutRunId": "run_...",
    "labelRunId": "run_...",
    "trendRunId": "run_...",
    "summarizeRunId": "run_..."
  },
  "warnings": [],
  "layout": {"method": "umap", "params": {}},
  "noise": {"noiseCount": 12, "noiseRatio": 0.02},
  "topics": [],
  "data": []
}
```

`labelRunId`, `trendRunId`, and `summarizeRunId` appear in `runRefs` only when
the corresponding runs exist. `embedding.dimensions` reports the resolved
embedding run's recorded dimensions when available, falling back to the stored
graph config (whose default is `null` — the model's native 1536 applies). Topic entries include label, summary, coherence, size, probability,
source mix, representative record ids, and an optional trend snapshot. Record
entries include truncated `customerText`, source fields, timestamp, x/y
coordinates, cluster probability, outlier score, and noise status; records
imported as external vectors also carry a `provenance` object.

Artifact responses are served with gzip when the client advertises it (for
responses of at least 1 KB) and use `Cache-Control: no-cache` plus a strong
ETag. Repeating the same request with `If-None-Match` returns `304` until the
resolved run refs, the labels, or the view records change. Coordinates,
probabilities, and scores are rounded to 4 decimal places for transport only.

`warnings` is always present on artifact and topics responses. It is empty for
a healthy view. It names the label endpoint when the resolved cluster run has
no labels, and it calls out label/trend default mismatches when a view points
at a cluster run different from the one that produced the default labels or
trend snapshots. It also flags a default trend run whose persisted spike math
predates the current trend-math version, naming the trends endpoint to re-run;
that staleness warning is likewise the one returned on `GET .../trends`.

## UI Modes

The frontend supports:

- Map mode: OpenLayers point map, cluster colors, muted noise, outlier outlines,
  and low-probability de-emphasis.
- Topic panel: topics sorted by size by default (spike and name sorts
  available) with labels, summaries, source mix, coherence flags, and spike
  badges.
- Topic selection: highlights the topic's points (de-emphasizing the rest —
  separate from the filter state) and shows stored representatives.
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
the scale benchmark in the [operations notes](operations.md) to measure the
exact artifact/evidence/UI numbers on the target machine before a large
customer demo.
