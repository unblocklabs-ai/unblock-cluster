# QMD Memory Bundle v1

`qmd-memory-v1` is the first adapter for Data Graph's provider-neutral external-vector
import service. The boundary is a portable snapshot produced on the machine where QMD
is installed. Import does not open a QMD database, reconstruct chunks, invoke QMD, or
call an embedding provider.

## Directory contract

```text
qmd-memory-bundle/
  manifest.json
  chunks.ndjson
  vectors.f32
  checksums.json
```

All JSON is UTF-8. `chunks.ndjson` contains one JSON object per line. `vectors.f32`
contains contiguous little-endian IEEE-754 float32 vectors; chunk offsets are byte
offsets from the start of that file. Payload filenames must be single relative
filenames, so a bundle cannot escape its directory.

### Manifest

The v1 manifest has this shape:

```json
{
  "schema": {"name": "qmd-memory-bundle", "version": 1},
  "exportId": "stable-export-identity",
  "exportedAt": "2026-05-02T00:00:00Z",
  "exporter": {"name": "qmd-memory-exporter", "version": "1.0.0"},
  "sourceIdentity": {
    "id": "stable-non-secret-installation-id",
    "label": "Optional operator label",
    "metadata": {}
  },
  "chunkCount": 8,
  "documentCount": 4,
  "embedding": {
    "model": "model-name-from-qmd",
    "fingerprint": "stable-space-fingerprint",
    "dimensions": 1024,
    "dtype": "float32-le",
    "distanceMetric": "cosine",
    "normalization": "normalized"
  },
  "chunking": {"strategy": "exporter-owned metadata when available"},
  "snapshot": {"mode": "full", "deletionPolicy": "tombstone-absent"},
  "payloads": {
    "chunks": "chunks.ndjson",
    "vectors": "vectors.f32",
    "checksums": "checksums.json"
  },
  "checksums": {
    "chunks.ndjson": {"sha256": "...", "bytes": 123},
    "vectors.f32": {"sha256": "...", "bytes": 32768}
  }
}
```

`sourceIdentity.id` must be stable for one QMD installation or logical source but must
not contain host credentials, database paths with secrets, or machine tokens. Import
binds a Data Graph dataset name to exactly one format and source identity.

V1 supports one embedding space per bundle, `float32-le`, cosine distance, and the
normalization values `normalized`, `unnormalized`, or `unknown`. The adapter rejects a
chunk-level `embeddingModel` or `embeddingFingerprint` that differs from the manifest.

### Chunk records

Each line in `chunks.ndjson` has:

```json
{
  "externalId": "stable document-hash + sequence + fingerprint identity",
  "documentHash": "content hash from QMD",
  "sequence": 0,
  "text": "the exact text that QMD embedded",
  "characterStart": 120,
  "characterEnd": 286,
  "totalChunks": 5,
  "collection": "memory",
  "path": "notes/example.md",
  "title": "Example",
  "documentCreatedAt": "2026-01-01T00:00:00Z",
  "documentModifiedAt": "2026-01-02T00:00:00Z",
  "active": true,
  "embeddedAt": "2026-01-03T00:00:00Z",
  "embeddingModel": "model-name-from-qmd",
  "embeddingFingerprint": "stable-space-fingerprint",
  "vector": {"offset": 0, "length": 4096},
  "metadata": {}
}
```

`characterEnd` may be `null`; all other fields above are required. `metadata` is the
extension point for later claim-level or provider-specific facts. Offsets must be
contiguous, start at zero, and advance by exactly `dimensions * 4`. The vector payload
must have exactly `chunkCount * dimensions * 4` bytes.

### Checksums

`checksums.json` uses:

```json
{
  "algorithm": "sha256",
  "files": {
    "manifest.json": {"sha256": "...", "bytes": 1000},
    "chunks.ndjson": {"sha256": "...", "bytes": 5000},
    "vectors.f32": {"sha256": "...", "bytes": 32768}
  }
}
```

The chunks and vectors entries must exactly match the inline manifest entries. Import
streams checksum calculation and vector reads; it never loads the complete vector
payload into memory.

## Import and run the pipeline

```sh
.venv/bin/python scripts/import_vectors.py \
  --format qmd-memory-v1 \
  --input ./qmd-memory-bundle \
  --dataset bill-memory
```

Use `--data-dir` or `DATAGRAPH_DATA_DIR` to select the database. The first import creates
a graph and its `all_records` view; later snapshots reuse them. The command prints the
graph, view, external import, succeeded embedding run, and visualization URL.

The embedding run is the new view default, so the existing endpoints need no special
QMD parameters:

```sh
curl -sS -X POST \
  "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/views/$VIEW_ID/cluster" \
  -H 'Content-Type: application/json' -d '{}'

curl -sS -X POST \
  "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/views/$VIEW_ID/layout" \
  -H 'Content-Type: application/json' -d '{}'
```

Label, trend, topic, outlier, artifact, and `topic_evidence` endpoints then use the same
commands as generated embeddings. Artifact points, full record responses, and topic
evidence representatives expose a top-level `provenance` object containing exact chunk,
source, embedding-space, and bundle identities.

## Atomicity, versioning, and deletion

Validation completes before persistence. Persistence uses one `BEGIN IMMEDIATE` SQLite
transaction, so a parser, database, or process error leaves no partial graph, import,
record, vector, or succeeded-looking run.

- Reimporting the same `exportId` and bytes returns its existing import and embedding run.
- Reusing an `exportId` with different bytes is rejected.
- A snapshot whose `exportedAt` is not newer than the latest distinct snapshot is rejected.
- An unchanged chunk reuses its immutable record version and vector.
- A changed chunk creates a deterministic immutable version; the prior record remains
  available to historical runs but becomes inactive for future runs.
- A chunk absent from a later full snapshot is tombstoned. Its record remains traceable
  through historical runs but is excluded from future record scopes and embeddings.
- `active: false` creates or retains traceable provenance without adding the chunk to the
  snapshot's embedding run.
- External embedding runs are immutable import history and cannot be deleted separately;
  deleting the dataset graph removes its imports, record versions, and run lineage.

Original float32 bytes are always stored in `external_vectors.original_vector`. Existing
representative math assumes unit vectors. If the manifest says `unnormalized` or
`unknown`, import stores a separately documented L2-normalized derivative for clustering;
it never mutates or discards the original. A `normalized` payload must actually have unit
norm and is stored byte-for-byte as the clustering representation.

## Known limitations and exporter follow-up contract

- V1 is a full-snapshot format. Delta manifests and explicit per-chunk delete records are
  future schema versions.
- V1 accepts one cosine float32 embedding space per bundle. It does not claim
  compatibility between different model fingerprints.
- Semantic `topic_search` and `question_evidence` need a query encoder proven compatible
  with the imported fingerprint. They currently return `422` for external runs instead
  of comparing vectors from mixed spaces. Stored-vector evidence such as
  `topic_evidence` is fully supported.
- Labels still use the configured label provider; importing vectors itself is completely
  offline.

The QMD-side exporter must emit QMD's exact embedded chunk text and original float32
vector together. It must not ask Data Graph to recover text from a full document or to
recreate tokenizer boundaries. It should derive a stable `externalId` from document hash,
chunk sequence, and embedding fingerprint; preserve collection/path/title, timestamps,
active state, start position and optional known end position; emit a stable non-secret
source identity; sort chunks deterministically; write contiguous offsets; and calculate
all counts and checksums after payload finalization.
