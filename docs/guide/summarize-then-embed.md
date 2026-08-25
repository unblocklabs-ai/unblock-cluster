# Summarize-Then-Embed (Optional)

Raw embedding remains the default. Use summarize-then-embed when regex junk
filtering is turning into whack-a-mole, source text length varies wildly, or
the source lacks useful facets such as product family, issue taxonomy, desired
resolution, or semantic junk type. The summarize run makes one structured
`gpt-5.4-nano` extraction call per record against the service-owned schema,
caches results by summarization model, prompt hash, and rendered raw text, and
stores a stable labeled-line summary representation for optional embedding.

```sh
SUMMARY_RUN_ID=$(
  curl -sS -X POST "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/summarize" \
    -H 'Content-Type: application/json' \
    -d '{"summarization":{"context":"Acme sells supplements and meal delivery. Real support traffic is about orders, spoiled deliveries, subscriptions, refunds, product guidance, and shipping. Choose product from exactly: [Metabolism Super Powder, Detox Water Drops, Daily Fiber, Prenatal Complete, Unknown]."}}' \
    | jq -r .id
)

SUMMARY_EMBED_RUN_ID=$(
  curl -sS -X POST "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/embeddings" \
    -H 'Content-Type: application/json' \
    -d '{"representation":"summary"}' | jq -r .id
)

curl -sS -X POST "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/views/$VIEW_ID/cluster" \
  -H 'Content-Type: application/json' \
  -d '{"embeddingRunId":"'$SUMMARY_EMBED_RUN_ID'","setDefault":false}'
```

`summarization.context` is optional static brand/service background, capped at
4,000 characters. It is appended to the effective prompt (the built-in prompt,
or a `summarization.prompt` override when one is set) and included in
`promptHash`, so changing context or `summarization.prompt` correctly
invalidates cached summaries. Use it to teach the semantic junk gate what
counts as real support traffic for this business. Also enumerate the brand's
canonical product families here, so `summary.product` maps extracted mentions
to a closed set instead of creating high-cardinality one-off product strings.
Prefer directive wording such as `Choose product from exactly: [A, B, C,
Unknown]`; prose enumeration still produced variants such as combined
`"Product A / Product B"` values in the Perelel run.

The A/B workflow is one graph, two embedding runs, and two cluster runs: keep a
raw embedding run as the control, create a summary embedding run, cluster each
with explicit `embeddingRunId`, then compare the same canonical questions
against `?clusterRunId=` overrides. Watch for suspiciously merged topics; that
is a homogenization smell and means the summary prompt is erasing customer
vocabulary. The built-in prompt explicitly asks for verbatim customer phrases
to preserve that signal.

Summary representation excludes records whose `junkType` is not `"none"` by
default at the embedding boundary. Use `{"representation":"summary",
"includeJunk":true}` only when intentionally inspecting the semantic junk
bucket. Summary facets resolve through the run lineage, so
`facetBy=summary.product` works only for clusters produced from a summary
embedding run; raw clusters return a 422 with the summarize/embed path to run.
Use `summary.product`, `summary.desiredResolution`, `summary.sentiment`, and
`summary.junkType` as aggregate facets. `summary.issue` is intentionally
per-record; use it for spot reads and reports, not as a topic-level aggregate
signal.

Summarization cost and latency are per-record LLM calls. Content-addressing
amortizes reruns: unchanged records with the same model and effective prompt
reuse stored summaries with zero provider calls, while changed records or
model/prompt/context changes summarize again. Reuse is per text, not per run: records that
failed in one summarize run are retried by the next run and can self-heal. In
the Sakara pilot, the second run reused 2,443 summaries and made 3 provider
calls to heal first-run misses; that is expected failure isolation, not drift.
Inspect the derived artifacts through
`GET /api/graphs/:gid/summarize-runs/:runId/report`, which reports junk counts
by type, token usage, and per-record summary fields for agent drop/keep
decisions.

For cost planning, the Sakara pilot summarized 2,446 records in about 14
minutes with about 2,500 provider requests. Run stats include
`tokenUsage: {promptTokens, completionTokens, totalTokens}` for summarize and
label runs, and prompt tokens for embedding runs, so actual spend is auditable
after the run completes. Dollar cost is:
`sum(promptTokens / 1_000_000 * input_rate + completionTokens / 1_000_000 *
output_rate)` per run, using the provider's current rate card for that model,
then summed across the runs in your workflow. This repo intentionally does not
maintain a rate card; use `tokenUsage` in run stats and the summarize-run
report, then apply the current provider prices outside the repo.

Receipts stay raw. Topic-record reads (`GET .../topics/:tid/records`),
evidence payloads, and the frontend artifact continue to show raw
`customerText`, never the summary representation. Summaries are derived
artifacts for embedding, faceting, labeling when selected, and the
summarize-run report; the representative blocks a summary-backed label run
sends to the label provider (and echoes in the label-run report) do use
summary text — see [Labeling](labeling.md).
