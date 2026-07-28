ALTER TABLE records
ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1));

CREATE TABLE embedding_spaces (
  id TEXT PRIMARY KEY,
  origin TEXT NOT NULL CHECK(origin IN ('generated', 'external')),
  model TEXT NOT NULL,
  fingerprint TEXT,
  dimensions INTEGER NOT NULL CHECK(dimensions > 0),
  dtype TEXT NOT NULL,
  distance_metric TEXT NOT NULL,
  normalization TEXT NOT NULL CHECK(normalization IN ('normalized', 'unnormalized', 'unknown')),
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE external_datasets (
  id TEXT PRIMARY KEY,
  graph_id TEXT NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
  dataset_key TEXT NOT NULL UNIQUE,
  format TEXT NOT NULL,
  source_identity_json TEXT NOT NULL,
  latest_export_id TEXT,
  latest_exported_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE external_imports (
  id TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL REFERENCES external_datasets(id) ON DELETE CASCADE,
  embedding_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  format TEXT NOT NULL,
  schema_name TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  export_id TEXT NOT NULL,
  exported_at TEXT NOT NULL,
  exporter_name TEXT NOT NULL,
  exporter_version TEXT NOT NULL,
  bundle_digest TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  stats_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  UNIQUE(dataset_id, export_id)
);

CREATE TABLE external_chunk_versions (
  dataset_id TEXT NOT NULL REFERENCES external_datasets(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,
  version_hash TEXT NOT NULL,
  record_id TEXT NOT NULL UNIQUE REFERENCES records(id) ON DELETE CASCADE,
  introduced_import_id TEXT NOT NULL REFERENCES external_imports(id) ON DELETE CASCADE,
  embedding_space_id TEXT NOT NULL REFERENCES embedding_spaces(id) ON DELETE RESTRICT,
  vector_id TEXT NOT NULL,
  vector_sha256 TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  document_hash TEXT NOT NULL,
  chunk_sequence INTEGER NOT NULL,
  character_start INTEGER NOT NULL,
  character_end INTEGER,
  total_chunks INTEGER NOT NULL,
  collection TEXT NOT NULL,
  source_path TEXT NOT NULL,
  source_title TEXT NOT NULL,
  document_created_at TEXT NOT NULL,
  document_modified_at TEXT NOT NULL,
  embedded_at TEXT NOT NULL,
  source_active INTEGER NOT NULL CHECK(source_active IN (0, 1)),
  is_current INTEGER NOT NULL CHECK(is_current IN (0, 1)),
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  superseded_at TEXT,
  PRIMARY KEY(dataset_id, external_id, version_hash)
);

CREATE UNIQUE INDEX idx_external_chunk_current
ON external_chunk_versions(dataset_id, external_id)
WHERE is_current = 1;

CREATE TABLE external_import_items (
  import_id TEXT NOT NULL REFERENCES external_imports(id) ON DELETE CASCADE,
  record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active', 'inactive')),
  PRIMARY KEY(import_id, record_id),
  UNIQUE(import_id, external_id)
);

CREATE TABLE external_vectors (
  embedding_space_id TEXT NOT NULL REFERENCES embedding_spaces(id) ON DELETE CASCADE,
  vector_id TEXT NOT NULL,
  vector_sha256 TEXT NOT NULL,
  original_vector BLOB NOT NULL,
  derived_vector BLOB NOT NULL,
  transformation TEXT NOT NULL CHECK(transformation IN ('none', 'l2-normalize')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(embedding_space_id, vector_id)
);

CREATE INDEX idx_external_datasets_graph ON external_datasets(graph_id);
CREATE INDEX idx_external_imports_dataset_exported
ON external_imports(dataset_id, exported_at);
CREATE INDEX idx_external_chunk_record ON external_chunk_versions(record_id);
CREATE INDEX idx_external_import_items_external
ON external_import_items(import_id, external_id);
