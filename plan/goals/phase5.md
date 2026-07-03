# Phase 5 `/goal` prompt (for gpt-5.5 / Codex)

Paste everything below as the `/goal`:

---

/goal Complete Phase 5 ("Trend runs") of `plan/build_plan.md`. That document remains the authoritative spec — treat its "Pipeline Design → Trend run", "Storage Schema" (trend_results, trend_summaries), "Graph Config" (time section), and "Phase 5" sections as binding. Build on Phases 0–4.

Desired end state: a view's cluster run can be analyzed over time as a `trend` run — zero-filled per-topic bucket series with counts, shares, and variance-floored spike scores persisted, plus a window-driven summary (surprising/new/vanishing/rising/falling topics) — merged as a trend snapshot into the topics APIs, and proven by deterministic tests that recover the synthetic dataset's three planted temporal patterns with correct bucket attribution.

Deliverables:

0. Phase 4 carry-in fixes (small): (a) a label run cancelled before any cluster is labeled must end `cancelled`, not `failed` — guard the `labeled == 0` raise with `not cancel_event.is_set()`, and add a test; (b) `clusterIds: []` on POST /label → 422 at POST time (an explicit empty list is a caller error), with a test; (c) move the duplicated `_new_id`/`_now_iso` helpers from `executor.py`/`label.py` into one shared module (e.g. `datagraph/core/ids.py`) and import from both.
1. `datagraph/core/trend_math.py` as PURE functions over plain data (no DB, no I/O) — this module is the deterministic heart of the phase and must be exhaustively unit-tested:
   - `bucket_start(timestamp_ms, bucket) -> str`: UTC bucketing; `day` → `YYYY-MM-DD`, `week` → the Monday of the ISO week (`YYYY-MM-DD`), `month` → `YYYY-MM-01`.
   - Series building: given (cluster_id, timestamp_ms) pairs, produce per-cluster ZERO-FILLED bucket series spanning the population's full time range (min bucket → max bucket, no gaps), plus per-bucket population totals (including noise records — totals are the volume denominator).
   - `share` = cluster count / bucket total (0 when total is 0).
   - Baseline per (cluster, bucket): mean and population std over the 8 prior buckets of the zero-filled series (fewer at series start; empty prior → mean 0, std 0).
   - `spike_score` = `(count − mean) / max(std, sqrt(mean), 1)` — the plan's variance-floored z-score, exactly.
   - Window summary, given a window (start/end bucket, inclusive): `surprisingTopics` = topics ranked by max spike_score within the window; `newTopics` = first-ever nonzero bucket falls within the window AND is not the very first bucket of the whole series AND window count ≥ 5; `vanishingTopics` = zero count in the window AND healthy pre-window baseline (mean ≥ 1.0 over the 8 buckets before the window); `risingTopics`/`fallingTopics` = ranked by delta of mean share, window vs the 8 buckets before it. Sections that need a pre-window baseline are empty when the window starts at the series start — document this in the README.
2. The `trend` run type (IO kind — it's SQL + arithmetic, no process pool needed): `POST /api/graphs/:gid/views/:vid/trends` with optional body `{clusterRunId?, window?: {start, end}, time?: {bucket?}}`. Resolution at POST time: cluster run defaults to the view's `default_cluster_run_id`, none → 409 naming the cluster endpoint; `time` merged onto graph config and re-validated (`bucket` ∈ day|week|month; `timestampField` must be `"timestamp"` for now — anything else 422); window timestamps ISO-parsed with the existing timestamp rules, `start` ≤ `end`, defaults to the full series span. Execution: load the cluster run's memberships joined to record timestamps; compute everything via `trend_math`; persist one `trend_results` row per (non-noise cluster, bucket) — `(run_id, cluster_id, bucket_start, count, share, spike_score)` — and one `trend_summaries` row with the full summary JSON (window echo, bucket, per-section topic lists with their scores/buckets). Stats: population, bucket, bucketCount, clusterCount, window echo. `input_refs_json`: `{clusterRunId, viewId}`. On success set the view's `default_trend_run_id`.
3. Trend reads: `GET /api/graphs/:gid/views/:vid/trends` (resolves the view's default trend run, `?trendRunId=` override; 409 with actionable message when none) returning `{trendRunId, clusterRunId, bucket, window, summary, series: [{clusterId, label (the topic's newest label text or null), buckets: [{bucketStart, count, share, spikeScore}]}]}`.
4. Trend snapshot merged into `GET .../topics` and `GET .../topics/:tid`: each topic gains `trend: {bucket, spikeScore, topBucket} | null` from the view's default trend run (when its cluster run matches the resolved one) — `spikeScore` = the topic's max spike score within the run's window, `topBucket` = the bucket_start where that max occurs. This is the artifact shape the plan specifies.
5. `scripts/quality_eval.py` extended behind the same key gate: after clustering and labeling, run trends with a December-2025 window and print the surprising-topics ranking (the deferred real-key acceptance will check the planted spike ranks #1).

Verified by — run all of these; do not claim completion from belief:

- `.venv/bin/pytest` green, including at minimum:
  - trend_math unit tests with hand-built fixtures asserting EXACT values: day/week-Monday/month bucketing across a month boundary and a year boundary; zero-fill including leading/trailing gaps; baseline mean/std over fewer-than-8 prior buckets; the spike formula against hand-computed numbers (at least one case where each of std, sqrt(mean), and 1 is the active floor); new-topic edge cases (first bucket of series excluded; window count < 5 excluded); vanishing (healthy baseline vs never-existed topic NOT flagged); rising/falling deltas.
  - The planted-pattern integration test (the phase's flagship): upload a slice of the synthetic 5k (~2,500 records, enough for pattern density), embed with the structured mock provider, cluster, then trend with bucket=week. With a December-2025 window: the cluster corresponding to `december_energy_crash_spike` (identified via ground-truth majority membership) ranks #1 in surprisingTopics with its topBucket in December 2025; `november_creatine_questions`'s cluster appears in newTopics. With a July–December window: `midyear_vanishing_packaging`'s cluster appears in vanishingTopics. All assertions against ground-truth-mapped clusters, not eyeballed.
  - trend_results integrity: rows only for non-noise clusters, zero-filled (every cluster has a row for every bucket in the span), shares in [0,1], per-bucket topic counts sum ≤ bucket total.
  - Topics API trend snapshot populated after the trend run (and null before), with correct spikeScore/topBucket for the spike topic; `default_trend_run_id` set; GET trends shape including series labels; 409s (no cluster run for POST, no trend run for GET); window validation (bad ISO → 422, start > end → 422, unsupported bucket → 422, `timestampField` ≠ "timestamp" → 422); determinism — re-running the same trend run yields identical trend_results rows.
  - The three Phase 4 carry-in fixes, each with its test.
- Suite runtime still under ~4 minutes; report the measured time.
- `ruff check .` clean.
- `npm run check` passes.

While preserving: all Phase 0–4 tests still green; `src/` and frontend files byte-identical; `plan/` untouched; no new dependencies; CI makes no network calls; no future-phase implementation (no evidence recipes endpoint, no analysis_events writes, no artifact endpoint). Schema changes should not be needed; if a real gap surfaces, edit `001_initial.sql` in place and say so in the final summary.

Between iterations: run pytest and ruff after each meaningful change and let failures pick the next action; keep a running list of any decision or deviation from `plan/build_plan.md` and include it in the final summary instead of silently diverging.

If blocked — the plan's trend semantics are ambiguous beyond what this goal pins down, the planted patterns can't be recovered at ~2,500 records, or a verification step cannot be run — stop and report the exact blocker, what was attempted, the evidence gathered, and the specific decision or input that would unlock progress. Do not change the spike formula, the bucketing rules, or the window-summary definitions unilaterally.
