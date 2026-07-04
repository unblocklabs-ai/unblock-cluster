# Phase 10 `/goal` prompt (for gpt-5.5 / Codex)

Paste everything below as the `/goal`:

---

/goal Complete Phase 10 ("Payload and rendering performance for tunnel exposure") — findings from a measured performance investigation (5k demo dataset; numbers below are the baseline to beat). Purpose: the viz will be exposed through a Cloudflare tunnel, so wire size, server CPU per request, and client rendering cost all matter. `plan/build_plan.md` remains the backdrop; this goal is the binding list. No new dependencies (Python or npm) — everything below is achievable with starlette built-ins and vanilla JS.

Baseline measurements (demo 5k dataset): artifact 3.23MB raw / would-be 0.59MB gzipped, no Content-Encoding, no ETag, ~87ms recompute on every fetch; floats serialized at full precision (~750KB of the artifact); records list endpoint spends 46% of its bytes on the `normalized` field; JS bundle 277KB uncompressed on the wire; static assets have ETag but no Cache-Control; frontend re-renders the full map source + all list rows on every search keystroke with per-feature Style allocation.

Desired end state: the demo artifact travels ≤ 0.55MB on the wire, repeat loads are 304s with zero recompute, filters are smooth, and list mode stays bounded — all proven by measured before/after numbers in tests.

Deliverables:

1. Gzip: add starlette's `GZipMiddleware` (minimum_size ~1KB) to the app. Applies to API JSON and served static assets alike.
2. Artifact caching: the artifact is immutable for a given set of resolved run ids + record set. Add (a) a strong `ETag` derived from the resolved runRefs + the view's record count and max `updated_at`; (b) `If-None-Match` → `304` with no recompute; (c) a small in-process cache (bounded, e.g. last 4 composed artifacts keyed by the ETag inputs) so even cold-header fetches skip recomposition; (d) `Cache-Control: no-cache` (always revalidate — correctness first, the 304 is the win). Invalidation is inherent: new runs/records change the key inputs.
3. Float trimming in the artifact: round `x`, `y`, `clusterProbability`, `outlierScore` (and topic `meanProbability`, trend `spikeScore`) to 4 decimal places at serialization. 4dp exceeds any visual or ranking need; do NOT round in storage, only in the response.
4. Records list slimming: `GET /api/graphs/:gid/records` and the view-records endpoint exclude `normalized` by default; an explicit `?include=normalized` restores it. The single-record endpoint keeps full fidelity (the UI inspector and agents use it). Update README and any tests relying on the default shape.
5. Static asset caching: serve `/assets/*` (hashed filenames) with `Cache-Control: public, max-age=31536000, immutable`; `/` (index.html) with `Cache-Control: no-cache`.
6. Frontend rendering efficiency (no behavior changes, only cost):
   - Debounce the search input (~200ms) so filter renders happen at most a few times per second; dropdown/date filters can stay immediate.
   - Replace per-feature `setStyle(new Style(...))` with a layer-level style function backed by a style cache keyed by (clusterId, isNoise, radius-bucket, selection state) — Style objects reused, not reallocated per record per render.
   - Selection-only changes (topic or record select/clear) must NOT rebuild the vector source or re-render the list rows — restyle via `layer.changed()` / targeted row class toggles.
   - List mode renders at most 500 rows with a "Show N more" affordance and a visible "showing X of Y" indicator (pure `uiState` pagination logic, node-tested). No virtualization library.

Verified by — run all of these; do not claim completion from belief:

- `.venv/bin/pytest` green, including new tests that assert MEASURED behavior: artifact response with `Accept-Encoding: gzip` carries `Content-Encoding: gzip` and body ≤ 0.55MB for the seeded 5k demo (vs 3.23MB baseline — assert the number, not just the header); second fetch with `If-None-Match` returns 304 with empty body; the in-process cache serves repeat cold-header fetches without recomposition (assert via a composition counter or timing proxy); float fields in the artifact have ≤ 4 decimals; records list excludes `normalized` by default, includes it with the flag, single-record unchanged; `/assets/*` responses carry the immutable Cache-Control and `/` carries no-cache.
- `node --test` green including the new list-pagination transitions; existing selection/filters tests still pass.
- Headless verification (demo seed + chrome, the standing recipe): map renders identically to Phase 9 (spread + selection screenshot), list shows the 500-row cap with the show-more control, and report DOM row count ≤ cap.
- Manual-style measurement reported in the summary: artifact wire bytes before/after, repeat-load status codes, and JS asset wire size with gzip.
- `ruff check .`, `npx knip`, `npm run build`, `npm run check` all green; suite runtime reported.

While preserving: artifact JSON field SHAPE unchanged (same keys — only float precision and transport change); records single-GET unchanged; all Phase 0–9 tests green (update only where the records-list default or float precision requires); `plan/` untouched; no new dependencies; CI makes no network calls; UI behavior identical except bounded list rendering.

Between iterations: run pytest, node tests, and ruff after each meaningful change and let failures pick the next action; keep a running list of decisions or deviations and include it in the final summary.

If blocked — GZipMiddleware interacts badly with the StaticFiles mount or FileResponse, the ETag inputs prove insufficient for correctness (stale artifact served after a data change), or the style-cache refactor fights OL's renderer — stop and report the exact blocker, what was attempted, the evidence gathered, and the decision needed. Do not add dependencies, change artifact keys, weaken the ETag to a timestamp, or cap the map's rendered points (only the LIST is capped).
