# OpenClaw Pilot Round 2 — Filtered Re-run

Purpose: round 1 proved the pipeline and exposed the input problem (~41% of
the graph was non-support content: link/tracking noise, OOO/autoresponders,
promo/PR, score-only NPS). Round 2 measures whether disciplined pre-upload
filtering plus the Phase 8/9 improvements produce a graph a brand can trust.
Everything below is comparable against round 1's numbers — report deltas,
not just states.

Before starting: pull latest `main` and `npm run build` (Phase 8 changed run
responses to camelCase and added `setDefault`/`warnings`; Phase 9 rebuilt the
UI). The README agent contract now includes the Pre-Upload Filtering and
Iterating On Quality sections — follow them.

## 1. Filter and re-extract (the main event)

- Apply the README exclusion checklist to the same Sakara Kustomer export:
  drop zero-inbound conversations (you already have `inboundMessageCount` in
  metadata), OOO/autoresponder patterns, newsletters and tracking-pixel/
  URL-dominant bodies, vendor/PR outreach, and score-only NPS rows (keep NPS
  records WITH comments if any exist).
- For incrementals, extract the first meaningful inbound customer message
  instead of Kustomer's `preview` (your own round-1 finding).
- Document the exact filter rules you used (patterns, thresholds, metadata
  predicates) and report exclusion counts BY REASON — this becomes the
  reusable Kustomer filtering recipe for future brands.
- Upload to a fresh graph. Report: records in vs round 1's 3,509, and the
  reason-bucketed exclusions.

## 2. Re-cluster and measure the delta

- Run at defaults first. Report clusterCount, noiseRatio, effectiveHdbscan
  vs round 1 (26 topics, 0.97% noise, minClusterSize 18).
- The headline metric: share of clustered records in junk topics — round 1
  was ~32% junk + 9% score-only NPS. Target: under 5% combined. Read every
  topic label and classify it genuine-support / junk / ambiguous.
- If any junk still clusters, report what pattern survived your filters —
  that's the next rule for the recipe.
- Use the new tuning workflow where granularity looks off: experiment with
  `{"setDefault": false}`, promote the winner with `true`. Report whether
  the workflow felt right and whether you saw any `warnings[]` from artifact/
  topics along the way (they should only appear if defaults get mismatched).

## 3. Label and evidence quality, round 2 verdicts

- Labels: same human classification as round 1 (recognizable / wrong / too
  generic) — round 1 had ~14 recognizable of 26. Watch for duplicate-sounding
  labels (over-split smell) and `coherent: false` (with clean input, expect
  zero fires; any fire is interesting — include the cluster).
- Re-ask the SAME brand questions from round 1 via the same recipes, plus
  any new ones the brand cares about. For each: response + human verdict
  after reading cited records (credible / partly / wrong). Round 1's
  surprising_topics top-5 was all junk; the target is all-genuine.
- Check whether the known operational themes (delivery delays, promo-code
  issues) now rank with credible spike attribution.
- Note again any question the five recipes cannot express — especially
  product/SKU/channel breakdowns (we have a `facetBy` feature queued; your
  round-2 questions confirm or reshape it).

## 4. The human pass (unblocked this round)

- Rebuild the UI (`npm run build`) so Bek reviews the Phase 9 interface.
- Expose the viz to Bek via a Cloudflare tunnel to the local server (or
  ship headless screenshots as interim: map with a selected record, list
  mode, a topic inspector — the Phase 9 verification recipe).
- Bek's review focus: does clicking records feel right now, do the topics
  read like his mental model of the brand's support load, and can he answer
  one real question from the UI alone (report which question and whether he
  could).

## 5. Report format

Same as round 1 (artifacts bundle + report), with a delta table up front:

| Metric | Round 1 | Round 2 |
|---|---|---|
| Records uploaded | 3,509 | |
| Junk-topic record share | ~32% (+9% NPS) | |
| Topics / noise | 26 / 0.97% | |
| Recognizable labels | ~14/26 | |
| surprising_topics top-5 genuine | 0/5 | |
| Human interventions (API flow) | 0 | |
| coherent:false fires | 6 (all NPS) | |

Attach: filter recipe with exclusion counts, run stats, evidence
request/response pairs + verdicts, analysis_events export, tuning-run params
if used, and the UI feedback from Bek's pass.
