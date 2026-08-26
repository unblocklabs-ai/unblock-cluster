# Data Graph

Local-first FastAPI + Vite application for turning agent-prepared feedback
records into run-based topic maps. The backend stores normalized records,
embeddings, clusters, layouts, labels, trends, evidence calls, and composed UI
artifacts in SQLite. The frontend reads one artifact endpoint and renders a
map/list inspection UI.

## Quickstart

```sh
npm install
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm run build
.venv/bin/python scripts/demo_seed.py
DATAGRAPH_DATA_DIR=output/demo-data .venv/bin/python -m datagraph.main
```

Open the `vizUrl` printed by `scripts/demo_seed.py`. Note that the seed script
deletes and recreates its data directory (`output/demo-data` by default) on
every run. The API binds to `127.0.0.1:8080` by default. Set `DATAGRAPH_PORT`
or `DATA_GRAPH_PORT` to override the port, and `DATAGRAPH_DATA_DIR` or
`DATA_GRAPH_DATA_DIR` to choose the SQLite data directory. The server also
reads `DATAGRAPH_READ_ONLY`, `OPENAI_API_KEY`, and `DATAGRAPH_INLINE_CPU_RUNS`
(or `DATA_GRAPH_INLINE_CPU_RUNS`, boolean: run cluster/layout in-process
instead of a process pool); `scripts/ui_smoke.py` additionally reads
`DATAGRAPH_CHROME_BIN`. Nothing loads `.env` files automatically — export
variables in the shell; `.env.example` documents the server-side ones.
Developed on Python 3.14 (CI targets 3.14 with an explicit 3.12 fallback);
`datagraph/main.py` pins uvicorn to `loop="asyncio", http="h11"` to work
around a Python 3.14 venv transport bug.

The demo above is fully offline. Running the real pipeline (OpenAI embeddings
and topic labeling) requires `OPENAI_API_KEY` in the server's environment.

### Import externally generated vectors

External vectors can be imported without regenerating embeddings. QMD Memory Bundle v1
is the first supported adapter:

```sh
.venv/bin/python scripts/import_vectors.py \
  --format qmd-memory-v1 \
  --input ./qmd-memory-bundle \
  --dataset bill-memory
```

The command creates or updates the dataset graph and prints its succeeded external
embedding run and `vizUrl`. It does not require QMD, network access, or an embedding API.

### Internal Unblock Memory analysis worker

Unblock Memory can analyze its QMD index in place without importing or copying vectors.
Configure the plugin with this checkout's relocatable worker executable:

```text
/path/to/unblock-cluster/bin/unblock-memory-analysis
```

The plugin owns and supplies its per-agent QMD SQLite path when it launches the worker;
users and agents do not configure that database path. The worker clusters each exact active
chunk text once, deterministically retaining the lexicographically first `(hash, seq)`
occurrence. It writes only the latest derived clusters, memberships, exact-duplicate occurrence
mappings, outlier scores, representative ranks, and 2D coordinates into the same SQLite
database. QMD documents and vectors remain unchanged, and the worker does not call an
embedding or labeling provider.

The private worker interface accepts `--db`, an optional `--config-json` object, and an
optional `--collections-json` array of QMD collection names to analyze. The plugin may tune
UMAP with `space.method`, `nComponents`, `nNeighbors`, and `minDist`; tune
HDBSCAN with `minClusterSize`, `minSamples`, `clusterSelectionMethod`,
`clusterSelectionEpsilon`, and `allowSingleCluster`; and set `seed`. Unknown or invalid
properties are rejected. Cosine distance and the visualization layout remain internal.

Distinct collection/path records may share one content-addressed QMD vector without
losing either source's provenance or duplicating the binary payload.
See [the QMD Memory Bundle v1 contract](docs/qmd-memory-bundle-v1.md) for payload fields,
checksums, provenance, snapshot/versioning semantics, and the upstream exporter contract.

### UI Verification

The local smoke suite drives a real headless Chromium over CDP and assumes the
offline demo data is already seeded and served:

```sh
.venv/bin/python scripts/demo_seed.py
DATAGRAPH_DATA_DIR=output/demo-data .venv/bin/python -m datagraph.main
```

In another shell:

```sh
python scripts/ui_smoke.py
```

Set `DATAGRAPH_CHROME_BIN=/path/to/chrome-headless-shell` if the browser is not
under the Playwright cache or on `PATH` as `google-chrome`/`chromium`.
Screenshots are written to `output/ui-smoke/`.

## Serve Modes

Production/local backend mode:

```sh
npm run build
DATAGRAPH_DATA_DIR=data .venv/bin/python -m datagraph.main
```

The backend serves `dist/index.html` at `/`, static assets at `/assets/*`, and
the API under `/api/*`. `vizUrl` values use:

```txt
http://127.0.0.1:{port}/?graphId={graphId}&viewId={viewId}
```

Development mode:

```sh
DATAGRAPH_DATA_DIR=data .venv/bin/python -m datagraph.main
npm run dev
```

Vite serves the frontend on `127.0.0.1:4173` and proxies `/api` to the backend.

Read-only mode:

```sh
DATAGRAPH_READ_ONLY=1 DATAGRAPH_DATA_DIR=data .venv/bin/python -m datagraph.main
```

Read-only mode is for sharing a finished graph without letting users mutate it.
GETs, static assets, and `POST /api/graphs/:gid/evidence` remain available; all
other `POST`, `PATCH`, `PUT`, and `DELETE` endpoints return `403`.

Every GET route also accepts `HEAD`, so tunnel and uptime checks can probe
read endpoints without downloading the response body:

```sh
curl -I "http://127.0.0.1:8080/api/health"
curl -I "http://127.0.0.1:8080/api/graphs/$GRAPH_ID/views/$VIEW_ID/artifact"
```

## Checks

```sh
.venv/bin/pytest
.venv/bin/pytest -m "not slow"
.venv/bin/ruff check .
npm run check
npm run build
```

The default pytest command runs the full coverage-preserving suite, including
slow gates. Use `-m "not slow"` for the fast inner-loop tier; CI runs the full
suite in parallel with `pytest -n auto`. The manual 100k scale benchmark lives
in the [operations notes](docs/guide/operations.md).

## Guide

The operational and API documentation lives in `docs/guide/`:

- [Pipeline and data model](docs/guide/pipeline.md) — the agent contract, the
  minimum run pipeline, run cancellation, record validation, views and scopes,
  and the run model.
- [Extraction quality](docs/guide/extraction-quality.md) — pre-upload
  filtering, representation choices, reading noise, quality tuning loops, and
  decomposing large topics (including allowed `facetBy` values).
- [Summarize-then-embed](docs/guide/summarize-then-embed.md) — the optional
  per-record summarization flow, semantic junk gating, and cost planning.
- [Labeling](docs/guide/labeling.md) — labeling configuration, text sources,
  and label-run reports.
- [Evidence recipes](docs/guide/evidence.md) — one-call REST answers for
  trends, topic search, and question evidence.
- [Artifact endpoint and UI](docs/guide/artifact-and-ui.md) — the composed
  artifact response, caching and warnings, and frontend modes.
- [Privacy](docs/guide/privacy.md) — exactly which flows transmit customer
  text, and what to redact before upload.
- [Operations notes](docs/guide/operations.md) — local automation footguns and
  the scale benchmark.
- [QMD Memory Bundle v1 contract](docs/qmd-memory-bundle-v1.md) — the external
  vector import format.
