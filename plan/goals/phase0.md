# Phase 0 `/goal` prompt (for gpt-5.5 / Codex)

Paste everything below as the `/goal`:

---

/goal Complete Phase 0 ("Scaffold and clean break") of `plan/build_plan.md`. That document is the authoritative spec — read it fully before writing code, and treat its "Package Layout", "Storage Schema", "Graph Config", and "Phase 0" sections as binding.

Desired end state: the legacy Python backend is deleted; a new `datagraph/` FastAPI package boots on a fresh data directory with the complete initial SQLite schema applied via a versioned migration runner; a uniform run executor moves a no-op run through its lifecycle observably via the REST polling API; the deterministic synthetic dataset generator and mock embedding provider exist — all verified by a green pytest suite.

Deliverables:

1. Clean break. Delete: `server.py`, `processor.py`, `scripts/data_graph_cli.py`, `scripts/data_graph_export.py`, `scripts/data_graph_import.py`, `scripts/data_graph_token.py`, `scripts/data_graph_service.sh`, `scripts/test_parallel_ingest.sh`, `tests/test_processor_embeddings.py`, `tests/test_server_regressions.py`, `tests/test_import_scripts.py`, `local-data/`, `sample-data/`, `sample-manifest.json`. Do NOT touch `src/`, `index.html`, `vite.config.js`, `dist/`, or `tests/*.test.mjs` — the frontend is frozen until Phase 7. Update `package.json`'s `check` script and the README's setup/run instructions so nothing references deleted files.
2. `requirements.txt`: fastapi, uvicorn[standard], numpy, scikit-learn, umap-learn, hdbscan, openai, tiktoken, pytest (ruff stays working as the linter). umap-learn and hdbscan are unused until Phase 3 but are installed now deliberately to surface native/numba build problems early — verify both import successfully.
3. `datagraph/` package exactly per the plan's Package Layout: `main.py` (app factory, startup hooks: run migrations + run recovery; uvicorn entry), `settings.py` (env config: data dir, port; `OPENAI_API_KEY` optional in Phase 0), `db.py` (sqlite connections, WAL mode, foreign keys on, `PRAGMA user_version` migration runner), `migrations/001_initial.sql` containing the COMPLETE Storage Schema from the plan (all tables, uniqueness constraints, and listed indexes; ids are prefixed-ULID text like `grf_…`, `run_…`, `view_…`), `models.py` (pydantic stubs), and empty-but-importable `api/`, `runs/`, `core/` subpackages.
4. `runs/executor.py`: uniform lifecycle over the `runs` table — statuses `queued|running|succeeded|failed|cancelled`, FIFO execution, CPU-type runs dispatched to a `ProcessPoolExecutor(max_workers=1)`, IO-type runs as asyncio tasks, `progress_json` updates, startup recovery marking any `running` run `failed` with error_text "interrupted by restart", cancellation (a queued run is always cancellable). Register a trivial `noop` run type to exercise the machinery.
5. API, Phase 0 scope only: `GET /api/health`; `GET /api/graphs/:gid/runs` (filter by `type`, `status`); `GET /api/graphs/:gid/runs/:runId`; `POST /api/graphs/:gid/runs/:runId/cancel`. Graph CRUD is Phase 1 — tests may insert graph rows directly through db helpers, and no-op runs may be enqueued via an internal helper rather than a public endpoint.
6. `core/` mock embedding provider: deterministic hash-seeded L2-normalized float32 vectors (default 1536 dims), exposing the same interface the real OpenAI provider will implement in Phase 2; zero network access.
7. `scripts/gen_synthetic.py` per the plan's "Synthetic Dataset" section: fully deterministic under a `--seed`, zero API calls; ~20 planted supplement-DTC topics built from hand-written phrase pools with combinatorial paraphrasing; 6 source types with realistic field presence (social comments lack titles and ratings); `--size` supporting 5000/50000/100000; timestamps across 2025 with a planted December-2025 spike topic, a topic that first appears in November 2025, and a topic that vanishes mid-year; ground-truth topic id in each record's `metadata`; output is JSON records conforming to the plan's normalized record template, written to a caller-specified path (default under a gitignored `data/` dir).

Verified by — run all of these; do not claim completion from belief:

- `.venv/bin/pytest` green, including at minimum: migrations apply on a fresh db (and are idempotent on reopen); an end-to-end no-op run observed moving queued→running→succeeded through the runs polling API; cancelling a queued run yields `cancelled`; startup recovery (seed a `running` run, boot the app, expect `failed` / "interrupted by restart"); mock provider determinism and unit-norm; generator determinism (same seed twice → byte-identical output at a small size).
- `ruff check .` clean.
- App boots against a fresh data dir and `GET /api/health` returns 200.
- `python scripts/gen_synthetic.py --size 5000 --seed 42 --out data/synthetic-5k.json` completes in seconds; output record count is exactly 5000 and planted temporal patterns are present (assert in a test, not by eyeball).
- `npm run check` passes with its updated script (node frontend tests untouched and green).

While preserving: `src/` and all frontend files byte-identical; `plan/` untouched; no dependencies beyond those listed; no network access in any test or in the generator; no future-phase implementation (no real OpenAI calls, no clustering/layout/labeling/trends logic beyond schema and importable stubs).

Between iterations: run pytest and ruff after each meaningful change and let failures pick the next action; keep a running list of any decision or small deviation from `plan/build_plan.md` and include it in the final summary instead of silently diverging.

If blocked — hdbscan or umap-learn fail to build on this machine, the plan doc is ambiguous or self-contradictory on something Phase 0 needs, or a verification step cannot be run — stop and report the exact blocker, what was attempted, the evidence gathered, and the specific decision or input that would unlock progress. Do not substitute alternative libraries or alter the schema unilaterally.
