# Phase 19 `/goal` prompt (for gpt-5.5 / Codex)

Paste everything below as the `/goal`:

---

/goal Complete Phase 19 ("Question-driven evidence and onboarding docs") of `plan/build_plan.md`. The binding spec is Amendment 2026-07-05 (h) — read it; it records the second brand's portability report, whose feedback this phase implements. Build on Phases 0–18. No new dependencies.

Desired end state: an agent can hand the service a natural-language question and get back the topics most likely to answer it (with evidence) — making canonical questions first-class instead of hand-mapped to topic ids (the ask has now surfaced from both brands) — plus the five documentation fixes the Perelel report quoted verbatim; all verified by a green suite including an offline structured-provider relevance test.

Deliverables:

1. `topic_search` evidence recipe: `POST /api/graphs/:gid/evidence` gains recipe `"topic_search"` with `"question": "<text>"` (required for this recipe, 422 otherwise; topK default 5 max 20). Execution: embed the question text ONCE via the graph's embedding config (this is the evidence endpoint's first provider call — an explicit, documented exception to "no provider calls": one embedding request, using the same provider/model as the resolved embedding run so the spaces match; the mock provider serves it in tests). Rank the resolved cluster run's topics by cosine similarity between the question vector and each topic's centroid — centroids computed from stored representative vectors (or member vectors) via the existing bulk loader; do NOT persist new state. Response: ranked topics with clusterId, label object, similarity score, size, sourceMix, representativeRecordIds, and the standard runRefs/freshness/vizUrl envelope; persisted to analysis_events like every recipe. IMPORTANT: if the resolved embedding run used `representation: "summary"`, embed the question as-is with the same model — document that questions are matched against whatever representation built the space.
2. `question_evidence` convenience recipe: same request shape; runs `topic_search`, takes the top match above a similarity floor (e.g. 0.2 — expose in the response, don't hide it), and returns full `topic_evidence` for it plus the runner-up list. 422 with the ranked list when nothing clears the floor ("no topic matches; closest were ...").
3. Documentation fixes, each answering a quoted gap from the Perelel report:
   - Privacy section: concrete PII pattern examples for support systems (requester names in ticket titles, signature/greeting names, quoted email display names, phone/address blocks, platform usernames) — examples, not just the abstract sentence.
   - Summarization context guidance: recommend a CLOSED product enum ("choose product from exactly: [...]") over prose enumeration — prose still yielded high-cardinality facet variants ("A / B" combos) at brand two; show the enum phrasing in the context example.
   - A "Reading Your Noise" protocol in the extraction-quality section: before declaring noise domain long-tail, read a random sample (classify junk / missed-theme / genuinely-individual) and run ONE smaller-minClusterSize tuning pass with setDefault:false; note the measured brand-two datum (25.8% noise at 10k, 0 junk in sample, tuning recaptured little — long-tail themes are normal at scale and focus reclustering is the drill, not global tuning).
   - Local automation note (README dev/agent section): scripts driving the API from stdin/heredocs hit macOS spawn's inability to re-import `<stdin>` for process-pool runs — write scripts to files, or use the `inline_cpu_runs=True` test seam; both agents and the reviewer have hit this.
   - Cost accounting: document the dollar-cost formula over `tokenUsage` (sum per run × the provider's current rate card) and explicitly state the repo does not maintain a rate card — pointing at tokenUsage in run stats and the summarize report.
4. UI (one small affordance, not a redesign): a "Noise" entry pinned at the bottom of the topic panel showing the noise count; selecting it dims non-noise points and lists a RANDOM sample of noise records (resampled each click, e.g. 20) in the inspector with the standard record-card affordances — the built-in random noise sampler brand two asked for, powering the noise-read protocol above. Node-testable sampling transition (seeded/injectable RNG for tests).

Verified by — run all of these; do not claim completion from belief:

- `.venv/bin/pytest -n auto` green including: topic_search on the planted structured-provider graph — a question phrased from a planted topic's vocabulary ranks that ground-truth topic #1 (the structured mock embeds by topic keyword, so relevance is deterministic); similarity scores descending; topK bounds; missing question 422; question_evidence returns full evidence for the top match and 422-with-candidates below the floor; the one-embedding-call exception verified via a counting provider (exactly one provider call per topic_search request, zero new runs, zero persisted vectors); works under summary representation; analysis_events row per call; read-only mode still allows it (evidence carve-out).
- `node --test` green including the noise-sampler transitions (seeded sampling, resample-on-click, selection dims correctly).
- Headless screenshot: noise entry selected — dimmed map + sampled noise records in the inspector, described.
- README contains all five doc fixes (completeness check against the quoted gaps).
- `ruff check .`, `npx knip`, `npm run build`, `npm run check` green; suite runtime budget unchanged (~35s parallel).

While preserving: all Phase 0–18 tests green; the evidence endpoint remains run-free and persistence-free apart from analysis_events and the SINGLE documented question-embedding call; artifact shape unchanged; `plan/` untouched; no new dependencies; existing recipes untouched.

Between iterations: fast tier while iterating, full checks before completion; keep the decisions list for the final summary.

If blocked — question-vs-centroid similarity is degenerate on the structured mock (report the scores), or the noise sampler fights the dim/selection model — stop and report with specifics. Do not persist question embeddings, add more than one provider call per search, or auto-answer questions with LLM narrative (ranking + evidence only; narrative stays with the agent).
