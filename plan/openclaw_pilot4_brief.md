# OpenClaw Pilot Round 4 — Raw vs Summary Representation A/B

Purpose: acceptance for Phase 14 (summarization runs) on real data, and the
referendum on summarize-then-embed. The service now does per-record
structured extraction (gpt-5.4-nano, service-owned schema, your brand
context in the prompt) and can embed the summary instead of raw text. Round
3's residual junk — the 164-record OOO topic and the sales-pitch pocket —
is already inside your concat graph, which makes it the perfect test: does
the semantic gate remove exactly that junk with ZERO new regex rules?

Setup: `git pull` main, `npm run build`, restart the server. Migration 002
applies automatically on boot. REUSE the round-3 concat graph
(`grf_01KWNF7BG8...`) — do not re-upload; the A/B runs on the same records.
Read the README's new "Summarize-Then-Embed" section first.

## 1. Summarize run (with your brand context)

- Write a short `summarization.context` for Sakara (a few sentences: what
  the company sells, what real support traffic is about, what junk looks
  like — vendor pitches, media requests, marketing sends). Include it in
  `POST /api/graphs/:gid/summarize`.
- Report: wall time, providerRequests/providerRetries, failed count, and
  the summarize-run report's junk counts by type. Expectation to test: the
  OOO records and sales pitches land in junkType != none.
- Reuse check: immediately re-POST the same summarize run → expect 100%
  reused, zero provider calls.
- Quote check: sample ~10 summaries from the report/records and verify
  keyCustomerPhrases appear VERBATIM in the raw customer text.

## 2. The A/B — sibling view, defaults untouched

- `POST /embeddings {"representation": "summary"}` (junk excluded by
  default). Note reuse/requests.
- Create a sibling view (e.g. `summary_ab`, empty scope). On THAT view:
  cluster (with `embeddingRunId` = the summary embedding run), label,
  layout, trends — all with default promotion (per-view defaults mean your
  raw concat view and its artifact stay untouched).
- You now have both representations live: the original view = raw concat,
  `summary_ab` = summary. Both viz URLs work side by side.

## 3. Verdicts (the point of the round)

- Topic tables side by side: raw-view topics vs summary-view topics, with
  sizes, labels, and your genuine-support/junk/ambiguous classification.
- JUNK: did the OOO topic and sales-pitch pocket vanish from the summary
  side without any new filter rules? Any junk that still clustered?
- HOMOGENIZATION WATCH (the known risk): topics that MERGE suspiciously on
  the summary side — e.g. warm/spoiled deliveries collapsing together with
  missing-items or delivery-delays into one generic delivery topic. Paste
  examples either way. This is the failure mode that decides the verdict.
- CANONICAL QUESTIONS: run your standing question list as evidence calls
  against BOTH views (`viewId` selects the side). Same
  credible/partly/wrong verdicts after reading cited records. Confirm the
  receipts: representatives must show RAW customer text on the summary side
  too.
- FACETS: `facetBy=summary.product` and `facetBy=summary.issue` on the
  summary view's topics — is product finally populated (round 3: 100%
  "(none)")? Attach payloads.
- UI: open both viz URLs — which topic panel would a support lead rather
  work from?
- COST: total summarize cost/time for 2,446 records, and your read on
  whether it's worth it at 10x the volume.

## 4. The recommendation

End the report with one paragraph: should summary representation become the
Sakara default going forward (new pulls summarize-then-embed), stay an
analysis option, or be dropped? Base it on the homogenization verdict +
junk elimination + facet value + cost.

Cleanup: if the A/B view isn't worth keeping, `DELETE` it (and its runs) —
the new lifecycle endpoints exist for exactly this.

## Report format

Delta-style: the side-by-side topic table, junk verdict, homogenization
examples, canonical Q&A pairs with verdicts for both sides, facet payloads,
quote-check samples, cost/time numbers, summarize-run report JSON, and the
recommendation paragraph. Human interventions count (streak: 0 through
three rounds).
