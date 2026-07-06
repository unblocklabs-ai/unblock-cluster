# Phase 21 `/goal` prompt (for gpt-5.5 / Codex)

---

/goal Complete Phase 21 ("Trend spike integrity and list-view polish") of `plan/build_plan.md`. The binding spec is Amendment 2026-07-05 (j) — read it first. Build on Phases 0–20. No new dependencies. Work on the current branch; plan/ is already committed — do not modify plan/.

The bug (user-reported from brand production): the first buckets of a trend series have an empty baseline, so their spike score equals their raw count — every topic's spike badge shows the date of the earliest records (e.g. "Spike 48.0 in 2025-12-29" = the first week bucket). The scores are meaningless and pollute surprising_topics rankings.

Desired end state: spike scores are statistically honest (series-start buckets gated, late-emerging-topic spikes preserved), the list view is scannable (color dots, single-line cells, icon sentiment), and the provenance metadata stops confusing non-developers — verified by exact-value math tests including the planted-spike preservation gate, node tests, and described headless screenshots.

Deliverables:

1. Spike baseline gate in `datagraph/core/trend_math.py`: buckets whose SERIES index is less than MIN_BASELINE_BUCKETS = 3 (a module constant, documented) get `spike_score = 0.0`. CRITICAL DISTINCTION: gate on series position, NOT on baseline emptiness — a topic emerging late in the series has a baseline of prior zero-buckets and its first-burst spike is meaningful and must still score (the planted December spike, ~69.0 with months of zero baseline, is the regression gate: it must still rank #1 in the existing planted-pattern tests). Counts/shares are unchanged — only spike scores are gated. Everything downstream inherits automatically (trend_results rows, window summaries, surprising_topics, snapshot topBucket/spikeScore); ensure the topic trend SNAPSHOT picks its max spike from eligible buckets only, and when a topic has no positive eligible spike its snapshot spikeScore is 0 with topBucket null (or the snapshot trend omitted — pick one, be consistent, document it).
2. UI spike badges: hide the spike badge (topic cards and inspector) when spikeScore <= 0 — no more first-week badges.
3. List view polish in `src/app.js`/`styles.css`:
   - Topic cell: the topic's color dot + label, single-line with ellipsis. Noise records (clusterId -1 / no topic) render a muted gray dot with the label "Noise" — replacing the current "Topic unknown".
   - Record (title) cell: single-line ellipsis, constrained width (title should never wrap to two lines).
   - Source and Sentiment cells: white-space nowrap (no more "negativ/e" wraps; source ids like customer_support_ticket stay on one line).
   - Sentiment as an icon: map positive/neutral/negative to a compact emoji or colored glyph with `title` and `aria-label` carrying the text value; unknown/other sentiment values render as short nowrap text; empty renders empty. Keep the full text form in the record inspector.
   - Sensible column widths so Text gets the remaining space.
4. Provenance de-noise: collapse the runs metadata block (the run-id chips + representation pill at the bottom of the topic panel) behind a small default-collapsed disclosure (e.g. "Run details"); click-to-copy stays inside; non-developers see one quiet line instead of six chips.
5. Small sweep items: search input gets a placeholder ("Search titles and text…"); the inspector's "Coherence: Normal" row is shown ONLY when the label is flagged incoherent (render nothing when normal — one less mystery word for non-developers).
6. README: one short note in the trends section documenting the spike gate (first 3 series buckets carry no spike score; late-topic first-burst spikes still count).

Verified by — run all of these; do not claim completion from belief:

- `.venv/bin/pytest -n auto` green including NEW exact-value trend_math tests: series bucket 0/1/2 score 0.0 even with large counts; bucket 3+ scores per the existing formula; a late-emerging topic (zeros through bucket k >= 3, then a burst) still produces its full spike; the EXISTING planted December-spike tests still pass unchanged (that topic's spike has months of prior buckets — if any planted gate fails, stop and report rather than adjusting thresholds); snapshot topBucket never points at a gated bucket; surprising_topics no longer ranks first-series-buckets.
- `node --test` green with new/updated cases: sentiment icon mapping (known values, unknown value passthrough, empty), noise-row labeling, single-line truncation classes present in rendered rows (string assertions are fine).
- Headless screenshots against the seeded demo, DESCRIBED: list view showing color-dotted single-line topic cells, icon sentiment, no wrapped columns; a topic panel where no first-bucket spike badge appears; the collapsed run-details disclosure closed and open.
- `ruff check .`, `npx knip`, `npm run build`, `npm run check` green; suite budget ~40s parallel.

While preserving: counts/shares/baselines in trend_math unchanged (only spike gating); artifact shape unchanged except snapshot semantics documented; receipts untouched; `plan/` untouched (already committed on this branch); no new dependencies; all Phase 0–20 tests green (update only where the spike gate legitimately changes expected scores — list every such change in your final summary with the old and new values).

Between iterations: run the fast pytest tier and node tests while iterating; full checks before claiming completion; keep a running list of decisions/deviations and include it in the final summary with the changed-assertion list.

If blocked — a planted-pattern gate fails under the position gate (report the scores and stop; the gate design may need my review), or the noise-row rendering fights the selection model — stop and report with specifics. Do not gate late-topic spikes, lower any planted gate, hide the provenance data entirely (collapse, not remove), or change count/share math.
