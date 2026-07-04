# Phase 13 `/goal` prompt (for gpt-5.5 / Codex)

Paste everything below as the `/goal`:

---

/goal Complete Phase 13 ("Extraction-quality guidance and small backlog") of `plan/build_plan.md`. The binding context is Amendments 2026-07-04 (d) and (e) — read both; they record the pilot findings this phase turns into standing agent guidance. This is a docs-led phase with two small code items. No new dependencies.

Desired end state: the README's agent contract teaches what three pilot rounds proved about input representation — so future agents START from these defaults instead of rediscovering them — and the two small backlog items (HEAD support, facet-metadata guidance) are closed; all verified by a green suite.

Deliverables:

1. New README section "Extraction Quality: What The Input Does To The Output" inside the Agent Contract, carrying the pilot evidence (cite the concrete numbers — they are the persuasion):
   - REPRESENTATION DOMINATES. One record per conversation with `customerText` = the chronological concatenation of ALL customer-authored messages (agent replies excluded). Evidence: switching from first-message/preview text to concatenation dissolved a 45%-of-graph generic mega-topic (1,145 records, "order status") into concrete operational themes (826 "warm or spoiled deliveries" + 526 "delivery/address"). Thin input produces vague mega-topics that NO clustering parameter can fix. Token budget note: real support threads measured max ~2.6k / p95 ~1k tokens — far under the 8k cap.
   - FILTER AT THE MESSAGE LEVEL, BEFORE CONCATENATION. Junk rules are representation-dependent: patterns tuned on single messages over-fire on concatenated threads (quoted footers, tracking links, a customer mentioning travel is not an OOO reply). Redact URLs/emails/phones inside kept messages rather than dropping records for containing them.
   - CHANGING REPRESENTATION CHANGES EVERY TEXT — a full re-embed (content-addressing cannot reuse). Cheap in absolute terms; plan for it rather than being surprised.
   - MAKE METADATA FACET-WORTHY AT EXTRACTION TIME. Facet usefulness is gated entirely by metadata population: pilot channel facets were excellent; product was 100% "(none)" while order SKUs sat unused. Map SKUs to product families, surface issue-taxonomy fields and booleans (hasRefund-style) as metadata — facetBy is only as good as this mapping.
   - BACKFILL HISTORY AT ONBOARDING. Trend baselines need runway: with ~1 month of data, new/vanishing-topic detection is structurally empty (window = series start). Recommend 6–12 months of backfill so temporal evidence works from day one.
   - A DIAGNOSTIC TABLE mapping symptom → likely cause → fix: largest topic > ~30% of graph with a generic label → representation too thin, or drill with focus; coherent junk topics → filtering gap (mine the topic's representatives for the next rule); coherent:false topics → no-signal records (score-only rows); many near-duplicate labels → over-split, raise minClusterSize; high noise after enriching text → expected and honest, not a defect (pilot: 0.6% → 6.4%).
   - ITERATION PRACTICES: representation A/B (two graphs over the same conversations, compare topic tables — embeddings are the only cost); a per-brand canonical-questions list re-run after every extraction change (this is how "concat is better" became a measured claim); focus reclustering as a junk detector inside large topics.
2. HEAD support on read endpoints (pilot round 3 backlog: `curl -sI` and uptime probes get 405 today). At minimum the artifact endpoint must answer HEAD with the same headers (ETag, Cache-Control, Content-Type) and no body; prefer a general solution for GET routes if FastAPI/Starlette allows it cleanly (e.g. registering HEAD alongside GET) rather than per-route duplication. Read-only mode must treat HEAD like GET (allowed).
3. README polish accompanying #2: the tunnel section notes HEAD works for health checks, with a curl -I example.

Verified by — run all of these; do not claim completion from belief:

- `.venv/bin/pytest` green including new tests: HEAD on the artifact endpoint returns 200 (or 304 with If-None-Match) with ETag/Cache-Control headers and an empty body; HEAD allowed in read-only mode; HEAD on at least one other read endpoint if the general solution is taken.
- README renders sanely (markdown lint by eye) and the new section contains the diagnostic table and every numbered practice above — this is a completeness check against the goal, not a style suggestion.
- `ruff check .`, `npx knip`, `npm run build`, `npm run check` green; suite runtime reported.

While preserving: all Phase 0–12 tests green; no API shape changes beyond HEAD; `plan/` untouched; no new dependencies; CI unchanged; UI untouched.

Between iterations: run pytest and ruff after each meaningful change; keep a running list of decisions or deviations and include it in the final summary.

If blocked — Starlette's HEAD handling fights the gzip or ETag middleware ordering, or any guidance item seems to contradict the bible — stop and report the exact conflict rather than resolving it silently. Do not thin out the pilot numbers from the guidance (the evidence is the persuasion), and do not move agent-side practices into service-side code.
