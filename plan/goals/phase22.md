# Phase 22 `/goal` prompt (for gpt-5.5 / Codex)

---

/goal Complete Phase 22 ("Interactive inspector sparkline") — user request: scrubbing the topic inspector's trend chart should show the date and value under the pointer. Frontend-only; build on the current branch (it contains the Phase 21b sparkline parts seam — partial start/end marking — which the scrubber must respect). No new dependencies; plan/ untouched; no backend changes.

Scope: ONLY the inspector's large sparkline becomes interactive. The small topic-card sparklines keep their native <title> tooltip (scrubbing a 96x24 chart is not usable; scope discipline).

Interaction spec (follow it precisely — it encodes dataviz interaction rules):

1. Crosshair + nearest-bucket snapping: pointermove anywhere over the inspector sparkline SVG maps the pointer x to the NEAREST bucket of the TRIMMED series (the plotted one — after Phase 18's leading-zero trim; off-by-one against the raw series is the likely bug, test for it). Render a vertical hairline at the snapped bucket's x and emphasize that point (small filled circle). The whole SVG is the hit target — the reader aims at a date, never at the 2px line.
2. Readout row, not a floating tooltip: a reserved single-line readout directly under the chart (space always reserved — zero layout jump). Default content: the existing range/peak summary text. While scrubbing: value leads, label follows — e.g. "212 records · week of May 18" — with "(partial)" appended when the snapped bucket is a partial first/final bucket, and "· spike 3.2" appended when that bucket's spikeScore > 0. Weekly buckets read "week of <Mon D>"; day buckets "<Mon D>"; month buckets "<Mon YYYY>". pointerleave restores the default content. All text set via textContent (never innerHTML concatenation).
3. Keyboard parity (same details on focus as on hover): the SVG is focusable (tabindex=0, role="img", aria-label naming the topic's trend); ArrowLeft/ArrowRight move the active bucket, Home/End jump to first/last, Escape clears; the readout row is aria-live="polite" so scrub values are announced. Visible focus style consistent with the existing tokens.
4. Touch works for free via pointer events (pointermove/pointerdown/pointerleave — use pointer events, not mouse events).
5. Make the inspector sparkline modestly taller (~40-48px plot height) so scrubbing is comfortable; the topic-card sparklines are unchanged.

Implementation seams (testability is the point):

- Pure functions in src/uiState.js (node-tested): pointer-x -> trimmed-bucket-index mapping given the same width/padding options the path builder uses (edge clamping, empty/single-bucket series, correctness against the trim offset), and a readout formatter (bucket granularity wording, partial suffix, spike suffix, thousands separators via the existing formatters).
- app.js wires events and DOM only; crosshair/point elements live inside the existing SVG; no re-render of the panel per pointermove (mutate the crosshair/readout nodes directly).

Verified by — run all of these; do not claim completion from belief:

- node --test green with new cases: x->index mapping (left edge, right edge, between points, single bucket, empty, AND a series with trimmed leading zeros where raw index != plotted index), readout formatting (week/day/month wording, partial suffix, spike suffix, separators), arrow-key navigation transitions (bounds clamped), Escape clearing.
- Markup/DOM assertions: the inspector SVG carries tabindex/role/aria-label; the readout row exists with aria-live; crosshair elements present after a simulated pointer event if your test setup allows, otherwise assert the wiring functions exist and are exported (state the limitation).
- .venv/bin/pytest -n auto green (no backend changes expected — if any test moves, say why).
- ruff check ., npx knip, npm run build, npm run check green.
- Headless screenshots are known-broken in this environment; verify at the function/DOM level and SAY SO in your summary — the requesting user has the dashboard open and is the visual verifier.

While preserving: small-card sparklines unchanged; Phase 21b partial marking intact (the scrubber readout must agree with the dashed segments); no floating tooltips; no new dependencies; map behavior untouched; plan/ untouched.

If blocked — the trim-offset mapping is ambiguous for both-ends-partial series, or pointer events fight the SVG sizing — stop and report with specifics. Do not add hover machinery to the map or the small sparklines, and do not introduce a floating tooltip that can clip the panel.
