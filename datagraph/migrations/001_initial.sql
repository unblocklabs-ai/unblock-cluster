CREATE TABLE graphs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  config_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE records (
  id TEXT PRIMARY KEY,
  graph_id TEXT NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
  record_key TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_name TEXT NOT NULL,
  source_record_id TEXT NOT NULL,
  title TEXT,
  customer_text TEXT NOT NULL,
  record_url TEXT,
  product TEXT,
  sku TEXT,
  rating REAL,
  sentiment TEXT,
  tags_json TEXT,
  timestamp_utc TEXT NOT NULL,
  timestamp_ms INTEGER NOT NULL,
  metadata_json TEXT,
  normalized_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(graph_id, record_key)
);

CREATE TABLE views (
  id TEXT PRIMARY KEY,
  graph_id TEXT NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT,
  scope_json TEXT NOT NULL,
  default_embedding_run_id TEXT,
  default_cluster_run_id TEXT,
  default_layout_run_id TEXT,
  default_label_run_id TEXT,
  default_trend_run_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(graph_id, name),
  FOREIGN KEY(default_embedding_run_id) REFERENCES runs(id) ON DELETE SET NULL,
  FOREIGN KEY(default_cluster_run_id) REFERENCES runs(id) ON DELETE SET NULL,
  FOREIGN KEY(default_layout_run_id) REFERENCES runs(id) ON DELETE SET NULL,
  FOREIGN KEY(default_label_run_id) REFERENCES runs(id) ON DELETE SET NULL,
  FOREIGN KEY(default_trend_run_id) REFERENCES runs(id) ON DELETE SET NULL
);

CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  graph_id TEXT NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
  view_id TEXT REFERENCES views(id) ON DELETE SET NULL,
  type TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  params_json TEXT NOT NULL,
  progress_json TEXT NOT NULL,
  error_text TEXT,
  input_refs_json TEXT NOT NULL,
  stats_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT
);

CREATE TABLE embedding_vectors (
  model TEXT NOT NULL,
  dimensions INTEGER NOT NULL,
  text_hash TEXT NOT NULL,
  vector BLOB NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(model, dimensions, text_hash)
);

CREATE TABLE embedding_items (
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  text_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY(run_id, record_id)
);

CREATE TABLE cluster_memberships (
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  cluster_id INTEGER NOT NULL,
  probability REAL NOT NULL,
  outlier_score REAL NOT NULL,
  is_noise INTEGER NOT NULL CHECK(is_noise IN (0, 1)),
  PRIMARY KEY(run_id, record_id)
);

CREATE TABLE cluster_summaries (
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  cluster_id INTEGER NOT NULL,
  size INTEGER NOT NULL,
  mean_probability REAL NOT NULL,
  representative_record_ids_json TEXT NOT NULL,
  source_mix_json TEXT NOT NULL,
  PRIMARY KEY(run_id, cluster_id)
);

CREATE TABLE cluster_labels (
  id TEXT PRIMARY KEY,
  label_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  cluster_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  cluster_id INTEGER NOT NULL,
  model TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  top_k INTEGER NOT NULL,
  label TEXT NOT NULL,
  summary TEXT NOT NULL,
  key_signals_json TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  coherent INTEGER NOT NULL CHECK(coherent IN (0, 1)),
  created_at TEXT NOT NULL
);

CREATE TABLE layout_points (
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  x REAL NOT NULL,
  y REAL NOT NULL,
  PRIMARY KEY(run_id, record_id)
);

CREATE TABLE trend_results (
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  cluster_id INTEGER NOT NULL,
  bucket_start TEXT NOT NULL,
  count INTEGER NOT NULL,
  share REAL NOT NULL,
  spike_score REAL NOT NULL,
  PRIMARY KEY(run_id, cluster_id, bucket_start)
);

CREATE TABLE trend_summaries (
  run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  summary_json TEXT NOT NULL
);

CREATE TABLE analysis_events (
  id TEXT PRIMARY KEY,
  graph_id TEXT NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
  view_id TEXT REFERENCES views(id) ON DELETE SET NULL,
  recipe TEXT NOT NULL,
  params_json TEXT NOT NULL,
  run_refs_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_records_graph_timestamp_ms ON records(graph_id, timestamp_ms);
CREATE INDEX idx_records_graph_source_type ON records(graph_id, source_type);
CREATE INDEX idx_cluster_memberships_run_cluster ON cluster_memberships(run_id, cluster_id);
CREATE INDEX idx_trend_results_run_cluster ON trend_results(run_id, cluster_id);

