#!/usr/bin/env python3
import argparse
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
ENV_PATH = Path(os.environ.get("DATA_GRAPH_ENV", ROOT / ".env")).resolve()


def load_env_file(path=ENV_PATH):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()

DATA_ROOT = Path(os.environ.get("DATA_GRAPH_STORAGE", ROOT / "local-data")).resolve()
DB_PATH = Path(
    os.environ.get("DATA_GRAPH_DB", DATA_ROOT / "data-graph.sqlite3")
).resolve()
PUBLIC_ROOT = Path(os.environ.get("DATA_GRAPH_PUBLIC_ROOT", ROOT / "dist")).resolve()
PUBLIC_BASE_URL = os.environ.get("DATA_GRAPH_PUBLIC_BASE_URL", "").rstrip("/")
MAX_BODY_BYTES = int(os.environ.get("DATA_GRAPH_MAX_BODY_BYTES", str(8 * 1024 * 1024)))
PROCESS_DEBOUNCE_SECONDS = float(os.environ.get("DATA_GRAPH_PROCESS_DEBOUNCE_SECONDS", "2.0"))
SUPPORTED_TYPES = {"String", "Number", "Boolean", "Object", "Array"}
ID_PATTERN = re.compile(r"^dg_[A-Za-z0-9_-]{16,64}$")
GRAPH_ROUTE_PATTERN = r"/api/data-graph/(dg_[A-Za-z0-9_-]+)"
PROCESS_TIMERS = {}
PROCESS_TIMERS_LOCK = threading.Lock()


def public_url(path):
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{PUBLIC_BASE_URL}{path}" if PUBLIC_BASE_URL else path


def now_iso():
    return datetime.now(UTC).isoformat(timespec="seconds")


def json_dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def pretty_json(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def ensure_private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except PermissionError:
        pass


def init_storage():
    ensure_private_dir(DATA_ROOT)
    ensure_private_dir(DB_PATH.parent)
    with connect_db() as db:
        db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS data_sinks (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              config_json TEXT NOT NULL,
              status TEXT NOT NULL,
              latest_artifact_path TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS data_batches (
              id TEXT PRIMARY KEY,
              sink_id TEXT NOT NULL REFERENCES data_sinks(id) ON DELETE CASCADE,
              raw_path TEXT NOT NULL,
              row_count INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artifacts (
              id TEXT PRIMARY KEY,
              sink_id TEXT NOT NULL REFERENCES data_sinks(id) ON DELETE CASCADE,
              artifact_path TEXT NOT NULL,
              kind TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )


def connect_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def sink_dir(sink_id):
    if not ID_PATTERN.match(sink_id):
        raise ValueError("Invalid data graph id.")
    path = (DATA_ROOT / "sinks" / sink_id).resolve()
    if DATA_ROOT not in path.parents:
        raise ValueError("Invalid data graph path.")
    return path


def new_id(prefix):
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("config must be an object.")

    schema = config.get("dataSchema")
    if not isinstance(schema, dict) or not schema:
        raise ValueError("config.dataSchema must be a non-empty object.")

    for field, field_type in schema.items():
        if not isinstance(field, str) or not field:
            raise ValueError("Schema field names must be non-empty strings.")
        if field.startswith("__"):
            raise ValueError(f"Schema field '{field}' cannot start with '__'.")
        if not isinstance(field_type, str) or field_type not in SUPPORTED_TYPES:
            raise ValueError(
                f"Schema field '{field}' must use one of: {', '.join(sorted(SUPPORTED_TYPES))}."
            )

    grouping_fields = config.get("groupingFields")
    if not isinstance(grouping_fields, list) or not grouping_fields:
        raise ValueError("config.groupingFields must be a non-empty array.")
    for field in grouping_fields:
        if field not in schema:
            raise ValueError(f"Grouping field '{field}' does not exist in dataSchema.")

    for optional_field in ("titleField", "detailField"):
        value = config.get(optional_field)
        if value is not None and value not in schema:
            raise ValueError(f"config.{optional_field} must exist in dataSchema.")

    cluster = config.get("cluster")
    if cluster is not None and not isinstance(cluster, dict):
        raise ValueError("config.cluster must be an object when provided.")

    return {
        **config,
        "name": str(config.get("name") or "Data Atlas")[:120],
        "dataSchema": schema,
        "groupingFields": grouping_fields,
    }


def validate_rows(config, rows):
    if not isinstance(rows, list):
        raise ValueError("data must be an array.")
    schema = config["dataSchema"]
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"data[{index}] must be an object.")
        for field, field_type in schema.items():
            if field not in row or row[field] is None:
                raise ValueError(f"data[{index}].{field} is required.")
            if not value_matches_type(row[field], field_type):
                raise ValueError(f"data[{index}].{field} must be {field_type}.")
    return rows


def value_matches_type(value, field_type):
    if field_type == "String":
        return isinstance(value, str)
    if field_type == "Number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(
            value, bool
        )
    if field_type == "Boolean":
        return isinstance(value, bool)
    if field_type == "Object":
        return isinstance(value, dict)
    if field_type == "Array":
        return isinstance(value, list)
    return False


def load_sink(db, sink_id):
    row = db.execute("SELECT * FROM data_sinks WHERE id = ?", (sink_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "config": json.loads(row["config_json"]),
        "status": row["status"],
        "latestArtifactPath": row["latest_artifact_path"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def read_all_rows(db, sink_id):
    rows = []
    batches = db.execute(
        "SELECT raw_path FROM data_batches WHERE sink_id = ? ORDER BY created_at ASC",
        (sink_id,),
    ).fetchall()
    for batch in batches:
        batch_path = Path(batch["raw_path"])
        payload = json.loads(batch_path.read_text())
        rows.extend(payload.get("data", []))
    return rows


def write_artifact(db, sink, data):
    artifact_id = new_id("art")
    artifact_path = sink_dir(sink["id"]) / "processed" / f"{artifact_id}.json"
    ensure_private_dir(artifact_path.parent)
    payload = {"config": sink["config"], "data": data}
    artifact_path.write_text(pretty_json(payload))

    created_at = now_iso()
    db.execute(
        "INSERT INTO artifacts (id, sink_id, artifact_path, kind, created_at) VALUES (?, ?, ?, ?, ?)",
        (artifact_id, sink["id"], str(artifact_path), "latest", created_at),
    )
    db.execute(
        "UPDATE data_sinks SET latest_artifact_path = ?, status = ?, updated_at = ? WHERE id = ?",
        (str(artifact_path), "ready", created_at, sink["id"]),
    )
    return artifact_path


def mark_sink_processing(db, sink_id):
    db.execute(
        "UPDATE data_sinks SET status = ?, updated_at = ? WHERE id = ?",
        ("processing", now_iso(), sink_id),
    )


def schedule_artifact_rebuild(sink_id):
    cancel_scheduled_rebuild(sink_id)
    timer = threading.Timer(PROCESS_DEBOUNCE_SECONDS, process_pending_sink, args=(sink_id,))
    timer.daemon = True
    with PROCESS_TIMERS_LOCK:
        PROCESS_TIMERS[sink_id] = timer
    timer.start()


def cancel_scheduled_rebuild(sink_id):
    with PROCESS_TIMERS_LOCK:
        timer = PROCESS_TIMERS.pop(sink_id, None)
    if timer:
        timer.cancel()


def is_rebuild_scheduled(sink_id):
    with PROCESS_TIMERS_LOCK:
        timer = PROCESS_TIMERS.get(sink_id)
    return bool(timer and timer.is_alive())


def process_pending_sink(sink_id):
    with PROCESS_TIMERS_LOCK:
        PROCESS_TIMERS.pop(sink_id, None)
    try:
        with connect_db() as db:
            sink = load_sink(db, sink_id)
            if not sink:
                return
            write_artifact(db, sink, read_all_rows(db, sink_id))
            db.commit()
    except Exception as error:
        print(f"Failed to process {sink_id}: {error}", file=sys.stderr)
        try:
            with connect_db() as db:
                db.execute(
                    "UPDATE data_sinks SET status = ?, updated_at = ? WHERE id = ?",
                    ("error", now_iso(), sink_id),
                )
                db.commit()
        except Exception as nested_error:
            print(f"Failed to mark {sink_id} as error: {nested_error}", file=sys.stderr)


def api_help_payload():
    return {
        "service": "Data Graph",
        "description": "Create a data graph, append JSON rows, and view grouped records.",
        "publicBaseUrl": PUBLIC_BASE_URL or None,
        "auth": {
            "type": "bearer",
            "header": "Authorization: Bearer <token>",
            "requiredFor": [
                "POST /api/data-graph",
                "POST /api/data-graph/:id/data",
                "PATCH /api/data-graph/:id/schema",
                "DELETE /api/data-graph/:id/data",
            ],
        },
        "dataSchemaTypes": {
            "description": "Allowed values for each config.dataSchema field type.",
            "allowed": sorted(SUPPORTED_TYPES),
        },
        "endpoints": {
            "status": {
                "method": "GET",
                "url": "/api/status",
            },
            "createDataGraph": {
                "method": "POST",
                "url": "/api/data-graph",
                "payload": {
                    "config": {
                        "name": "Book Clusters",
                        "dataSchema": {
                            "bookName": "String",
                            "genre": "String",
                            "summary": "String",
                        },
                        "groupingFields": ["genre"],
                        "titleField": "bookName",
                        "detailField": "summary",
                    },
                    "data": [
                        {
                            "bookName": "Dune",
                            "genre": "Science Fiction",
                            "summary": "A desert planet power struggle.",
                        }
                    ],
                },
            },
            "getDataGraph": {
                "method": "GET",
                "url": "/api/data-graph/:id",
            },
            "getDataGraphStatus": {
                "method": "GET",
                "url": "/api/data-graph/:id/status",
            },
            "getDataGraphHelp": {
                "method": "GET",
                "url": "/api/data-graph/:id/help",
            },
            "appendRows": {
                "method": "POST",
                "url": "/api/data-graph/:id/data",
                "payload": {
                    "data": [
                        {
                            "bookName": "Dune",
                            "genre": "Science Fiction",
                            "summary": "A desert planet power struggle.",
                        }
                    ]
                },
            },
            "clearRows": {
                "method": "DELETE",
                "url": "/api/data-graph/:id/data",
            },
            "view": {
                "method": "GET",
                "url": "/clusters/:id",
            },
        },
        "notes": "\n".join(
            [
                "Every grouping field must exist in config.dataSchema.",
                "titleField and detailField are optional, but must exist in config.dataSchema when provided.",
                "POST /api/data-graph/:id/data appends rows; it does not overwrite existing rows.",
                "Appended rows are processed after a debounce window so rapid requests are batched.",
                "Use GET /api/data-graph/:id/status to check processing state and row counts.",
            ]
        ),
    }


def system_status_payload():
    with connect_db() as db:
        sink_count = db.execute("SELECT COUNT(*) FROM data_sinks").fetchone()[0]
        batch_count = db.execute("SELECT COUNT(*) FROM data_batches").fetchone()[0]
        row_count = db.execute("SELECT COALESCE(SUM(row_count), 0) FROM data_batches").fetchone()[0]
        artifact_count = db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    with PROCESS_TIMERS_LOCK:
        scheduled = sorted(PROCESS_TIMERS.keys())
    return {
        "ok": True,
        "service": "Data Graph",
        "time": now_iso(),
        "storage": "filesystem",
        "database": "sqlite",
        "dataGraphCount": sink_count,
        "batchCount": batch_count,
        "rowCount": row_count,
        "artifactCount": artifact_count,
        "scheduledProcessingCount": len(scheduled),
        "scheduledDataGraphIds": scheduled,
        "processDebounceSeconds": PROCESS_DEBOUNCE_SECONDS,
        "maxBodyBytes": MAX_BODY_BYTES,
        "publicBaseUrl": PUBLIC_BASE_URL or None,
    }


def sink_help_payload(sink):
    config = sink["config"]
    sample_row = sample_row_for_schema(config["dataSchema"])
    return {
        "dataGraphId": sink["id"],
        "name": sink["name"],
        "status": sink["status"],
        "schema": config["dataSchema"],
        "groupingFields": config["groupingFields"],
        "titleField": config.get("titleField"),
        "detailField": config.get("detailField"),
        "auth": {
            "type": "bearer",
            "header": "Authorization: Bearer <token>",
            "requiredForAppend": True,
        },
        "append": {
            "method": "POST",
            "url": public_url(f"/api/data-graph/{sink['id']}/data"),
            "contentType": "application/json",
            "body": {"data": [sample_row]},
            "behavior": "Rows are appended immediately, then the latest artifact is rebuilt after the debounce window.",
            "processDebounceSeconds": PROCESS_DEBOUNCE_SECONDS,
        },
        "statusCheck": {
            "method": "GET",
            "url": public_url(f"/api/data-graph/{sink['id']}/status"),
        },
        "latestArtifact": {
            "method": "GET",
            "url": public_url(f"/api/data-graph/{sink['id']}/artifact/latest"),
        },
        "clearRows": {
            "method": "DELETE",
            "url": public_url(f"/api/data-graph/{sink['id']}/data"),
            "authRequired": True,
        },
        "viewUrl": public_url(f"/clusters/{sink['id']}"),
    }


def sample_row_for_schema(schema):
    return {field: sample_value_for_type(field_type) for field, field_type in schema.items()}


def sample_value_for_type(field_type):
    if field_type == "String":
        return "example"
    if field_type == "Number":
        return 1
    if field_type == "Boolean":
        return True
    if field_type == "Object":
        return {"example": True}
    if field_type == "Array":
        return ["example"]
    return None


class DataGraphHandler(SimpleHTTPRequestHandler):
    server_version = "DataGraphLocal/0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC_ROOT), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache"
        )
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            if not self.require_auth():
                return
            return self.handle_api_get(parsed.path)
        if parsed.path.startswith("/clusters/"):
            return self.serve_index()
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and not self.require_auth():
            return
        if parsed.path == "/api/data-graph":
            return self.create_sink()
        match = re.fullmatch(f"{GRAPH_ROUTE_PATTERN}/data", parsed.path)
        if match:
            return self.ingest_data(match.group(1))
        return self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found.")

    def do_PATCH(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and not self.require_auth():
            return
        match = re.fullmatch(f"{GRAPH_ROUTE_PATTERN}/schema", parsed.path)
        if match:
            return self.update_schema(match.group(1))
        return self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found.")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and not self.require_auth():
            return
        match = re.fullmatch(f"{GRAPH_ROUTE_PATTERN}/data", parsed.path)
        if match:
            return self.clear_data(match.group(1))
        return self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found.")

    def handle_api_get(self, path):
        if path in ("/api/help", "/api"):
            return self.send_json(api_help_payload())

        if path == "/api/status":
            return self.send_json(system_status_payload())

        match = re.fullmatch(f"{GRAPH_ROUTE_PATTERN}$", path)
        if match:
            with connect_db() as db:
                sink = load_sink(db, match.group(1))
            if not sink:
                return self.send_error_json(
                    HTTPStatus.NOT_FOUND, "Data graph not found."
                )
            return self.send_json(sink)

        match = re.fullmatch(f"{GRAPH_ROUTE_PATTERN}/status", path)
        if match:
            return self.send_sink_status(match.group(1))

        match = re.fullmatch(f"{GRAPH_ROUTE_PATTERN}/help", path)
        if match:
            return self.send_sink_help(match.group(1))

        match = re.fullmatch(
            f"{GRAPH_ROUTE_PATTERN}/artifact/latest", path
        )
        if match:
            with connect_db() as db:
                sink = load_sink(db, match.group(1))
            if not sink:
                return self.send_error_json(
                    HTTPStatus.NOT_FOUND, "Data graph not found."
                )
            if not sink["latestArtifactPath"]:
                return self.send_json({"config": sink["config"], "data": []})
            artifact_path = Path(sink["latestArtifactPath"]).resolve()
            if DATA_ROOT not in artifact_path.parents or not artifact_path.exists():
                return self.send_error_json(HTTPStatus.NOT_FOUND, "Artifact not found.")
            return self.send_json(json.loads(artifact_path.read_text()))

        return self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found.")

    def send_sink_status(self, sink_id):
        with connect_db() as db:
            sink = load_sink(db, sink_id)
            if not sink:
                return self.send_error_json(
                    HTTPStatus.NOT_FOUND, "Data graph not found."
                )
            batch_count = db.execute(
                "SELECT COUNT(*) FROM data_batches WHERE sink_id = ?", (sink_id,)
            ).fetchone()[0]
            row_count = db.execute(
                "SELECT COALESCE(SUM(row_count), 0) FROM data_batches WHERE sink_id = ?",
                (sink_id,),
            ).fetchone()[0]
            artifact_count = db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE sink_id = ?", (sink_id,)
            ).fetchone()[0]

        return self.send_json(
            {
                "dataGraphId": sink_id,
                "name": sink["name"],
                "status": sink["status"],
                "processingScheduled": is_rebuild_scheduled(sink_id),
                "processDebounceSeconds": PROCESS_DEBOUNCE_SECONDS,
                "batchCount": batch_count,
                "rowCount": row_count,
                "artifactCount": artifact_count,
                "hasLatestArtifact": bool(sink["latestArtifactPath"]),
                "viewUrl": public_url(f"/clusters/{sink_id}"),
                "ingestUrl": public_url(f"/api/data-graph/{sink_id}/data"),
                "latestArtifactUrl": public_url(
                    f"/api/data-graph/{sink_id}/artifact/latest"
                ),
                "updatedAt": sink["updatedAt"],
            }
        )

    def send_sink_help(self, sink_id):
        with connect_db() as db:
            sink = load_sink(db, sink_id)
        if not sink:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Data graph not found.")
        return self.send_json(sink_help_payload(sink))

    def create_sink(self):
        try:
            payload = self.read_json_body()
            config = validate_config(payload.get("config"))
            rows = payload.get("data", [])
            validate_rows(config, rows)
        except ValueError as error:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))

        sink_id = new_id("dg")
        created_at = now_iso()
        base_dir = sink_dir(sink_id)
        ensure_private_dir(base_dir)
        ensure_private_dir(base_dir / "raw")
        ensure_private_dir(base_dir / "processed")

        with connect_db() as db:
            db.execute(
                """
                INSERT INTO data_sinks (id, name, config_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sink_id,
                    config["name"],
                    json_dumps(config),
                    "created",
                    created_at,
                    created_at,
                ),
            )
            sink = load_sink(db, sink_id)
            if rows:
                self.persist_batch(db, sink, rows)
                mark_sink_processing(db, sink_id)
            db.commit()
        if rows:
            schedule_artifact_rebuild(sink_id)

        return self.send_json(
            {
                "dataGraphId": sink_id,
                "viewUrl": public_url(f"/clusters/{sink_id}"),
                "ingestUrl": public_url(f"/api/data-graph/{sink_id}/data"),
                "latestArtifactUrl": public_url(
                    f"/api/data-graph/{sink_id}/artifact/latest"
                ),
            },
            HTTPStatus.CREATED,
        )

    def ingest_data(self, sink_id):
        with connect_db() as db:
            sink = load_sink(db, sink_id)
            if not sink:
                return self.send_error_json(
                    HTTPStatus.NOT_FOUND, "Data graph not found."
                )
            try:
                payload = self.read_json_body()
                rows = validate_rows(sink["config"], payload.get("data"))
            except ValueError as error:
                return self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))

            batch_id = self.persist_batch(db, sink, rows)
            mark_sink_processing(db, sink_id)
            db.commit()
        schedule_artifact_rebuild(sink_id)

        return self.send_json(
            {
                "dataGraphId": sink_id,
                "batchId": batch_id,
                "rowCount": len(rows),
                "status": "processing",
                "processAfterSeconds": PROCESS_DEBOUNCE_SECONDS,
                "viewUrl": public_url(f"/clusters/{sink_id}"),
            },
            HTTPStatus.CREATED,
        )

    def update_schema(self, sink_id):
        try:
            payload = self.read_json_body()
            config = validate_config(payload.get("config"))
        except ValueError as error:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))

        with connect_db() as db:
            sink = load_sink(db, sink_id)
            if not sink:
                return self.send_error_json(
                    HTTPStatus.NOT_FOUND, "Data graph not found."
                )
            existing_rows = read_all_rows(db, sink_id)
            try:
                validate_rows(config, existing_rows)
            except ValueError as error:
                return self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            updated_at = now_iso()
            db.execute(
                "UPDATE data_sinks SET name = ?, config_json = ?, updated_at = ? WHERE id = ?",
                (config["name"], json_dumps(config), updated_at, sink_id),
            )
            updated_sink = load_sink(db, sink_id)
            write_artifact(db, updated_sink, existing_rows)
            db.commit()

        return self.send_json(
            {"dataGraphId": sink_id, "viewUrl": public_url(f"/clusters/{sink_id}")}
        )

    def clear_data(self, sink_id):
        cancel_scheduled_rebuild(sink_id)
        with connect_db() as db:
            sink = load_sink(db, sink_id)
            if not sink:
                return self.send_error_json(
                    HTTPStatus.NOT_FOUND, "Data graph not found."
                )

            raw_paths = [
                Path(row["raw_path"]).resolve()
                for row in db.execute(
                    "SELECT raw_path FROM data_batches WHERE sink_id = ?", (sink_id,)
                ).fetchall()
            ]
            artifact_paths = [
                Path(row["artifact_path"]).resolve()
                for row in db.execute(
                    "SELECT artifact_path FROM artifacts WHERE sink_id = ?", (sink_id,)
                ).fetchall()
            ]

            for path in raw_paths + artifact_paths:
                if DATA_ROOT in path.parents and path.exists():
                    path.unlink()

            db.execute("DELETE FROM data_batches WHERE sink_id = ?", (sink_id,))
            db.execute("DELETE FROM artifacts WHERE sink_id = ?", (sink_id,))
            artifact_path = write_artifact(db, sink, [])
            db.commit()

        return self.send_json(
            {
                "dataGraphId": sink_id,
                "cleared": True,
                "rowCount": 0,
                "artifactPath": artifact_path.name,
                "viewUrl": public_url(f"/clusters/{sink_id}"),
            }
        )

    def persist_batch(self, db, sink, rows):
        batch_id = new_id("batch")
        batch_path = sink_dir(sink["id"]) / "raw" / f"{batch_id}.json"
        batch_path.write_text(pretty_json({"data": rows}))
        created_at = now_iso()
        db.execute(
            "INSERT INTO data_batches (id, sink_id, raw_path, row_count, created_at) VALUES (?, ?, ?, ?, ?)",
            (batch_id, sink["id"], str(batch_path), len(rows), created_at),
        )
        return batch_id

    def read_json_body(self):
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise ValueError("Content-Type must be application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid Content-Length.") from error
        if length <= 0:
            raise ValueError("Request body is required.")
        if length > MAX_BODY_BYTES:
            raise ValueError("Request body is too large.")
        body = self.rfile.read(length)
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSON: {error.msg}.") from error

    def require_auth(self):
        expected = os.environ.get("DATA_GRAPH_API_TOKEN")
        if not expected:
            self.send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "DATA_GRAPH_API_TOKEN is required before write APIs can be used.",
            )
            return False
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            self.send_error_json(
                HTTPStatus.UNAUTHORIZED, "Authorization must use a bearer token."
            )
            return False
        provided = header.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(provided, expected):
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid bearer token.")
            return False
        return True

    def serve_index(self):
        index_path = Path(self.directory) / "index.html"
        if not index_path.exists():
            return self.send_error_json(
                HTTPStatus.NOT_FOUND,
                "Build the frontend first with npm run build.",
            )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(index_path.read_bytes())

    def send_json(self, payload, status=HTTPStatus.OK):
        body = pretty_json(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"error": message}, status)


def main():
    parser = argparse.ArgumentParser(
        description="Run the local Data Graph API and static server."
    )
    parser.add_argument(
        "--host", default=os.environ.get("DATA_GRAPH_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("DATA_GRAPH_PORT", "8080"))
    )
    args = parser.parse_args()

    init_storage()
    httpd = ThreadingHTTPServer((args.host, args.port), DataGraphHandler)
    print(f"Data Graph local server: http://{args.host}:{args.port}")
    print(f"SQLite: {DB_PATH}")
    print(f"Storage: {DATA_ROOT}")
    if not os.environ.get("DATA_GRAPH_API_TOKEN"):
        print(
            "DATA_GRAPH_API_TOKEN is not set; write APIs will reject requests.",
            file=sys.stderr,
        )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
