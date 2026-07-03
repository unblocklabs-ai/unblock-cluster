# Phase 7 `/goal` prompt (for gpt-5.5 / Codex)

Paste everything below as the `/goal`:

---

/goal Complete Phase 7 ("UI port and cleanup") of `plan/build_plan.md` — the final phase. That document remains the authoritative spec — treat its "Artifact Shape Direction" heritage (via the API Surface artifact endpoint), "Phase 7", and the vizUrl format from Phase 6 as binding. THE FRONTEND FREEZE IS LIFTED for this phase only: `src/`, `index.html`, `vite.config.js`, and the node tests may now be modified or rewritten. Keep the existing stack (Vite + OpenLayers + vanilla JS modules); add no new frameworks or runtime npm dependencies.

Desired end state: the Phase 6 vizUrl is live — opening `http://127.0.0.1:{port}/?graphId=…&viewId=…` serves the built UI from the backend and renders that view's run-based artifact as an interactive topic map/list with labels, summaries, source mix, trend badges, outlier styling, and a time filter; a seed script makes this demonstrable offline in one command; all backend tests and rewritten frontend tests green.

Deliverables:

1. `GET /api/graphs/:gid/views/:vid/artifact` — the composed visualization payload from the view's default runs. Requires a succeeded default cluster run AND layout run (409 with the actionable endpoint name otherwise); labels and trend are optional-null when absent. Shape (top level): `{graphId, viewId, config: {embedding model/dimensions summary}, runRefs: {embeddingRunId, clusterRunId, layoutRunId, labelRunId?, trendRunId?}, layout: {method, params}, noise: {noiseCount, noiseRatio}, topics: [...], data: [...]}`. Each topic: `{clusterId, label, summary, coherent, size, meanProbability, sourceMix, representativeRecordIds, trend: {bucket, spikeScore, topBucket} | null}` (label fields null when unlabeled — same merge rules as the topics API). Each data row (the intersection of layout points and memberships): `{id, recordId, title, customerText (truncated to 300 chars), sourceType, sourceName, product, sentiment, rating, tags, timestamp, recordUrl, x, y, clusterId, clusterProbability, outlierScore, isNoise}` — truncation keeps a 100k artifact bounded; full text stays behind the records API. Pytest coverage: shape, data count == layout population, topics carry labels + trend snapshots when present, 409 matrix, truncation.
2. Backend serves the built frontend: mount `dist/` statically so `/` (with query params) serves the app from the same origin as the API — this makes the Phase 6 vizUrl real. `npm run dev` (Vite) keeps working against the backend via dev proxy for `/api`. Document both modes in the README.
3. UI adaptation (rewrite `src/app.js` and friends as needed — a clean rewrite is acceptable and probably right; keep `src/` layout, OpenLayers map, and the map/list mode duo):
   - On load: read `graphId`/`viewId` from query params and fetch the artifact; with no params, fetch `/api/graphs` and render a simple graph/view picker.
   - Map mode: points positioned by x/y, colored by cluster, noise/outliers visually distinct (e.g. muted color, outline scaled by outlierScore), low-probability points de-emphasized.
   - Topic panel: topics sorted by size with label, summary, size, mean probability, source-mix breakdown, `coherent: false` flagged visibly, and trend badges — a spike badge (with topBucket) when `trend.spikeScore` is high, driven by the artifact's trend snapshot.
   - Selecting a topic highlights its points and shows its representative records (from `representativeRecordIds` resolved against the artifact's data rows; `recordUrl` rendered as a link).
   - Time filter: a client-side range control over `timestamp` that filters visible points in both modes.
   - List mode: filterable record table (search over title/customerText, filter by topic and sourceType), preserved from the old app's spirit.
   - Errors surfaced honestly: 409 from the artifact endpoint renders the actionable message (which run to trigger), not a blank screen.
4. Frontend tests: rewrite `tests/*.test.mjs` for the new logic — artifact→view-state transformation, time-filter behavior, topic selection/highlight state, URL param parsing (keep `urlPolicy` semantics for external links). Delete tests for removed behavior; `node --check` targets and `npm run check` updated accordingly.
5. `scripts/demo_seed.py`: one command that boots against a fresh data dir and runs the full offline pipeline on the synthetic 5k — structured topic-keyed mock embeddings (reuse the test provider pattern), cluster, scripted labels, week trends, layout — then prints the vizUrl to open. This is how a human (and the reviewer) eyeballs the result without an API key.
6. Cleanup, per the plan's clean-break intent: delete dead frontend code paths from the old artifact format, rebuild `dist/`, and give the README its final pass — quickstart (venv, serve, seed, open), the agent contract, privacy statement, run model, recipes, and both UI modes.

Verified by — run all of these; do not claim completion from belief:

- `.venv/bin/pytest` green including the new artifact endpoint tests; all Phase 0–6 tests still green; suite under ~4 minutes.
- `npm run check` green with the rewritten frontend tests; `npm run build` succeeds and the backend serves the built app at `/` with `GET /` returning 200.
- End-to-end demo: `python scripts/demo_seed.py` completes offline, prints a vizUrl; `curl` of the artifact endpoint for the seeded view returns topics with non-null labels and trend snapshots including the planted December spike topic.
- `ruff check .` clean.
- Report anything that could not be verified headlessly (visual rendering quality) as an explicit list for human review — do not claim visual acceptance yourself.

While preserving: `plan/` untouched; no new Python or npm runtime dependencies; CI makes no network calls; the artifact endpoint creates no runs. Schema changes should not be needed; if a real gap surfaces, edit `001_initial.sql` in place and say so in the final summary.

Between iterations: run pytest, ruff, and the node tests after each meaningful change and let failures pick the next action; keep a running list of decisions or deviations from `plan/build_plan.md` and include them in the final summary instead of silently diverging.

If blocked — the OpenLayers rendering approach fights the new artifact shape, static serving conflicts with the API routes, or the plan is ambiguous on something Phase 7 needs — stop and report the exact blocker, what was attempted, the evidence gathered, and the specific decision or input that would unlock progress. Do not add frameworks, change the artifact contract to fit the old UI, or ship a UI that silently hides labels, trends, or outliers when data is missing.
