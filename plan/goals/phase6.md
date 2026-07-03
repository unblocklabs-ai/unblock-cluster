# Phase 6 `/goal` prompt (for gpt-5.5 / Codex)

Paste everything below as the `/goal`:

---

/goal Complete Phase 6 ("Evidence recipes") of `plan/build_plan.md`. That document remains the authoritative spec — treat its "Pipeline Design → Evidence recipes", "Storage Schema" (analysis_events), and "Phase 6" sections as binding. Build on Phases 0–5. This phase is the product's purpose: an OpenClaw agent must be able to answer "Was there a surprising topic in December 2025?" with one synchronous REST call over already-persisted runs.

Desired end state: `POST /api/graphs/:gid/evidence` serves five recipes as synchronous, deterministic reads over persisted runs — no new run creation, no LLM calls, actionable 409s when prerequisite runs are missing — every response carrying runRefs, labels, source mix, representative ids, and a visualization URL, with every call persisted to `analysis_events`; proven end-to-end offline against the planted synthetic patterns.

Deliverables:

0. Carry-in: delete the dead stub `datagraph/runs/trends.py` (the implementation lives in `trend.py`).
1. `POST /api/graphs/:gid/evidence` with body `{viewId, recipe, timeRange?: {start, end}, periods?: {a: {start, end}, b: {start, end}}, topicId?, topK?}` (`topK` default 10, max 50). Validation: unknown body keys → 422; unknown recipe → 422 whose message lists the valid recipes; `topicId` required for `topic_evidence` (422 otherwise); `periods` (both `a` and `b`) required for `compare_periods`; timestamps parsed with the existing rules. Resolution: the view's `default_cluster_run_id` (none → 409 naming the cluster endpoint); temporal recipes additionally require the view's `default_trend_run_id` with matching cluster run (none/mismatch → 409 naming the trends endpoint). Labels resolve newest-per-(cluster run, cluster id) as everywhere else.
2. Recipe semantics — temporal recipes REUSE `trend_math` (pure functions) over the default cluster run's persisted memberships, with the request's window and the trend run's bucket, computed in-request (milliseconds at this scale; deterministic from persisted data — this is still "reads over persisted runs", the trend run anchors bucket choice and appears in runRefs):
   - `surprising_topics`: topics ranked by max spike score within `timeRange` (defaults to the trend run's window, else full span), topK.
   - `new_topics`, `vanishing_topics`, `rising_topics`: the corresponding summary sections for `timeRange`, topK.
   - `topic_evidence`: one topic — newest label object (or null), size, meanProbability, sourceMix, representatives as record payloads (id, recordId, sourceType, title, customerText, recordUrl, timestamp), and the topic's persisted trend series + spike snapshot when a trend run exists (null otherwise — this recipe must work without one).
   - `compare_periods`: per topic, count and mean share in period `a` vs period `b` with deltas, ranked by |share delta|, topK; requires the trend run for bucket choice.
3. Every evidence response carries: `viewId`, `recipe`, `evidence` (recipe-shaped list/object where every topic entry includes clusterId, label text when labeled, sourceMix, representativeRecordIds), `runRefs` `{embeddingRunId, clusterRunId, labelRunId?, trendRunId?}`, `freshness` `{clusterRunCreatedAt, recordsAddedSinceClusterRun}` (count of view-scope records with `created_at` after the cluster run's `created_at` — the agent's staleness signal), and `vizUrl` (format `http://127.0.0.1:{port}/?graphId={gid}&viewId={vid}` from settings — Phase 7 makes it live; define the format now and note it in the README).
4. Every call (including for each recipe) inserts one `analysis_events` row: id, graph_id, view_id, recipe, params_json (request echo), run_refs_json, evidence_json, created_at. No read API for events this phase (the table is the audit trail).
5. README: document the five recipes with request/response examples, the freshness field, and the agent question flow from the plan ("Agent Question Flow" section made concrete with curl).

Verified by — run all of these; do not claim completion from belief:

- `.venv/bin/pytest` green, including at minimum, on the planted-pattern fixture (structured mock embeddings → cluster → scripted labels → trend, reusing the Phase 5 test scaffolding):
  - `surprising_topics` with a December-2025 timeRange → the planted spike topic ranks #1 (ground-truth-mapped), its entry carries the scripted label text, a sourceMix matching an independent tally, non-empty representativeRecordIds, and complete runRefs.
  - `new_topics` (Nov–Dec) → november topic present; `vanishing_topics` (Jul–Dec) → mid-year topic present; `rising_topics` → shape + consistency with the trends summary for the same window.
  - `topic_evidence` for the spike topic → label object, ≤ topK representatives with full text, non-empty trend series with its max-spike bucket in December; and with a fresh view that has a cluster run but NO trend run → trend section null, rest intact.
  - `compare_periods` H1-2025 vs H2-2025 → the vanishing topic shows a strongly negative share delta; the spike topic a positive one.
  - Determinism: the same evidence request twice returns identical payloads (excluding event ids/timestamps).
  - `analysis_events`: one row per call with correct recipe, params echo, and runRefs; accumulates across calls.
  - `freshness`: add records after the cluster run → `recordsAddedSinceClusterRun` reflects exactly those in view scope.
  - Validation/409 matrix: unknown recipe 422 listing recipes; missing topicId/periods 422; no cluster run 409 naming cluster endpoint; temporal recipe with no trend run 409 naming trends endpoint; timeRange ISO/order errors 422.
  - The deleted stub stays deleted (no import references).
- Suite runtime still under ~4 minutes; report the measured time.
- `ruff check .` clean.
- `npm run check` passes.

While preserving: all Phase 0–5 tests still green; `src/` and frontend files byte-identical; `plan/` untouched; no new dependencies; CI makes no network calls; the evidence endpoint creates NO runs and makes NO provider calls; no Phase 7 implementation (no artifact endpoint changes, no UI work — defining the vizUrl string format is in scope, serving it is not). Schema changes should not be needed; if a real gap surfaces, edit `001_initial.sql` in place and say so in the final summary.

Between iterations: run pytest and ruff after each meaningful change and let failures pick the next action; keep a running list of any decision or deviation from `plan/build_plan.md` and include it in the final summary instead of silently diverging.

If blocked — recipe semantics are ambiguous beyond what this goal pins down, or in-request trend_math computation proves too slow at realistic scale — stop and report the exact blocker, what was attempted, the evidence gathered, and the specific decision or input that would unlock progress. Do not turn the evidence endpoint into a run, add narrative/LLM summarization to it, or invent recipes beyond the five.
