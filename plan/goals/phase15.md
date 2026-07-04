# Phase 15 `/goal` prompt (for gpt-5.5 / Codex)

Paste everything below as the `/goal`:

---

/goal Complete Phase 15 ("Config forward-compatibility and cost auditability") of `plan/build_plan.md`. The binding spec is Amendment 2026-07-04 (g) — read it; it records pilot round 4's verdict (summary representation adopted for Sakara) and the two issues this phase fixes. This is a small hardening phase. No new dependencies.

Desired end state: graphs created before any config section existed work with endpoints that need that section (stored configs hydrate with current defaults on load — systemically, so every future section addition inherits the fix); LLM run stats persist token usage so cost is auditable; and the README documents reuse/self-heal semantics and product-facet canonicalization via context — all verified by a green suite including a regression test that reproduces the pilot's 500.

Deliverables:

1. Config forward-compatibility (the pilot's 500 bug, fixed systemically): introduce ONE shared loader (e.g. `load_graph_config(row)` in `datagraph/core/config.py`) that merges the stored `config_json` over `DEFAULT_GRAPH_CONFIG` (reusing the existing `_merge_section` semantics — stored values win, missing sections/keys fill from defaults, unknown stored keys tolerated-and-dropped with nothing raised: old graphs must never 500 on read). Replace EVERY direct `json.loads(graph["config_json"])` call site across api/ and runs/ with it. Do NOT rewrite stored configs on disk — hydration happens at read time only; PATCH persists the merged+validated result as today.
2. Regression tests that reproduce the pilot failure class: insert a graph row directly with (a) a pre-Phase-14 config (no `summarization` section) → `POST /summarize` succeeds with defaults and the run's echoed params show the hydrated section; (b) a pre-Phase-11 config (no `cluster.space.minDist`) → cluster run succeeds; (c) a config containing an unknown legacy key → reads succeed, PATCH still validates strictly.
3. Token usage in LLM run stats: summarize and label runs sum the provider responses' usage (`prompt_tokens`/`completion_tokens` from the OpenAI SDK; the embed API also reports usage — include it there too as `promptTokens`) and persist `tokenUsage: {promptTokens, completionTokens (where applicable), totalTokens}` in `stats_json`. Providers that report no usage (the mock/scripted ones) yield zeros — tests assert the field exists and sums correctly via a scripted provider that reports fake usage. Update `scripts/bench_scale.py`'s report and the summarize-run report endpoint to surface it.
4. README: (a) reuse/self-heal semantics under the summarize section — reuse is per-text (content-addressed), per-record failures are isolated into `failedRecordIds` and re-attempted by the next run (the pilot's second run healed 4 first-run failures with 4 provider calls; that is design, not drift); (b) product-facet canonicalization guidance — enumerate the brand's product families in `summarization.context` so `summary.product` maps to a closed set (this is the agent-side fix for round 4's high-cardinality caveat), and note `summary.issue` is intentionally per-record (topics are the aggregate; issue facets are for spot reads); (c) cost expectations updated with the measured pilot numbers (2,446 records ≈ 14 min, ~2.5k requests) and a pointer to `tokenUsage` in run stats.

Verified by — run all of these; do not claim completion from belief:

- `.venv/bin/pytest` green including: the three forward-compat regression tests above; no remaining direct `json.loads(...config_json...)` call sites outside the shared loader (enforce with a small test that greps the source — crude but effective against regression); `tokenUsage` present and correctly summed in summarize and label run stats with a scripted usage-reporting provider, zeros with the plain mock; embed stats include promptTokens; the summarize-run report surfaces tokenUsage.
- `ruff check .`, `npx knip`, `npm run build`, `npm run check` green; suite runtime reported (~5.5 min budget).

While preserving: all Phase 0–14 tests green; stored configs never mutated by reads; PATCH validation remains strict (hydration tolerance applies to READS of legacy rows only); API shapes unchanged except the additive `tokenUsage` stats field; `plan/` untouched; no new dependencies; CI unchanged; no network in CI.

Between iterations: run pytest and ruff after each meaningful change; keep a running list of decisions or deviations and include it in the final summary.

If blocked — the OpenAI SDK's usage fields differ from expectations for the chat or embeddings surface, or a call site resists the shared loader cleanly — stop and report the exact conflict. Do not silently rewrite stored configs, loosen PATCH validation, or fabricate token counts where the provider reports none.
