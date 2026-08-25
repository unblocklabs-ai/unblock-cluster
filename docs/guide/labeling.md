# Labeling Configuration And Reports

Label runs default to `labeling.topK: 12` stored representatives per topic
(valid range 1-20). `labeling.exampleTextLimit` defaults to `700` characters
(valid range 100-4,000) and controls the text the label provider sees; the
limit truncates `customerText` and a present title independently, so a full
representative block can slightly exceed it, and stored records are never
truncated. `labeling.promptAppend` accepts up to 2,000 characters and is
appended after either the built-in prompt or a full `labeling.prompt` override.
The example limit is echoed in both run params and stats; the append text is
echoed in run params, and the effective prompt hash changes when the append
text changes.

Terse-ticket brands often do better with fewer and shorter examples because
each representative already carries a complete support ask. Heterogeneous
clusters usually need more examples, not longer ones: raise `labeling.topK`
first so the model sees breadth, then adjust `exampleTextLimit` only when
important context is being truncated.

`labeling.textSource` controls which representative text is sent to the label
provider:

- `"auto"` (default): use summary `rendered_text` when the cluster run came
  from a summary-backed embedding run; otherwise use raw `customerText`.
- `"raw"`: always use raw `customerText`; this preserves pre-summary behavior.
- `"summary"`: require summary-backed cluster lineage. Without it, the label
  POST returns 422 naming the summarize endpoint to run first.

When summary text is selected, any representative missing a summary falls back
to raw `customerText`; run stats report `textSource: "summary_rendered_text"`
and `fallbackRawCount`. Raw runs report `textSource: "raw_customer_text"`.

Inspect exactly what a label run would send with:

```sh
curl -sS "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/label-runs/$LABEL_RUN_ID/report"
```

The report recomputes representative blocks from the run's recorded params and
stored representatives. It includes record ids, title presence, per-block text
source, truncation flags, prompt hash, model, duplicate-label groups, near
duplicates, and very-short/generic label flags. Near duplicates are detected
deterministically by lowercasing labels, tokenizing on alphanumerics, and
flagging label pairs whose shared-token count covers at least 80% of the
smaller token set; pairs that are identical after normalization are excluded
here and reported as duplicate groups instead (so case/punctuation-only
differences show up as duplicates, not near duplicates). The assembled prompts
and representative blocks sent to the provider are intentionally not
persisted — the report recomputes them from the run's recorded params (which
do retain any `prompt`/`promptAppend` config) and current assembly code.
