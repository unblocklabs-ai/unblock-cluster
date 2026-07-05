# Phase 18 `/goal` prompt (for gpt-5.5 / Codex)

Paste everything below as the `/goal`:

---

/goal Complete Phase 18 ("Sparkline correctness and UI nits") — a small frontend-only cleanup from real-brand usage (Perelel screenshots). No backend changes, no new dependencies, no layout rework. The trend SERIES stays zero-filled (that is a Phase 5 correctness requirement for baselines) — only the sparkline PRESENTATION changes.

Observed defects (from production screenshots): every sparkline for a topic that starts mid-history begins with a near-vertical climb out of zero (the zero-filled leading buckets drag the plot floor down — the user's report: "they all show a huge spike up because the first tick starts at 0"); nearly every sparkline ends with an artificial dip (the current, incomplete bucket rendered as a decline); large counts render without thousands separators ("10389 visible records").

Deliverables:

1. Sparkline leading-zero trim in `trendSparklinePath` (pure function, node-tested): drop leading zero-count buckets so the line starts at the topic's first nonzero bucket — "start at the first value". An all-zero series renders the existing flat midline. A series with one nonzero bucket after trimming uses the single-value flat rendering. The y-domain then derives from the trimmed values (no more forced zero floor from buckets the topic predates).
2. Partial-final-bucket honesty: when the final bucket is incomplete — determinable client-side: bucketStart + bucket granularity extends past the artifact data's max record timestamp (pass what is needed via options; keep the function pure) — the last segment must be visually marked as partial rather than drawn as a confident decline: reduced-opacity/dashed final segment (or hollow final point), your choice, but marked — do NOT silently drop real data. Complete final buckets render normally.
3. Sparkline native tooltip: one SVG `<title>` per sparkline summarizing the trimmed range — e.g. "Jun 1 – Jul 4 · peak 32 (May 11)" — so hover explains the shape. No JS hover handlers (Phase 9 removed hover machinery deliberately).
4. Thousands separators everywhere counts render: header chips, topic card sizes/visible counts, inspector totals, facet bar counts, picker record counts, list "Showing X of Y" (`Number.prototype.toLocaleString("en-US")` or equivalent single helper). Fix any pluralization glitches while there ("1 topics" class of bugs) via the same helper module.
5. Selected-topic scroll-into-view: when a topic becomes selected via URL deep link or map click (i.e., not by clicking its own card), scroll its card into view in the topic panel (`scrollIntoView({block: "nearest"})`).
6. Provenance chip ergonomics: each run chip in the footer gets the full run id as a `title` attribute and click-to-copy (navigator.clipboard with a brief "copied" affordance) — the short ids are for eyes, the full ids are what agents/debugging need.

Verified by — run all of these; do not claim completion from belief:

- `node --test` green with updated/new cases for `trendSparklinePath`: leading-zero trim (zeros then data; data from bucket one; all zeros; single nonzero), partial-final marking on/off, y-domain from trimmed values, tooltip text builder if extracted.
- `.venv/bin/pytest -n auto` green (nothing backend should change — if a test moves, explain why).
- Headless screenshots against the seeded demo, described: a topic panel where a late-starting topic's sparkline visibly starts at its first real value (compare the December-spike topic before/after if convenient); separators visible in the chips; the inspector sparkline with the partial-final marking.
- `ruff check .`, `npx knip`, `npm run build`, `npm run check` green.

While preserving: the trend series data and all backend payloads unchanged; sparkline SVG stays dependency-free inline markup; Phase 16 behavior otherwise intact; `plan/` untouched; suite runtime budget unchanged.

Between iterations: run the node tests and the fast pytest tier while iterating, full checks before claiming completion.

If blocked — the partial-bucket determination lacks a clean client-side signal, or trimming interacts oddly with the inspector's larger sparkline — stop and report with specifics. Do not zero-fill-trim the underlying trend data, add hover JS, or drop the final bucket silently.
