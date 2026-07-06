# Phase 23 — Date-windowed trend charts, left-card polish, and a self-service UI verification harness

## Outcome

Four deliverables, in priority order:

1. **A committed, repeatable UI verification harness** (`scripts/ui_probe.py` + `scripts/ui_smoke.py`) that drives a real headless Chromium over CDP with zero third-party dependencies, so interactive UI regressions (scrubber, filters, facets) are caught by a script instead of a human. A working reference implementation is provided in `plan/goals/phase23_cdp_reference.py` — it has been verified end-to-end against this app (navigation, `Runtime.evaluate`, real `Input.dispatchMouseEvent` scrubbing, keyboard events, screenshots). Adopt it rather than reinventing.
2. **Date-range-reactive trend charts.** When the user sets a date range (inputs or presets), every trend visualization re-renders against the windowed bucket series: left topic-card sparklines, the card spike badge, the inspector chart, its caption, and the scrubber readouts. Today all of them render the full series regardless of filters.
3. **Left topic-card polish** (details below): compact source mix, full-width sparkline, human-readable spike badge dates, remove developer-facing "mean p" from the card.
4. **Facet select accessibility fix**: `<select data-facet-selector>` in `src/app.js` renders without an `id` or `name` attribute and triggers a DevTools issue ("A form field element should have an id or name attribute"). Give it both. Sweep for any other dynamically-created form control with the same gap.

Explicit **non-goal**: do NOT adopt a chart library (uPlot, Chart.js, etc.). The decision was evaluated and rejected: our chart needs (sparkline + crosshair scrub + readout) are already met by ~200 lines of tested vanilla SVG; the recent chart bugs were data-staleness and process failures, not drawing failures; a library adds 40–200 KB runtime weight, a styling boundary, and a supply-chain surface for zero new capability. Runtime dependencies remain exactly: `ol`.

## Context

- Branch: `phase23/date-window-ui-harness` (already created off latest `main`, which includes PR #41's interactive inspector sparkline).
- Trend data shape (already client-side, nothing new to fetch): `runtime.trends` in `src/app.js` maps `clusterId → buckets`, each bucket `{bucketStart: "YYYY-MM-DD", count, share, spikeScore}`; the series-level bucket unit is `"week"`. Topic cards also carry a persisted snapshot `topic.trend = {bucket, spikeScore, topBucket}` used by `spikeBadge()` (`src/uiState.js:273`).
- Filters live in `state.filters` with date-only strings `start` / `end` (empty string = unbounded); presets flow through `datePresetFilters` / `applyDatePreset` (`src/uiState.js:438`).
- Render paths that must become window-aware: card sparkline (`src/app.js` ~line 400, `renderSparkline(series…)`), inspector chart + caption + scrubber binding (`renderInspector`, ~line 444–470 and `bindInspectorSparklineScrubber` ~line 1011).
- Pure chart logic lives in `src/uiState.js` (trim, partial marking, scrub math) with node tests in `tests/uiState.test.mjs` (36 tests, `npm run test:ui` or `node --test tests/uiState.test.mjs`).

## Requirements

### 1. UI verification harness

- `scripts/ui_probe.py`: the CDP client library. Start from `plan/goals/phase23_cdp_reference.py` (verified working). Requirements:
  - Python stdlib only (the WebSocket client is hand-rolled in the reference — keep it; it handles masking, fragmentation, 64-bit lengths, and ping/pong).
  - Browser binary resolution order: `$DATAGRAPH_CHROME_BIN` if set, else newest `chrome-headless-shell` under `~/Library/Caches/ms-playwright/chromium_headless_shell-*/`, else `google-chrome`/`chromium` on PATH. Fail with a clear message listing what was tried.
  - Keep the public surface small and documented: `Browser(port)`, `.goto(url, settle)`, `.js(expr)`, `.mouse_move(x, y)`, `.click(x, y)`, `.cmd(method, **params)` (for key events etc.), `.shot(path)`, `.console` (captured console entries), `.close()`.
- `scripts/ui_smoke.py`: the scenario suite — the "bug tests" this phase exists to automate. It must:
  - Accept `--base-url` (default `http://127.0.0.1:8080`) and assume a running server seeded via `scripts/demo_seed.py` (document the two-command setup in the script docstring and README).
  - Run these scenarios, asserting each, and exit non-zero with a readable failure report if any fails:
    a. **Boot**: landing page renders the graph picker; clicking the first view row lands in the workspace with topics listed.
    b. **Form-field hygiene**: every `<select>` and `<input>` in the live DOM has an `id` or `name` attribute (this locks in deliverable 4).
    c. **Scrubber**: select the first topic; the inspector sparkline exists; dispatch real `mouseMoved` events at 20%/50%/80% of the SVG width and assert the `[data-sparkline-readout]` text changes and matches `formatTrendBucketReadout` shape (`N records · week of …`); crosshair element becomes visible; `ArrowRight`/`ArrowLeft` on the focused SVG move the readout; `Escape`/pointer-leave restores the default caption.
    d. **Date-window reactivity**: capture the selected topic's inspector sparkline path `d` attribute and readout caption; click the `30d` preset; assert the path/caption changed and the card sparklines re-rendered (compare one card's SVG markup before/after); click `All`; assert restoration.
    e. **Console hygiene**: zero console errors across the whole run.
  - Save screenshots for each scenario under `output/ui-smoke/` (gitignored already via `output/`).
- README: a short "UI verification" subsection in the development docs — the two commands (seed + serve, then `python scripts/ui_smoke.py`) and the `DATAGRAPH_CHROME_BIN` override.
- Do NOT wire into CI (no Chrome guarantee there); this is a local reviewer/agent tool. Do not add it to pytest collection.
- Delete `plan/goals/phase23_cdp_reference.py` once absorbed.

### 2. Date-windowed trend charts

- New pure function in `src/uiState.js`: `trendWindowBuckets(buckets, filters, options = {})` returning the sub-array of buckets whose bucket interval intersects `[filters.start, filters.end]` (either bound may be empty = unbounded). A week bucket `bucketStart` spans 7 days; a bucket is included if any part of its span is inside the range. Date-only string comparisons must be timezone-safe (reuse the existing `parseDateOnly` approach).
- All chart consumers derive from the windowed series: card sparkline, inspector chart, caption, and the scrubber (bind to the windowed buckets so readout indices match what is drawn).
- **Windowing happens before leading-zero trim and partial marking.** A bucket cropped by the window edge is NOT "partial" (dashed marking means incomplete data, not cropping); true partial first/final buckets keep their marking only when they survive the window.
- Spike badge becomes window-aware: when a date window is active, the badge shows the maximum in-window bucket `spikeScore` (respecting the existing `> threshold` gate) with a human-readable date; when no window is active, keep using the persisted snapshot but humanize the date (see polish below). New pure helper (e.g. `windowedSpikeBadge(topic, buckets, filters)`) so it's node-testable.
- Empty window (no buckets intersect): card shows no sparkline and no spike badge; inspector shows a quiet "No trend data in the selected range" caption instead of an empty chart. No exceptions thrown.
- Topic sort by "spike" may keep using the persisted snapshot (sorting stability across windows is fine); do not build windowed sorting in this phase.

### 3. Left topic-card polish

- **Source mix**: replace the multi-line full enumeration with one truncated line: top two sources by count plus `+N more` (e.g. `chat_transcript 90 · support_ticket 90 · +4 more`). Pure helper in `uiState.js` (e.g. `compactSourceMix(sourceMix, limit = 2)`) with node tests. The full mix stays in the inspector, unchanged.
- **Sparkline width**: the card sparkline currently renders at a fixed small width leaving dead space; make it span the card's content width (wider viewBox and/or CSS `width: 100%`), same height. Small-card sparklines remain non-interactive.
- **Spike badge date format**: `Spike 69.0 in 2025-12-01` → human form consistent with the scrubber readout, e.g. `Spike 69.0 · week of Dec 1`. Reuse the existing `trendBucketReadoutLabel` formatting; include the year when the bucket's year differs from the current year (match existing readout behavior).
- **Remove `mean p` from the card**: the `281/281 visible · mean p 0.89` line drops the `mean p` segment. Ensure mean probability remains visible in the inspector details (add a row there if it is not already present).
- Do not change card click/selection behavior, the 500-row list cap, sort/search, or the noise card.

### 4. Facet select fix

- `<select data-facet-selector …>` gets `id="facetSelector" name="facetSelector"` (and keep the existing `aria-label`). Sweep `src/app.js` for any other generated `<select>`/`<input>`/`<textarea>` lacking both `id` and `name` and fix likewise. The smoke suite's form-field-hygiene scenario is the regression guard.

## Verification (evidence required — paste actual outputs)

1. `node --test tests/uiState.test.mjs` — all existing tests green plus new tests covering: `trendWindowBuckets` (unbounded, both-bounded, edge-intersection, empty result, window-before-trim interplay with leading zeros and partial marking), `windowedSpikeBadge` (gate, windowed max vs snapshot, empty window), `compactSourceMix` (≤2 sources, >2 sources, empty).
2. `.venv/bin/python -m pytest -m "not slow" -n auto` green (no backend changes expected; prove no regression).
3. `npm run build` clean.
4. **The smoke suite is the acceptance gate**: run `scripts/demo_seed.py`, serve, `python scripts/ui_smoke.py`, and paste its full output showing every scenario PASS. If you cannot launch the browser in your sandbox (port binding is typically blocked there), say so explicitly and paste the suite's failure output — the reviewer will run it outside the sandbox; do not claim browser scenarios passed without running them.
5. Screenshot evidence (from the smoke run when possible): workspace with polished cards; inspector mid-scrub; the same topic before/after a `30d` preset showing both panels' charts changed.

## Constraints

- No new runtime dependencies (`package.json` `dependencies` stays `ol` only). No new Python dependencies (harness is stdlib-only). Dev-dependencies also unchanged — the harness needs none.
- All new chart/window/badge/source-mix logic is pure functions in `src/uiState.js` with node tests; `src/app.js` only wires DOM.
- Scrubber public contract from Phase 22 is unchanged (`trendSparklineBucketIndexAtX`, `trendSparklinePointAtIndex/AtX`, `trendSparklineKeyboardIndex`, `trendSparklineReadout`, `formatTrendBucketReadout` keep signatures and semantics for un-windowed input).
- Readout/caption/tooltip text set via `textContent` only (labels are untrusted data).
- Keep the existing visual language (no redesign): colors, spacing, and typography stay; this is a polish pass, not a re-theme.
- Do not modify `plan/build_plan.md`.

## Iteration policy

Work in small commits on `phase23/date-window-ui-harness`. If a smoke scenario fails, fix the app or the scenario's selector — never weaken an assertion to pass. Re-run the full verification list after the last change, not just the affected piece.

## Blocked-stop

Stop and report (rather than improvising) if: the reference CDP client fails against your environment in a way that requires third-party packages; windowing the buckets would require server/API changes; or the smoke suite cannot express a required assertion without new dependencies.
