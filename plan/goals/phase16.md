# Phase 16 `/goal` prompt (for gpt-5.5 / Codex)

Paste everything below as the `/goal`:

---

/goal Complete Phase 16 ("UI insight and trust pass") — findings from a UI-focused review (screenshots + code audit; specifics below are the observed defects). Keep the stack (Vite + OpenLayers + vanilla JS), the Phase 9 interaction model (click-only hit detection, unified inspector), and the Phase 10 performance work. No new dependencies. This is a polish-and-surface phase, not a redesign. One small ADDITIVE backend change is in scope where named; everything else is frontend.

Desired end state: the picker is a real landing page; topic selection dims instead of hides; loading, warnings, and provenance are visible; the trend series, facets, and topic-panel ergonomics surface the data the API already computes — verified by node tests, headless screenshots of every new state, and a green suite.

Deliverables:

1. Picker as a landing page (observed: the graph card currently renders inside the map workspace with a non-functional filter bar, Fit button, and a "Select a topic" inspector — it reads as a broken state). When no graphId/viewId params: hide the workspace chrome entirely and render a dedicated layout — app title (unify on ONE name: "Data Graph"; the picker currently says "Open Data Graph"), one card per graph with name, record count, and its views (name, description, record count) as links. Empty state ("No graphs yet") points at the README quickstart.
2. Topic selection dims, not hides. Selecting a topic (panel click or topicId param) keeps ALL points on the map: selected topic's points full-color and slightly enlarged, others faded to low-opacity gray (the pointStyle layer already has the topicSelected branch — the filter path bypasses it; route selection through style state instead of removing records). Selection also auto-fits the view to the selected topic's points (Fit button returns to all). The Topic FILTER dropdown keeps its current hard-filter behavior — selection (spatial context) and filtering (scoping) become distinct, and the panel/inspector visible-counts must respect the distinction. List mode: selection highlights rows as today.
3. Trust surfaces:
   - Artifact loading state: skeleton or spinner + "Loading N records…" in the map/list area from fetch start until first render (observed: 100k views are blank white for ~5s). Errors keep the existing actionable-409 rendering.
   - Warnings banner: when `artifact.warnings` is non-empty, a dismissible amber banner above the workspace listing each warning verbatim (observed: the UI ignores the field entirely — the exact silent-mismatch class the warnings were built for).
   - Provenance footer under the topic panel: short run ids (last 6 chars) for embedding/cluster/label/trend runs, and a "summary representation" pill when applicable. BACKEND (additive only): the artifact's `runRefs` gains `summarizeRunId` and the artifact gains `representation: "raw"|"summary"`, derived from the embedding run's input refs/params — with tests; no other shape changes.
4. Insight surfaces:
   - Per-topic trend sparklines: fetch the view's trend series once (`GET .../trends`, tolerate 409 = no trend run → no sparklines); render a small inline-SVG sparkline (pure function building the path from buckets — node-testable) in each topic card and a larger one in the topic inspector; spike badge stays.
   - Topic panel ergonomics: a sort control (Size | Spike | Name) and a topic-search input filtering panel cards client-side (pure uiState transitions, node-tested).
   - Facets in the topic inspector: a small facet selector (sourceType, sourceName, product, sentiment, plus summary.product/summary.issue/summary.junkType only when the artifact says representation is "summary") that fetches `GET .../topics?facetBy=<field>` on demand and renders the selected topic's facet counts as labeled bars with counts; handle 422 (no lineage) with a quiet explanatory line.
5. Ergonomic details: date-range preset buttons (7d / 30d / 90d / All) next to the date inputs, driving the existing filter; `recordId` joins `mode`/`topicId` as a URL param so a selected record is deep-linkable/shareable (parse + apply on load, update on selection); a visible focus style and aria-labels on the new controls.
6. Styling: extend the existing `:root` custom properties into a small token set (spacing, panel typography scale, muted/accent colors) and apply to the new components + the topic panel so the pass looks intentional; no layout rework beyond the picker.

Verified by — run all of these; do not claim completion from belief:

- `node --test` green with new uiState tests: sort/search transitions, selection-vs-filter distinction (dim list vs removed list), sparkline path builder (empty series, single bucket, flat, spike shapes), URL param round-trip including recordId.
- `.venv/bin/pytest` green including the additive artifact fields (representation + summarizeRunId present for a summary-representation view, absent/raw otherwise; artifact shape otherwise byte-compatible — existing artifact tests updated only for the two new fields).
- Headless screenshots against the seeded demo (standing recipe), each DESCRIBED not just saved: the new picker; map with a topic selected showing dimmed-but-visible other points and auto-fit; sparklines visible in the topic panel; the loading state (capture mid-load at 100k bench data or throttled); the warnings banner (reproduce via a setDefault:true tuning run on a labeled view — the Phase 8 test scenario); facet bars in the inspector; a deep-linked record URL restoring selection.
- `ruff check .`, `npx knip`, `npm run build`, `npm run check` green; suite runtime reported.

While preserving: Phase 9 selection/inspector model and Phase 10 rendering costs (style cache, 500-row cap, debounce — dimming must go through the cached style function, not per-feature allocation); artifact byte-shape unchanged except the two named additive fields; `plan/` untouched; no new dependencies; CI unchanged; read-only mode unaffected.

Between iterations: run the node tests, pytest, and ruff after each meaningful change; keep a running list of decisions or deviations and include it in the final summary.

If blocked — dim-not-hide fights the style cache at 100k, the trends fetch shape doesn't fit the sparkline cleanly, or the additive artifact fields collide with the ETag/cache — stop and report the exact conflict, what was attempted, and the decision needed. Do not remove the hard topic filter, regress Phase 10 render performance, add hover hit-detection back, or let the picker keep any workspace chrome.
