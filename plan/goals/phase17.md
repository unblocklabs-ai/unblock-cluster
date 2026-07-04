# Phase 17 `/goal` prompt (for gpt-5.5 / Codex)

Paste everything below as the `/goal`:

---

/goal Complete Phase 17 ("Test consolidation and speed") — reorganize the pytest suite by domain and cut its runtime from the measured 343s to the budgets below WITHOUT losing coverage. Measured baseline (pytest --durations): test_phase14's summarize-flow test alone is 103s (five summarize runs over 5,000 records each); the read-side tests in phases 7/8/10/11/13 spend ~90s combined rebuilding the identical upload→embed→cluster→label→trend pipeline six times; the phase-3 planted-structure ARI gate (24s, real UMAP) is load-bearing and stays. One dev-only dependency is explicitly allowed: pytest-xdist. Nothing else changes in runtime code except where a test seam genuinely requires it (report any such change).

Desired end state: tests organized by domain instead of by phase; full suite ≤ 120s single-process and ≤ 75s with `-n auto` on this machine; a `-m "not slow"` fast tier ≤ 45s for inner-loop iteration; CI runs the full suite parallel; and a coverage-preservation protocol proves nothing was lost.

Deliverables:

1. Reorganize `tests/test_phase0.py..test_phase15.py` into domain files (indicative, adjust sensibly): `test_records_views.py`, `test_runs_executor.py`, `test_embedding.py`, `test_clustering_layout.py`, `test_labeling.py`, `test_trends.py`, `test_evidence.py`, `test_artifact_serving.py` (artifact/gzip/ETag/HEAD/warnings/static), `test_summarize.py`, `test_ops.py` (read-only, deletion, config forward-compat, bench smoke), `test_real_api.py` (all opt-in real-key tests), plus existing conftest helpers consolidated. Keep the node test files as they are.
2. Shared expensive fixtures: ONE session-scoped "built graph" (planted synthetic slice ~500 records, structured mock providers, cluster space "none", embedded + clustered + labeled + trended, its own session tmp data dir and TestClient) consumed by every READ-ONLY test (artifact shape, gzip/ETag, HEAD, warnings-free baseline, topics/facets/evidence shapes, trends reads). Tests that MUTATE state (setDefault promotion, deletion, cancellation, re-embed, warnings-triggering tuning runs) keep isolated per-test graphs — correctness beats speed there. Document the rule at the top of conftest.
3. Population discipline: default planted populations 300–600 records. The summarize-flow test drops from 5,000 to ~500 (all its assertions — junk counts on 3 planted records, reuse counters, promptHash changes — are population-independent). The phase-3 planted-structure ARI ≥ 0.8 gate keeps its real-UMAP fit; its population may be reduced from 2,000 ONLY if the gate still passes with visible margin (report the ARI at the size you choose); do not touch the gate threshold.
4. Speed mechanics: poll intervals tightened (0.01s); `pytest-xdist` added as a DEV-ONLY dependency (comment it as such in requirements.txt) with per-test tmp dirs keeping workers isolated; `@pytest.mark.slow` on the ARI gate, the bench smoke, and any test still > 8s — the default run includes them (full coverage stays the default), and README's checks section documents `-m "not slow"` as the iteration tier with its measured time.
5. CI: pytest step becomes `-n auto`; everything else unchanged.
6. Coverage-preservation protocol (the heart of the phase — speed without this is a regression):
   - A mapping table in your final summary: every test function from the old phase files → its new home (merged targets listed explicitly). No orphans.
   - These named gates survive verbatim (assert their presence before deleting old files): planted-structure ARI ≥ 0.8; the dist-stomp regression (real dist untouched by the static test); the config grep-guard; the pilot warnings regression (labeled view + tuning run → three warnings); the stale-ETag label-freshness regression; the camelCase run key-set checks; the receipts assertion (raw text under summary representation); HEAD semantics; the read-only matrix; deletion guards; legacy-config forward-compat trio; tokenUsage summation; every zero-provider-call reuse assertion.
   - Any assertion intentionally dropped is listed with justification. Deduplicating a repeated setup is fine; deduplicating an assertion is a decision to be visible.
7. Report before/after: total runtime single-process and `-n auto`, the new --durations top 10, and the fast-tier time.

Verified by — run all of these; do not claim completion from belief:

- Full suite green single-process AND with `-n auto`; measured times meet the budgets (≤120s / ≤75s / fast tier ≤45s) or you report the measured number with analysis rather than silently missing.
- The mapping table and named-gate checklist complete in the summary.
- Real-key opt-in tests still collected and skipped without a key (they moved, not vanished).
- `ruff check .`, `npx knip`, `npm run build`, `npm run check` green (npm run check inherits the faster suite).

While preserving: zero runtime-code behavior changes (test seams only, reported); all named gates; scripts (quality_eval, bench_scale, demo_seed) untouched; `plan/` untouched; CI's no-key assertion intact; node tests untouched.

Between iterations: run the fast tier while iterating, the full suite before claiming completion; keep a running list of decisions and include the mapping table in the final summary.

If blocked — xdist and the session fixture fight (worker-scoped session fixtures), the ARI gate fails below 2,000 records, or a consolidation would force dropping a named gate — stop and report with numbers. Do not lower any gate threshold, skip tests by default, or trade an assertion for a millisecond silently.
