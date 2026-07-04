CREATE TABLE record_summaries (
  model TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  rendered_text TEXT NOT NULL,
  junk_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(model, prompt_hash, text_hash)
);

CREATE TABLE summary_items (
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  text_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending', 'summarized', 'reused', 'failed')),
  PRIMARY KEY(run_id, record_id)
);

CREATE INDEX idx_summary_items_run_status ON summary_items(run_id, status);
