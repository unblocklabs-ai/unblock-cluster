# OpenClaw Pilot Round 3 — Mega-Topic Decomposition + Facets

Purpose: acceptance for Phase 11 on real data. Round 2 left one open quality question — the 45% mega-topic ("Order status and support requests", 1,145 records) that global tuning provably cannot split — and two pilot rounds of questions that needed facet breakdowns. Both capabilities now exist; this round judges whether they deliver.

Setup: `git pull` main and restart the server (`npm run build` first — Phase 10 changed serving). Your existing round-2 graph and runs remain valid — do NOT re-upload or re-embed; the schema is unchanged. Read the README's new "Decomposing A Large Topic" section before starting.

## 1. Decompose the mega-topic (the main event)

Target: cluster 0 ("Order status and support requests") on your round-2 default cluster run.

- Follow the README workflow: `POST .../cluster` with `{"focus": {"clusterId": 0}}` (focus runs are automatically non-default — they cannot disturb your artifact). Try config defaults first; if children are too few or everything lands in noise, iterate: smaller `minClusterSize` (try 10, then 8 with `minSamples` ~5), and `clusterSelectionMethod: "leaf"` inside the focus. Report EVERY attempt: params, childCount, noise, sizes.
- Label the best sub-run (`POST .../label {"clusterRunId": "<focusRun>", "setDefault": false}`), read `GET .../topics?clusterRunId=<focusRun>`.
- THE VERDICT (human judgment, the whole point): are the children distinct, actionable sub-themes a support lead would route differently — e.g. where-is-my-order vs address-change vs skip-week vs damaged-arrival? Or is the blob genuinely homogeneous "where's my order" noise? Classify each child (genuine-support / junk / ambiguous) with sizes and labels, exactly like round 2's topic table.
- If topic 0 decomposes well, repeat once for cluster 1 ("Delivery address and shipment issues", 605) as a second data point.
- Note the wall-time per focus run and any `warnings[]` you see on override reads (informational mismatch warnings are expected and correct there).

## 2. Facet re-verdicts (the twice-deferred questions)

Re-ask the questions both rounds couldn't express, now with `facetBy`:

- "Which products/SKUs drive delivery complaints?" → `GET .../topics?facetBy=product` (and `facetBy=sku`), plus `topic_evidence` with `"facetBy": "product"` for the delivery topics.
- "What changed by support channel / Kustomer tag?" → `facetBy=metadata.channel` and `facetBy=metadata.primaryTag` on topics, and on one temporal recipe (e.g. `surprising_topics` or `compare_periods`).
- Per question: verdict — answerable now (credible / partly / no)? Attach the facet payloads. Note the "(none)" share per facet (tells us whether your metadata fields are populated enough to be useful) and whether the top-20 + "(other)" capping ever hid something you needed.
- List anything STILL not expressible — that's the next feature queue.

## 3. Tunnel-readiness spot-check (one minute)

With the server freshly restarted: fetch the artifact twice with `curl -sI --compressed` (second time sending the returned `ETag` as `If-None-Match`) and report the Content-Encoding, wire size, and that the second response is `304`. This confirms Phase 10 behaves on your machine before any tunnel goes up.

## Report format

Compact: the per-attempt decomposition table, the child-topic classification table with your verdict sentence ("a support lead would / would not route these differently"), facet question verdicts with payloads, the curl spot-check output, plus run stats JSON and analysis-events export as usual. Human interventions count (streak to keep: 0).
