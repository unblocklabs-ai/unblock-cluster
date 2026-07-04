# Phase 12 `/goal` prompt (for gpt-5.5 / Codex)

Paste everything below as the `/goal`:

---

/goal Complete Phase 12 ("Scale validation and operational hardening") of `plan/build_plan.md`. This phase validates the bible's founding claim — "design for 100k records from day one" — which has never been executed (largest run to date: 5k synthetic, 3.5k real), and hardens operations for tunnel exposure. Build on the shipped Phases 0–11. No new Python or npm dependencies (a GitHub Actions workflow file is not a dependency).

Desired end state: CI protects main; a benchmark script runs the full offline pipeline at 100,000 records and emits a measured report; the measured budgets below are met (or a blocker is reported with numbers); the server has a read-only mode for tunnel serving; views and runs can be deleted with sane guards — all verified by a green suite plus one complete 100k benchmark run whose report appears in your final summary.

Deliverables:

1. CI: `.github/workflows/ci.yml` — on push/PR to main: install Python (3.14 via actions/setup-python; if unavailable on the runner fall back to 3.12 and say so) + Node LTS with dependency caching; run `ruff check .`, `pytest` (real-API tests already skip without OPENAI_API_KEY — assert the workflow sets no key), `node --test tests/*.test.mjs`, `npx knip`, `npm run build`. No network calls beyond package installs. You cannot execute Actions locally: verify every command line-for-line against what passes locally, keep the workflow dead simple, and flag it for owner observation on first merge.
2. `scripts/bench_scale.py` (manual tool like quality_eval, never run by pytest/CI): seeds N synthetic records (default 100000, `--size` accepts 5000/50000/100000) into a fresh data dir, runs the full offline pipeline — batched upload via the API, mock embeddings at 1536 dims, cluster, layout, scripted labels, trends — then measures and emits a JSON + printed report: per-stage wall time (cluster/layout phase durations included), upload throughput, artifact compose time cold and warm, artifact wire bytes (gzip), evidence recipe latency (surprising_topics and topic_evidence, each cold), peak RSS (resource.getrusage), and final DB file size. Deterministic (seeded); a `--no-seed` flag may be offered for unseeded-UMAP timing comparison but the reported run is seeded.
3. Measured budgets at 100k — meet them, or stop and report the measured number with analysis (do NOT silently relax):
   - Artifact: compose ≤ 5s cold, cache hit effectively instant, wire ≤ 12MB gzipped.
   - Evidence: ≤ 2s per call cold (the recipes load all memberships per request — optimize the query/computation if this misses, e.g. slimmer SQL projection; the endpoint stays synchronous and run-free).
   - Upload: 100 batches of 1000 complete without error; report throughput.
   - UI at 100k (headless, standing recipe, against the bench data dir): first meaningful render ≤ 15s on localhost, map shows the point cloud (spread predicate, not just pixels), list capped at 500 rows, topic selection responsive. Likely suspect: per-feature OL Feature construction — batch `addFeatures`, avoid per-record allocations in the loop. If a budget is provably unreachable without new dependencies (e.g. canvas rendering fundamentally cannot draw 100k features acceptably), stop and report with measurements and options.
   - Cluster/layout runtime at 100k: NO gate — report seeded wall time honestly (UMAP dominates; the bible predicts minutes) and update the README's runs section with realistic expectations at scale.
4. Read-only mode: `DATAGRAPH_READ_ONLY=1` (settings-driven). When on: every mutating endpoint — record upsert, graph/view create/patch/delete, all run-creating POSTs, run cancel, run/view delete — returns 403 with a single clear message; all GETs, static serving, and `POST /evidence` remain available (evidence writes only its audit row — document this judgment call in the README). Implement centrally (middleware or shared dependency), not per-route copy-paste. Tests cover representative blocked + allowed endpoints, both modes.
5. Lifecycle deletion:
   - `DELETE /api/graphs/:gid/views/:vid` — deletes the view AND its view-scoped runs (their outputs cascade via the existing FKs); records and embeddings are never touched (embedding runs are graph-level and embedding_vectors are content-addressed — state this in the README). Deleting the auto-created `all_records` view → 409.
   - `DELETE /api/graphs/:gid/runs/:runId` — terminal runs only (409 for queued/running with cancel guidance); 409 if the run is any view's default_*_run_id (repoint or delete the view first — name it in the message); outputs cascade. Embedding runs additionally 409 if any view's default_embedding_run_id references them.
   - Both respect read-only mode. README documents the tuning-hygiene loop: experiment with setDefault:false, promote the winner, DELETE the losers.

Verified by — run all of these; do not claim completion from belief:

- `.venv/bin/pytest` green including new tests: read-only matrix (blocked mutations 403 with message, GETs + evidence still working, mode off = unchanged); view deletion (view + its runs + outputs gone, records/embeddings intact, all_records 409); run deletion (outputs cascaded, default-run 409 naming the view, running-run 409, embedding-run guard); bench script importable with a tiny smoke path (e.g. `--size 5000` exercised in a test-marked-slow or via direct function call — full 100k stays manual).
- ONE complete seeded 100k benchmark run executed on this machine, its report table pasted in your final summary, and every budget above either met (with the number) or reported as a blocker (with the number). Include the 5k bench for comparison.
- Headless UI verification at 100k per the budget above, screenshots described (spread, panels, list cap) not just saved.
- `ruff check .`, `npx knip`, `npm run build`, `npm run check` green; suite runtime reported (~5 min budget).

While preserving: all Phase 0–11 tests green; artifact/API shapes unchanged except the new DELETE endpoints and 403s; `plan/` untouched; CI workflow makes no OpenAI calls; the bench uses mock embeddings only; no new dependencies; UI behavior unchanged below 100k except where the rendering fix is a pure win.

Between iterations: run pytest, node tests, and ruff after each meaningful change and let failures pick the next action; keep a running list of decisions or deviations and include it in the final summary.

If blocked — a budget is unreachable within the stack (report measurements + options), hdbscan/umap wheels fail on the CI runner, or SQLite behavior at 100k surprises (lock contention, file size) — stop and report the exact blocker, what was attempted, the evidence gathered, and the decision needed. Do not relax budgets silently, skip the real 100k run, add dependencies, or let the bench sneak into CI.
