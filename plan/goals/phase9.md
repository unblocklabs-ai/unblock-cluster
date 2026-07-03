# Phase 9 `/goal` prompt (for gpt-5.5 / Codex)

Paste everything below as the `/goal`:

---

/goal Complete Phase 9 ("UI: record inspection, projection, and interaction polish") — human viz-pass feedback from the first real-data pilot. `plan/build_plan.md` remains the spec backdrop; this goal is the binding list. Frontend files (`src/`, `index.html`, `vite.config.js`, node tests) are open for modification; keep the stack (Vite + OpenLayers + vanilla JS, no new frameworks or runtime deps). Backend changes are allowed ONLY where this goal names them.

Desired end state: clicking anything that represents a record — a map node, a representative card, a list row — opens a full record inspector in the right panel; the map uses a proper flat projection with responsive hit-detection and a quiet console; long URLs and text wrap everywhere; mode buttons show state and the toolbar groups sensibly — verified by node tests, pytest, and headless screenshots.

Deliverables:

1. Flat projection for the layout map: define a custom cartesian OpenLayers projection (arbitrary units, no geographic semantics) and use it for the View, features, and fit logic. Acceptance: the `useGeographic()` console warning is gone and map behavior (fit, zoom, pan, selection) is unchanged or better.
2. Hit-detection and click responsiveness: eliminate the repeated `Canvas2D ... willReadFrequently` warnings by fixing the readback pattern — investigate what triggers the getImageData spam (likely pointer-move hover hit-detection over ~3.5k features) and restructure: hit-detect on click always; if hover affordances are kept, throttle or disable them at high feature counts. Node click must feel immediate (no long tasks; verify no per-mousemove full hit scans remain). If a small number of the warnings are provably emitted by OpenLayers internals we cannot configure away, document that with evidence; do not suppress the console.
3. The selection model (the core of this phase). One inspector in the right panel, two levels:
   - Topic selected (existing behavior, kept): topic summary, label/summary/coherence, source mix, trend badge, clickable representative cards.
   - Record selected — via map node click, representative card click, or list row click: the panel shows FULL record detail: title, full untruncated `customerText` fetched from `GET /api/graphs/:gid/records/:rid` (the artifact carries only 300 chars — fetch on selection, show the truncated text as a placeholder while loading), sourceType/sourceName, product, sku, rating, sentiment, tags, timestamp, `recordUrl` as a link, `metadata` rendered as a compact key/value list, plus its topic label, clusterProbability, and outlierScore. A clear "back to topic" affordance returns to the topic inspector; selecting a record highlights the corresponding map node.
   - Map/list/topic-panel selection state lives in `uiState.js` as pure, node-testable transitions (select topic, select record, back, clear; record selection implies its topic context).
4. List mode layout: the center table remains the browse surface; the right panel in list mode is the SAME inspector (topic or record detail) — it must not duplicate the table's content as cards. Table cells wrap (`overflow-wrap: anywhere`) with the text column line-clamped to a few lines; full text lives in the inspector.
5. Text wrapping everywhere: representative cards, inspector fields, and topic panel summaries wrap long tokens (URLs, tracking blobs) — nothing overflows its container horizontally. Cards line-clamp with full content available via the inspector.
6. Toolbar and mode affordances: Map/List is a segmented control with visible active state (styling + `aria-pressed`); Fit and Clear Topic are map-scope actions grouped separately and Fit is hidden (or disabled) in list mode; Clear Topic remains available in both modes when a topic is selected.

Verified by — run all of these; do not claim completion from belief:

- `node --test`: new/updated uiState tests covering the selection transitions (topic → record → back, record selection from list vs map vs card converge to the same state, clear resets), and the artifact→state mapping still green.
- `.venv/bin/pytest` green (no backend changes expected beyond none — if you believe a backend change is needed, stop and report instead).
- `npm run build` green; `ruff check .` and `npx knip` clean; `npm run check` green; suite runtime reported.
- Headless verification against `scripts/demo_seed.py` data (the recipe from prior phases: chrome `--headless --window-size=1500,950 --screenshot` / `--dump-dom`):
  - Map screenshot: cluster points visible AND a selected record's node visually highlighted with the record inspector populated in the right panel.
  - List screenshot: wrapped table text (no horizontal overflow), inspector showing a record detail.
  - A screenshot with a long-URL record selected proving the inspector wraps it.
  - Console capture (headless chrome `--enable-logging=stderr` or DOM-injected console recorder): assert the `useGeographic` warning is absent and report the count of remaining `willReadFrequently` warnings with the evidence for any that remain.
  - Report what each screenshot shows — spread, panels, states — not just that files exist (prior-phase lesson: weak pixel predicates hid a one-dot map).

While preserving: `plan/` untouched; no new dependencies (Python or npm); backend API surface unchanged (the record-detail fetch uses the EXISTING records endpoint); artifact shape unchanged; all Phase 0–8 tests green; CI makes no network calls.

Between iterations: run the node tests, pytest, and ruff after each meaningful change and let failures pick the next action; keep a running list of decisions or deviations and include it in the final summary.

If blocked — the custom projection fights OpenLayers feature rendering, the willReadFrequently source turns out to be unfixable inside OL's canvas renderer, or the records endpoint lacks something the inspector needs — stop and report the exact blocker, what was attempted, the evidence gathered, and the decision needed. Do not add a WebGL renderer, a UI framework, or backend endpoints to work around it.
