# OpenClaw Pilot Brief — Data Graph v2

Purpose: first run against real brand data. The goal is not "does it work" (the pipeline is verified) but "which of our design assumptions breaks on real data." Execute the flow below, collect the artifacts listed, and report per section. Where something is awkward or wrong, capture the concrete example — one real payload beats a paragraph of description.

## The flow to execute

1. Export + normalize a real source (start with support tickets OR reviews, then add a second source type to the same graph).
2. Create graph (explicit `embedding.textFields`), upload in batches, embed, cluster, label, layout, trends (week bucket).
3. Ask 3–5 real questions the brand actually cares about via `POST /evidence`, verify the answers by reading the cited records.
4. Add an incremental batch (e.g. next day's records), re-embed, check freshness, decide whether to recluster.
5. Have the human open the vizUrl and try to answer one question from the UI alone.

## 1. Ingestion contract (highest uncertainty — designed from assumptions)

Report:
- Fields you could not map into the template, and what you put in `metadata` that deserves to be first-class.
- Every validation rejection: the payload fragment, the error message, and whether you could fix it FROM THE MESSAGE ALONE (that was a design goal — each miss is a bug report on the message).
- Timestamp formats in the wild; anything rejected.
- Ticket→record pre-aggregation: what you concatenated, what you dropped, and whether one-record-per-conversation felt right.
- Redaction burden: what you had to strip pre-upload, and how.
- Volume, source mix, batch count, upload wall-time.

## 2. Embedding + throttle under a real key

Report:
- Embed run stats JSON (requests, durations, reused counts).
- Any 429s/retries observed; the requestsPerMinute/maxConcurrency you used and your key's actual tier limits.
- Truncation: how many records exceeded 8k tokens (long tickets are the candidates).
- After the incremental batch: did reuse behave (only new texts embedded)?

## 3. Clustering quality on real language (the big one)

Report:
- clusterCount + noiseRatio + `effectiveHdbscan` from run stats, at the defaults FIRST. Then, if granularity looks wrong, override minClusterSize (try 2x and 0.5x) and report which felt right to a human.
- Verdict with examples: over-split (near-duplicate topics), under-split (mixed topics), or about right. Paste 2–3 example clusters each way.
- Read 20 random noise records: genuinely junk, or a lost topic?
- Read the top representatives for 5 clusters: do they represent?

## 4. Labels

Report:
- All labels, and a human verdict: which would a support-ops lead recognize instantly, which are wrong or too generic?
- Duplicate-sounding labels (the over-split smell).
- `coherent: false` occurrences — our first real-world data on whether the model actually flags junk clusters (untested in the ledger; no real junk cluster existed synthetically). If none fired, say whether any SHOULD have.

## 5. Evidence loop (the product's purpose)

Report per question asked:
- The question, the recipe(s) used, the response payload, and a human verdict after reading the cited representative records: credible / partly / wrong.
- Whether a known real event (launch, shipping issue, promo) surfaced in surprising/new/rising topics with the right time bucket.
- Any question you could NOT express with the five recipes — the exact question text (this drives the recipe roadmap).
- Count of times you hit a 409 and whether its message got you unstuck without human help. Total human interventions across the whole flow — the agent-native UX metric.
- The `analysis_events` table is your audit trail — export it and include it.

## 6. Ops, DX, and UI

Report:
- End-to-end wall time per stage; any failed/interrupted runs and their error_text quality.
- API confusions: anywhere you guessed wrong about a name, shape, or default (count your own dead-end calls).
- From the human on the viz: what they looked for first, whether the map or list answered it, what was missing.
- Device behavior during CPU runs (UMAP pegging a core is expected; anything worse isn't).

## Artifacts to attach

- Run stats JSON for every run (embed/cluster/label/layout/trend).
- Evidence request/response pairs + human verdicts.
- analysis_events export.
- 5–10 anonymized example records per problem area (mislabeled cluster, rejected upload, unrepresentative representative).
- The final graph config used (after any tuning).

## What we'll decide from this

- Whether the retuned minClusterSize default holds on real language, or the next move is the cached-reduction parameter sweep.
- Whether the record template needs new first-class fields.
- Which evidence recipes to add or reshape.
- Whether error messages are agent-sufficient (the 409/422 texts are part of the product surface).
- Whether coherent-flagging works on real junk.
