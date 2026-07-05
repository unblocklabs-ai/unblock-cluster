# OpenClaw Brief — Perelel Round 1 Report (Second-Brand Portability)

Purpose: you are the first brand to onboard AFTER the system stabilized —
your report measures whether the knowledge split works (generic science in
the service README, brand rules in per-brand playbooks) and what a fresh
onboarding actually costs. Plus one immediate action item.

## 0. IMMEDIATE — PII redaction gap (do this first)

Representative records currently show full customer names in titles
("Support Request from <first> <last>..."). This is women's-health data
(pregnancy status, conditions) flowing to OpenAI via embeddings,
summarization, and labeling. The privacy contract puts redaction on the
agent BEFORE upload. Add personal-name redaction (and re-check
emails/phones/addresses) to your extraction pass, re-upload, and re-run the
pipeline — content-addressing limits re-embed/re-summarize cost to the
changed texts (which will be all of them; budget accordingly, it is still
small). Update your Perelel playbook with the redaction rules used. Report
what leaked, what you changed, and the re-run cost from tokenUsage.

## 1. Portability audit (the reason this report exists)

- Timeline: wall-clock from first data export to first labeled graph, and
  which steps dominated.
- Knowledge sources: for each onboarding decision (representation,
  filtering, context writing, config), classify where the answer came from:
  (a) the service README alone, (b) the Sakara playbook (adapted), (c) had
  to be invented fresh for Perelel. Category (c) is the gold — it tells us
  what the README is still missing.
- Which Sakara filter-recipe rules transferred verbatim, which needed
  Perelel variants, which were unnecessary?
- Human interventions count for the API flow (the streak across all Sakara
  rounds: 0).
- Your summarization.context: paste it. Did you enumerate Perelel's product
  families per the README canonicalization guidance? Show
  facetBy=summary.product for a big topic either way.

## 2. The 25% noise question (most interesting number on the board)

Sakara ran 0.6–6.4% noise; you are at ~25% (2,621/10,389). Investigate,
don't assume:

- Read 25 random noise records. Classify: genuinely unique one-off
  questions / junk that slipped the semantic gate / members of a real theme
  the clustering missed.
- Report effectiveHdbscan from your cluster run stats, and try one tuning
  pass (setDefault:false): smaller minClusterSize and/or minSamples — does
  noise convert into coherent small topics, or stay noise?
- Hypothesis to test: personalized health questions may be intrinsically
  more unique per-record than logistics complaints — if the noise reads as
  real-but-individual customer questions, that is a finding about the
  domain, not a defect. Say which it is, with examples (redacted).

## 3. Standing setup for Perelel (the practices)

- Write Perelel's canonical-questions list (5–10 standing brand questions),
  run them as evidence calls, verdict each (credible/partly/wrong) after
  reading cited records. Note anything inexpressible.
- The May 18 same-day spikes in two operational topics (shipping address,
  canceled-but-charged) look like a real incident — does it correlate with
  a known event (site change, 3PL issue, billing deploy)? This tests spike
  attribution credibility on brand two.
- How much history did you backfill? If less than ~6 months, note whether
  new/vanishing-topic recipes have runway (README guidance).
- Junk report: junkType counts from your summarize run — did the semantic
  gate handle Perelel's junk without new regex? Anything that slipped?

## 4. Feedback (freeform but specific)

- Doc gaps: every place the README was wrong, ambiguous, or silent for a
  non-Kustomer/non-Sakara context. Exact quotes of what confused you.
- API/DX: any 4xx/500, retries, dead ends — with payloads.
- UI: which Phase 16 features earned their place from your human's use
  (sparklines, dim selection, facets, deep links), what is missing.
- The one thing you would change about onboarding for brand three.

## Report format

Standard bundle: report.md with the portability classification table, noise
investigation with redacted examples, canonical Q&A verdicts, junk counts,
facet payloads, run stats + tokenUsage (cost your run in dollars), config +
context used, and the PII remediation summary. Attach analysis-events
export.
