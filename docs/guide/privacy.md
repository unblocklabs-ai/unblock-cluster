# Privacy

Extraction, pre-filtering, aggregation, and redaction belong to agents before
upload — this service has no redaction pipeline. With `provider: "openai"`,
customer text can leave the machine through three provider flows: embedding
runs send each record's rendered text the first time that text is seen
(content-addressed reuse means cached texts are not re-sent on reruns),
summarize runs (optional) likewise send each record's uncached rendered raw
text to the configured summarization model (`gpt-5.4-nano` by default), and
label runs (optional — but whenever one is triggered) send each topic's
representative text to the configured labeling model (`gpt-5.4-mini` by
default). Both model names are config values; overriding them redirects those
flows to whatever OpenAI model is named. Label representatives are raw
`customerText` by default for raw clusters, and summary `rendered_text` by
default for summary-backed clusters unless `labeling.textSource` is set
explicitly — though any representative missing a summary still falls back to
its raw `customerText`, so raw text can leave the machine even on
summary-backed label runs.
Skipping summarization or labeling skips those optional flows; nothing else
transmits customer text. Redact or drop sensitive values before upload. The
demo, default tests, mock embeddings, scripted summarization, artifact reads,
and frontend build make no network calls. Evidence reads make no network calls
except `topic_search` / `question_evidence`, which embed the supplied question
once with the resolved embedding provider.

Support-system PII often hides in structured fields and quoted text, not just
the main message body. Check for requester names in ticket titles, signature
and greeting names, quoted email display names such as `"Jane Doe" <jane@...>`,
phone and address blocks, order/contact forms pasted into replies, and
platform usernames or handles. Redact those patterns in the extraction layer
before upload, especially for health, fertility, finance, or other sensitive
domains.
