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
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from processor import (
    ProcessingError,
    default_feature_fields,
    default_numeric_fields,
    process_records,
    resolve_processor_options,
)

ROOT = Path(__file__).resolve().parent
ENV_PATH = Path(os.environ.get("DATA_GRAPH_ENV", ROOT / ".env")).resolve()


def load_env_file(path=ENV_PATH):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()


class StoredDataError(RuntimeError):
    pass


def read_json_file(path, context):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise StoredDataError(f"{context} could not be read.") from error
    except json.JSONDecodeError as error:
        raise StoredDataError(f"{context} is not valid JSON.") from error

DATA_ROOT = Path(os.environ.get("DATA_GRAPH_STORAGE", ROOT / "local-data")).resolve()
DB_PATH = Path(
    os.environ.get("DATA_GRAPH_DB", DATA_ROOT / "data-graph.sqlite3")
).resolve()
PUBLIC_ROOT = Path(os.environ.get("DATA_GRAPH_PUBLIC_ROOT", ROOT / "dist")).resolve()
PUBLIC_BASE_URL = os.environ.get("DATA_GRAPH_PUBLIC_BASE_URL", "").rstrip("/")
MAX_BODY_BYTES = int(os.environ.get("DATA_GRAPH_MAX_BODY_BYTES", str(8 * 1024 * 1024)))
PROCESS_DEBOUNCE_SECONDS = float(os.environ.get("DATA_GRAPH_PROCESS_DEBOUNCE_SECONDS", "2.0"))
SUPPORTED_TYPES = {"String", "Number", "Boolean", "Object", "Array"}
CONFIG_KEYS = {
    "name",
    "description",
    "source",
    "dataSchema",
    "groupingFields",
    "titleField",
    "detailField",
    "imageField",
    "recordIdField",
    "pipeline",
    "cluster",
}
TEXT_FEATURE_METHODS = {"tfidf", "embedding"}
EMBEDDING_PROVIDERS = {"openai"}
PIPELINE_FILTER_OPS = {"equals", "notEquals", "contains", "notContains", "exists", "notExists"}
PIPELINE_TRANSFORM_TYPES = {
    "copyField",
    "renameField",
    "setField",
    "trim",
    "lowercase",
    "uppercase",
}
CLUSTER_CONFIG_KEYS = {
    "method",
    "featureFields",
    "numericFields",
    "minClusterSize",
    "textFeatureMethod",
    "embeddingProvider",
    "embeddingModel",
    "embeddingDimensions",
    "labelStrategy",
    "labelField",
    "labelOverrides",
}
SECRET_CONFIG_VALUE_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
ID_PATTERN = re.compile(r"^dg_[A-Za-z0-9_-]{16,64}$")
PROCESS_TIMERS = {}
PROCESS_TIMERS_LOCK = threading.Lock()
SAMPLE_MANIFEST = read_json_file(ROOT / "sample-manifest.json", "Sample manifest")
PUBLIC_SAMPLE_FILES = {
    Path(sample_name).name
    for sample_name in SAMPLE_MANIFEST.get("publicSampleFiles", [])
}


class RequestBodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app, max_body_bytes):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = dict(scope.get("headers") or []).get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_body_bytes:
                    await self.send_json_error(
                        send, 413, "Request body is too large."
                    )
                    return
            except ValueError:
                await self.send_json_error(send, 400, "Invalid Content-Length.")
                return

        received_bytes = 0
        response_started = False

        async def limited_receive():
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    raise RequestBodyTooLarge()
            return message

        async def limited_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except RequestBodyTooLarge:
            if response_started:
                raise
            await self.send_json_error(send, 413, "Request body is too large.")

    @staticmethod
    async def send_json_error(send, status_code, message):
        payload = json_dumps({"error": message}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"same-origin"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


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


def positive_int(value, field_name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


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
              source_revision INTEGER NOT NULL DEFAULT 0,
              processed_revision INTEGER NOT NULL DEFAULT 0,
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

            CREATE TABLE IF NOT EXISTS embedding_cache (
              provider TEXT NOT NULL,
              model TEXT NOT NULL,
              dimensions INTEGER NOT NULL DEFAULT 0,
              text_hash TEXT NOT NULL,
              embedding_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (provider, model, dimensions, text_hash)
            );
            """
        )
        ensure_data_sink_revision_columns(db)


def ensure_data_sink_revision_columns(db):
    columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(data_sinks)").fetchall()
    }
    if "source_revision" not in columns:
        db.execute(
            "ALTER TABLE data_sinks ADD COLUMN source_revision INTEGER NOT NULL DEFAULT 0"
        )
    if "processed_revision" not in columns:
        db.execute(
            "ALTER TABLE data_sinks ADD COLUMN processed_revision INTEGER NOT NULL DEFAULT 0"
        )


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect_db():
    db = sqlite3.connect(DB_PATH, factory=ClosingConnection)
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
    reject_secret_config_keys(config, "config")
    reject_secret_config_values(config, "config")
    unknown_keys = sorted(set(config) - CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(
            "config contains unsupported fields: "
            + ", ".join(unknown_keys)
            + "."
        )

    schema = config.get("dataSchema")
    if not isinstance(schema, dict) or not schema:
        raise ValueError("config.dataSchema must be a non-empty object.")
    reject_secret_config_keys(schema, "config.dataSchema")

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

    for optional_field in ("titleField", "detailField", "imageField", "recordIdField"):
        value = config.get(optional_field)
        if value is not None and value not in schema:
            raise ValueError(f"config.{optional_field} must exist in dataSchema.")

    for text_field in ("description", "source"):
        value = config.get(text_field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"config.{text_field} must be a string.")

    pipeline = validate_pipeline_config(config.get("pipeline"), schema)
    cluster = validate_cluster_config(config.get("cluster"), schema)

    normalized = {
        "name": str(config.get("name") or "Data Atlas")[:120],
        "dataSchema": schema,
        "groupingFields": grouping_fields,
        "cluster": cluster,
    }
    for optional_field in (
        "description",
        "source",
        "titleField",
        "detailField",
        "imageField",
        "recordIdField",
    ):
        if optional_field in config:
            normalized[optional_field] = config.get(optional_field)
    if pipeline:
        normalized["pipeline"] = pipeline
    return normalized


def validate_pipeline_config(pipeline, schema):
    if pipeline is None:
        return {}
    if not isinstance(pipeline, dict):
        raise ValueError("config.pipeline must be an object when provided.")
    reject_secret_config_keys(pipeline, "config.pipeline")

    unknown_keys = sorted(set(pipeline) - {"filters", "transforms"})
    if unknown_keys:
        raise ValueError(
            "config.pipeline contains unsupported fields: "
            + ", ".join(unknown_keys)
            + "."
        )

    normalized = {}
    filters = pipeline.get("filters", [])
    if filters is None:
        filters = []
    if not isinstance(filters, list):
        raise ValueError("config.pipeline.filters must be an array.")
    normalized_filters = []
    for index, item in enumerate(filters):
        if not isinstance(item, dict):
            raise ValueError(f"config.pipeline.filters[{index}] must be an object.")
        reject_secret_config_keys(item, f"config.pipeline.filters[{index}]")
        unknown_filter_keys = sorted(set(item) - {"field", "op", "value"})
        if unknown_filter_keys:
            raise ValueError(
                f"config.pipeline.filters[{index}] contains unsupported fields: "
                + ", ".join(unknown_filter_keys)
                + "."
            )
        field = item.get("field")
        if not isinstance(field, str) or not field:
            raise ValueError(f"config.pipeline.filters[{index}].field must be a non-empty string.")
        op = item.get("op", "equals")
        if not isinstance(op, str) or op not in PIPELINE_FILTER_OPS:
            raise ValueError(
                f"config.pipeline.filters[{index}].op must be one of: "
                + ", ".join(sorted(PIPELINE_FILTER_OPS))
                + "."
            )
        normalized_item = {"field": field, "op": op}
        if "value" in item:
            normalized_item["value"] = item["value"]
        normalized_filters.append(normalized_item)
    if normalized_filters:
        normalized["filters"] = normalized_filters

    transforms = pipeline.get("transforms", [])
    if transforms is None:
        transforms = []
    if not isinstance(transforms, list):
        raise ValueError("config.pipeline.transforms must be an array.")
    normalized_transforms = []
    for index, item in enumerate(transforms):
        if not isinstance(item, dict):
            raise ValueError(f"config.pipeline.transforms[{index}] must be an object.")
        reject_secret_config_keys(item, f"config.pipeline.transforms[{index}]")
        transform_type = item.get("type")
        if not isinstance(transform_type, str) or transform_type not in PIPELINE_TRANSFORM_TYPES:
            raise ValueError(
                f"config.pipeline.transforms[{index}].type must be one of: "
                + ", ".join(sorted(PIPELINE_TRANSFORM_TYPES))
                + "."
            )
        normalized_item = {"type": transform_type}
        if transform_type in {"copyField", "renameField"}:
            unknown_transform_keys = sorted(set(item) - {"type", "from", "to"})
            if unknown_transform_keys:
                raise ValueError(
                    f"config.pipeline.transforms[{index}] contains unsupported fields: "
                    + ", ".join(unknown_transform_keys)
                    + "."
                )
            source = item.get("from")
            target = item.get("to")
            if not isinstance(source, str) or not source:
                raise ValueError(f"config.pipeline.transforms[{index}].from must be a non-empty string.")
            if target not in schema:
                raise ValueError(f"config.pipeline.transforms[{index}].to must exist in dataSchema.")
            normalized_item.update({"from": source, "to": target})
        elif transform_type == "setField":
            unknown_transform_keys = sorted(set(item) - {"type", "field", "value"})
            if unknown_transform_keys:
                raise ValueError(
                    f"config.pipeline.transforms[{index}] contains unsupported fields: "
                    + ", ".join(unknown_transform_keys)
                    + "."
                )
            field = item.get("field")
            if field not in schema:
                raise ValueError(f"config.pipeline.transforms[{index}].field must exist in dataSchema.")
            value = item.get("value")
            if value is None or not value_matches_type(value, schema[field]):
                raise ValueError(
                    f"config.pipeline.transforms[{index}].value must be {schema[field]}."
                )
            normalized_item.update({"field": field, "value": value})
        else:
            unknown_transform_keys = sorted(set(item) - {"type", "field"})
            if unknown_transform_keys:
                raise ValueError(
                    f"config.pipeline.transforms[{index}] contains unsupported fields: "
                    + ", ".join(unknown_transform_keys)
                    + "."
                )
            field = item.get("field")
            if field not in schema:
                raise ValueError(f"config.pipeline.transforms[{index}].field must exist in dataSchema.")
            if schema.get(field) != "String":
                raise ValueError(f"config.pipeline.transforms[{index}].field must be a String field.")
            normalized_item["field"] = field
        normalized_transforms.append(normalized_item)
    if normalized_transforms:
        normalized["transforms"] = normalized_transforms
    return normalized


def validate_cluster_config(cluster, schema):
    if cluster is None:
        return {}
    if not isinstance(cluster, dict):
        raise ValueError("config.cluster must be an object when provided.")
    reject_secret_config_keys(cluster, "config.cluster")

    normalized = dict(cluster)
    unknown_keys = sorted(set(normalized) - CLUSTER_CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(
            "config.cluster contains unsupported fields: "
            + ", ".join(unknown_keys)
            + "."
        )

    for field_name in ("featureFields", "numericFields"):
        fields = normalized.get(field_name)
        if fields is None:
            continue
        if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
            raise ValueError(f"config.cluster.{field_name} must be an array of field names.")
        unknown = [field for field in fields if field not in schema]
        if unknown:
            raise ValueError(
                f"config.cluster.{field_name} contains unknown fields: {', '.join(unknown)}."
            )

    if "numericFields" in normalized:
        non_numeric = [
            field
            for field in normalized["numericFields"]
            if schema.get(field) != "Number"
        ]
        if non_numeric:
            raise ValueError(
                "config.cluster.numericFields can only include Number fields: "
                + ", ".join(non_numeric)
                + "."
            )

    text_feature_method = normalized.get("textFeatureMethod")
    if text_feature_method is not None:
        if not isinstance(text_feature_method, str):
            raise ValueError("config.cluster.textFeatureMethod must be a string.")
        text_feature_method = text_feature_method.strip().lower()
        if text_feature_method not in TEXT_FEATURE_METHODS:
            raise ValueError(
                "config.cluster.textFeatureMethod must be one of: "
                + ", ".join(sorted(TEXT_FEATURE_METHODS))
                + "."
            )
        normalized["textFeatureMethod"] = text_feature_method

    embedding_provider = normalized.get("embeddingProvider")
    if embedding_provider is not None:
        if not isinstance(embedding_provider, str):
            raise ValueError("config.cluster.embeddingProvider must be a string.")
        embedding_provider = embedding_provider.strip().lower()
        if embedding_provider not in EMBEDDING_PROVIDERS:
            raise ValueError(
                "config.cluster.embeddingProvider must be one of: "
                + ", ".join(sorted(EMBEDDING_PROVIDERS))
                + "."
            )
        normalized["embeddingProvider"] = embedding_provider

    embedding_model = normalized.get("embeddingModel")
    if embedding_model is not None:
        if not isinstance(embedding_model, str) or not embedding_model.strip():
            raise ValueError("config.cluster.embeddingModel must be a non-empty string.")
        normalized["embeddingModel"] = embedding_model.strip()

    if "embeddingDimensions" in normalized and normalized["embeddingDimensions"] is not None:
        normalized["embeddingDimensions"] = positive_int(
            normalized["embeddingDimensions"], "config.cluster.embeddingDimensions"
        )

    if "minClusterSize" in normalized and normalized["minClusterSize"] is not None:
        normalized["minClusterSize"] = positive_int(
            normalized["minClusterSize"], "config.cluster.minClusterSize"
        )

    label_strategy = normalized.get("labelStrategy")
    if label_strategy is not None:
        if not isinstance(label_strategy, str):
            raise ValueError("config.cluster.labelStrategy must be a string.")
        label_strategy = label_strategy.strip()
        allowed = {"groupingField", "labelField", "clusterId"}
        if label_strategy not in allowed:
            raise ValueError(
                "config.cluster.labelStrategy must be one of: "
                + ", ".join(sorted(allowed))
                + "."
            )
        normalized["labelStrategy"] = label_strategy

    label_field = normalized.get("labelField")
    if label_field is not None:
        if label_field not in schema:
            raise ValueError("config.cluster.labelField must exist in dataSchema.")
        normalized["labelField"] = label_field

    label_overrides = normalized.get("labelOverrides")
    if label_overrides is not None:
        if not isinstance(label_overrides, dict):
            raise ValueError("config.cluster.labelOverrides must be an object.")
        normalized["labelOverrides"] = {
            str(key): str(value)[:120]
            for key, value in label_overrides.items()
            if value not in (None, "")
        }

    return normalized


def reject_secret_config_keys(config, path):
    for key in config:
        if not isinstance(key, str):
            continue
        normalized = re.sub(r"[^a-z0-9]", "", key.lower())
        is_secret_key = (
            normalized
            in {
                "apikey",
                "apitoken",
                "key",
                "openaiapikey",
                "openaikey",
                "openaisecret",
                "secret",
                "token",
            }
            or normalized.endswith("apikey")
            or normalized.endswith("apitoken")
            or normalized.endswith("secret")
            or normalized.endswith("token")
            or (normalized.endswith("key") and ("api" in normalized or "openai" in normalized))
        )
        if is_secret_key:
            raise ValueError(f"{path}.{key} must not contain API keys or tokens; use .env.")


def reject_secret_config_values(value, path):
    if isinstance(value, str):
        if SECRET_CONFIG_VALUE_PATTERN.search(value):
            raise ValueError(f"{path} must not contain API keys or tokens; use .env.")
        return
    if isinstance(value, dict):
        for key, nested_value in value.items():
            nested_path = f"{path}.{key}" if isinstance(key, str) else path
            reject_secret_config_values(nested_value, nested_path)
        return
    if isinstance(value, list):
        for index, nested_value in enumerate(value):
            reject_secret_config_values(nested_value, f"{path}[{index}]")


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


def validate_raw_rows(rows):
    if not isinstance(rows, list):
        raise ValueError("data must be an array.")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"data[{index}] must be an object.")
    return rows


def transformed_rows_for_config(config, rows):
    raw_rows = validate_raw_rows(rows)
    transformed_rows, pipeline_metadata = apply_pipeline(config, raw_rows)
    validate_rows(config, transformed_rows)
    return transformed_rows, pipeline_metadata


def apply_pipeline(config, rows):
    pipeline = config.get("pipeline") or {}
    filters = pipeline.get("filters") or []
    transforms = pipeline.get("transforms") or []
    output = []
    filtered_count = 0
    for row in rows:
        next_row = dict(row)
        for transform in transforms:
            apply_transform(next_row, transform)
        if all(filter_matches(next_row, item) for item in filters):
            output.append(next_row)
        else:
            filtered_count += 1
    return output, {
        "enabled": bool(filters or transforms),
        "inputRecordCount": len(rows),
        "outputRecordCount": len(output),
        "filteredRecordCount": filtered_count,
        "transformCount": len(transforms),
        "filterCount": len(filters),
    }


def apply_transform(row, transform):
    transform_type = transform["type"]
    if transform_type == "copyField":
        if transform["from"] in row:
            row[transform["to"]] = row.get(transform["from"])
        return
    if transform_type == "renameField":
        if transform["from"] in row:
            row[transform["to"]] = row.pop(transform["from"])
        return
    if transform_type == "setField":
        row[transform["field"]] = transform.get("value")
        return

    field = transform["field"]
    value = row.get(field)
    if value is None:
        return
    text = str(value)
    if transform_type == "trim":
        row[field] = text.strip()
    elif transform_type == "lowercase":
        row[field] = text.lower()
    elif transform_type == "uppercase":
        row[field] = text.upper()


def filter_matches(row, item):
    op = item["op"]
    value = row.get(item["field"])
    expected = item.get("value")
    if op == "exists":
        return value not in (None, "")
    if op == "notExists":
        return value in (None, "")
    if op == "equals":
        return value == expected
    if op == "notEquals":
        return value != expected
    contains = value_contains(value, expected)
    if op == "contains":
        return contains
    if op == "notContains":
        return not contains
    return True


def value_contains(value, expected):
    if value is None:
        return False
    if isinstance(value, list):
        return expected in value
    return str(expected).casefold() in str(value).casefold()


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
    try:
        config = json.loads(row["config_json"])
    except json.JSONDecodeError as error:
        raise StoredDataError(f"Stored config for {sink_id} is not valid JSON.") from error
    return {
        "id": row["id"],
        "name": row["name"],
        "config": config,
        "status": row["status"],
        "latestArtifactPath": row["latest_artifact_path"],
        "sourceRevision": row["source_revision"],
        "processedRevision": row["processed_revision"],
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
        payload = read_json_file(batch_path, "Stored data batch")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise StoredDataError("Stored data batch must contain a data array.")
        rows.extend(payload["data"])
    return rows


class SqliteEmbeddingCache:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def dimensions_value(dimensions):
        return int(dimensions or 0)

    def get_many(self, provider, model, dimensions, text_hashes):
        if not text_hashes:
            return {}
        self.db.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS embedding_cache_lookup (
              text_hash TEXT PRIMARY KEY
            )
            """
        )
        self.db.execute("DELETE FROM embedding_cache_lookup")
        try:
            self.db.executemany(
                "INSERT OR IGNORE INTO embedding_cache_lookup (text_hash) VALUES (?)",
                ((text_hash,) for text_hash in text_hashes),
            )
            rows = self.db.execute(
                """
                SELECT embedding_cache.text_hash, embedding_cache.embedding_json
                FROM embedding_cache
                INNER JOIN embedding_cache_lookup
                  ON embedding_cache_lookup.text_hash = embedding_cache.text_hash
                WHERE provider = ?
                  AND model = ?
                  AND dimensions = ?
                """,
                (
                    provider,
                    model,
                    self.dimensions_value(dimensions),
                ),
            ).fetchall()
        finally:
            self.db.execute("DELETE FROM embedding_cache_lookup")
        embeddings = {}
        for row in rows:
            try:
                embeddings[row["text_hash"]] = json.loads(row["embedding_json"])
            except json.JSONDecodeError:
                continue
        return embeddings

    def set_many(self, provider, model, dimensions, embeddings_by_hash):
        if not embeddings_by_hash:
            return
        created_at = now_iso()
        self.db.executemany(
            """
            INSERT OR REPLACE INTO embedding_cache
              (provider, model, dimensions, text_hash, embedding_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    provider,
                    model,
                    self.dimensions_value(dimensions),
                    text_hash,
                    json_dumps(embedding),
                    created_at,
                )
                for text_hash, embedding in embeddings_by_hash.items()
            ],
        )


def embedding_cache_stats(db):
    return {
        "embeddingCacheEntryCount": db.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0],
    }


def safe_processor_settings(config=None):
    try:
        resolved = resolve_processor_options(config or {"cluster": {}})
    except ProcessingError as error:
        return {
            "error": str(error),
            "embeddingConfigured": bool(os.environ.get("OPENAI_API_KEY")),
        }
    return {
        "textFeatureMethod": resolved["textFeatureMethod"],
        "embeddingProvider": resolved["embeddingProvider"],
        "embeddingModel": resolved["embeddingModel"],
        "embeddingDimensions": resolved["embeddingDimensions"],
        "embeddingBatchSize": resolved["embeddingBatchSize"],
        "embeddingTimeoutSeconds": resolved["embeddingTimeoutSeconds"],
        "embeddingConfigured": bool(os.environ.get("OPENAI_API_KEY")),
    }


def ensure_processing_ready(config):
    try:
        resolved = resolve_processor_options(config)
    except ProcessingError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if resolved["textFeatureMethod"] == "embedding" and not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(
            status_code=400,
            detail="OPENAI_API_KEY is required when textFeatureMethod is embedding.",
        )


def process_sink_records(db, sink, rows):
    processed_input_rows, pipeline_metadata = transformed_rows_for_config(sink["config"], rows)
    metadata = {"pipeline": pipeline_metadata}
    cache = SqliteEmbeddingCache(db)
    processed_rows = process_records(
        sink,
        processed_input_rows,
        embedding_cache=cache,
        metadata=metadata,
    )
    return processed_rows, metadata


def write_artifact(db, sink, data, expected_revision=None, processor_metadata=None):
    revision = (
        expected_revision
        if expected_revision is not None
        else sink.get("sourceRevision", 0)
    )
    current_revision = db.execute(
        "SELECT source_revision FROM data_sinks WHERE id = ?", (sink["id"],)
    ).fetchone()
    if (
        expected_revision is not None
        and current_revision
        and current_revision["source_revision"] != expected_revision
    ):
        return None

    artifact_id = new_id("art")
    artifact_path = sink_dir(sink["id"]) / "processed" / f"{artifact_id}.json"
    ensure_private_dir(artifact_path.parent)
    payload = {
        "config": sink["config"],
        "layout": {
            "method": "PaCMAP",
            "clusterMethod": "HDBSCAN",
            **(processor_metadata or {}),
        },
        "data": data,
    }
    artifact_path.write_text(pretty_json(payload), encoding="utf-8")

    created_at = now_iso()
    db.execute(
        "INSERT INTO artifacts (id, sink_id, artifact_path, kind, created_at) VALUES (?, ?, ?, ?, ?)",
        (artifact_id, sink["id"], str(artifact_path), "latest", created_at),
    )
    result = db.execute(
        """
        UPDATE data_sinks
        SET latest_artifact_path = ?,
            status = ?,
            updated_at = ?,
            processed_revision = ?
        WHERE id = ? AND source_revision = ?
        """,
        (str(artifact_path), "ready", created_at, revision, sink["id"], revision),
    )
    if result.rowcount != 1:
        db.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
        try:
            artifact_path.unlink()
        except FileNotFoundError:
            pass
        return None
    return artifact_path


def mark_sink_processing(db, sink_id):
    updated_at = now_iso()
    db.execute(
        """
        UPDATE data_sinks
        SET status = ?,
            updated_at = ?,
            source_revision = source_revision + 1
        WHERE id = ?
        """,
        ("processing", updated_at, sink_id),
    )
    row = db.execute(
        "SELECT source_revision FROM data_sinks WHERE id = ?", (sink_id,)
    ).fetchone()
    return row["source_revision"] if row else None


def schedule_artifact_rebuild(sink_id, revision):
    cancel_scheduled_rebuild(sink_id)
    timer = threading.Timer(
        PROCESS_DEBOUNCE_SECONDS, process_pending_sink, args=(sink_id, revision)
    )
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


def process_pending_sink(sink_id, expected_revision):
    with PROCESS_TIMERS_LOCK:
        PROCESS_TIMERS.pop(sink_id, None)
    try:
        with connect_db() as db:
            sink = load_sink(db, sink_id)
            if not sink:
                return
            rows = read_all_rows(db, sink_id)
            processed_rows, processor_metadata = process_sink_records(db, sink, rows)
            write_artifact(
                db,
                sink,
                processed_rows,
                expected_revision=expected_revision,
                processor_metadata=processor_metadata,
            )
            db.commit()
    except Exception as error:
        print(f"Failed to process {sink_id}: {error}", file=sys.stderr)
        try:
            with connect_db() as db:
                db.execute(
                    """
                    UPDATE data_sinks
                    SET status = ?, updated_at = ?
                    WHERE id = ? AND source_revision = ?
                    """,
                    ("error", now_iso(), sink_id, expected_revision),
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
                            "bookId": "String",
                            "bookName": "String",
                            "genre": "String",
                            "summary": "String",
                            "archived": "Boolean",
                        },
                        "groupingFields": ["genre"],
                        "recordIdField": "bookId",
                        "titleField": "bookName",
                        "detailField": "summary",
                        "pipeline": {
                            "transforms": [
                                {"type": "trim", "field": "bookName"},
                                {"type": "copyField", "from": "sourceTicketId", "to": "bookId"},
                            ],
                            "filters": [
                                {"field": "archived", "op": "notEquals", "value": True}
                            ],
                        },
                        "cluster": {
                            "method": "PaCMAP+HDBSCAN",
                            "textFeatureMethod": "tfidf",
                            "featureFields": ["bookName", "summary"],
                            "numericFields": [],
                            "embeddingProvider": "openai",
                            "embeddingModel": "text-embedding-3-small",
                            "embeddingDimensions": 512,
                            "minClusterSize": 3,
                            "labelStrategy": "labelField",
                            "labelField": "genre",
                            "labelOverrides": {"-1": "Needs review"},
                        },
                    },
                    "data": [
                        {
                            "sourceTicketId": "BOOK-001",
                            "bookName": "Dune",
                            "genre": "Science Fiction",
                            "summary": "A desert planet power struggle.",
                            "archived": False,
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
                            "sourceTicketId": "BOOK-001",
                            "bookName": "Dune",
                            "genre": "Science Fiction",
                            "summary": "A desert planet power struggle.",
                            "archived": False,
                        }
                    ]
                },
            },
            "clearRows": {
                "method": "DELETE",
                "url": "/api/data-graph/:id/data",
            },
            "searchRecords": {
                "method": "GET",
                "url": "/api/data-graph/:id/records/search?q=<record id or text>",
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
                "Processing uses PaCMAP for 2D layout and HDBSCAN for cluster labels.",
                "Optional config.cluster.textFeatureMethod can be tfidf or embedding.",
                "Embedding mode uses OPENAI_API_KEY from the server environment; API keys are never stored in graph config.",
                "Optional config.pipeline.transforms and filters run before validation and clustering while raw batches remain unchanged.",
                "Optional config.recordIdField controls record URL/search identity.",
                "Optional config.cluster.featureFields, numericFields, embeddingModel, embeddingDimensions, minClusterSize, labelStrategy, labelField, and labelOverrides can tune processing.",
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
        cache_stats = embedding_cache_stats(db)
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
        "processor": safe_processor_settings(),
        **cache_stats,
    }


def sink_help_payload(sink):
    config = sink["config"]
    sample_row = sample_row_for_schema(config["dataSchema"])
    processor_settings = safe_processor_settings(config)
    return {
        "dataGraphId": sink["id"],
        "name": sink["name"],
        "status": sink["status"],
        "schema": config["dataSchema"],
        "groupingFields": config["groupingFields"],
        "titleField": config.get("titleField"),
        "detailField": config.get("detailField"),
        "recordIdField": config.get("recordIdField"),
        "pipeline": config.get("pipeline", {}),
        "processor": {
            "layoutMethod": "PaCMAP",
            "clusterMethod": "HDBSCAN",
            "textFeatureMethod": processor_settings.get("textFeatureMethod"),
            "featureFields": (config.get("cluster") or {}).get("featureFields", default_feature_fields(config)),
            "numericFields": (config.get("cluster") or {}).get("numericFields", default_numeric_fields(config)),
            "minClusterSize": (config.get("cluster") or {}).get("minClusterSize"),
            "labelStrategy": (config.get("cluster") or {}).get("labelStrategy", "groupingField"),
            "labelField": (config.get("cluster") or {}).get("labelField"),
            "labelOverrideCount": len((config.get("cluster") or {}).get("labelOverrides", {})),
            "embeddingProvider": processor_settings.get("embeddingProvider"),
            "embeddingModel": processor_settings.get("embeddingModel"),
            "embeddingDimensions": processor_settings.get("embeddingDimensions"),
            "embeddingConfigured": processor_settings.get("embeddingConfigured"),
        },
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
        "searchRecords": {
            "method": "GET",
            "url": public_url(f"/api/data-graph/{sink['id']}/records/search?q=<record id or text>"),
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


@asynccontextmanager
async def lifespan(app):
    init_storage()
    yield


app = FastAPI(title="Data Graph", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(BodyLimitMiddleware, max_body_bytes=MAX_BODY_BYTES)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse({"error": "Invalid request body."}, status_code=400)


@app.exception_handler(StoredDataError)
async def stored_data_exception_handler(request: Request, exc: StoredDataError):
    return JSONResponse({"error": str(exc)}, status_code=500)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = (
        "no-store" if request.url.path.startswith("/api/") else "no-cache"
    )
    return response


def require_auth(authorization: str = Header(default="")):
    expected = os.environ.get("DATA_GRAPH_API_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="DATA_GRAPH_API_TOKEN is required before APIs can be used.",
        )
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization must use a bearer token.",
        )
    provided = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token.",
        )


def persist_batch(db, sink, rows):
    batch_id = new_id("batch")
    batch_path = sink_dir(sink["id"]) / "raw" / f"{batch_id}.json"
    batch_path.write_text(pretty_json({"data": rows}), encoding="utf-8")
    created_at = now_iso()
    db.execute(
        "INSERT INTO data_batches (id, sink_id, raw_path, row_count, created_at) VALUES (?, ?, ?, ?, ?)",
        (batch_id, sink["id"], str(batch_path), len(rows), created_at),
    )
    return batch_id


def get_data_graph_status_payload(graph_id):
    with connect_db() as db:
        sink = load_sink(db, graph_id)
        if not sink:
            raise HTTPException(status_code=404, detail="Data graph not found.")
        batch_count = db.execute(
            "SELECT COUNT(*) FROM data_batches WHERE sink_id = ?", (graph_id,)
        ).fetchone()[0]
        row_count = db.execute(
            "SELECT COALESCE(SUM(row_count), 0) FROM data_batches WHERE sink_id = ?",
            (graph_id,),
        ).fetchone()[0]
        artifact_count = db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE sink_id = ?", (graph_id,)
        ).fetchone()[0]

    return {
        "dataGraphId": graph_id,
        "name": sink["name"],
        "status": sink["status"],
        "processingScheduled": is_rebuild_scheduled(graph_id),
        "processDebounceSeconds": PROCESS_DEBOUNCE_SECONDS,
        "batchCount": batch_count,
        "rowCount": row_count,
        "artifactCount": artifact_count,
        "hasLatestArtifact": bool(sink["latestArtifactPath"]),
        "viewUrl": public_url(f"/clusters/{graph_id}"),
        "ingestUrl": public_url(f"/api/data-graph/{graph_id}/data"),
        "latestArtifactUrl": public_url(f"/api/data-graph/{graph_id}/artifact/latest"),
        "processor": safe_processor_settings(sink["config"]),
        "updatedAt": sink["updatedAt"],
    }


def record_identity_fields(config):
    fields = []
    if config.get("recordIdField"):
        fields.append(config["recordIdField"])
    for field in ("id", "ticketId", "sourceTicketId", "sourceId", "issueId", "key"):
        if field not in fields:
            fields.append(field)
    return fields


def record_identity(record, config):
    for field in record_identity_fields(config):
        value = record.get(field)
        if value not in (None, ""):
            return str(value)
    return None


def record_search_text(record, config):
    fields = record_identity_fields(config)
    for field in (config.get("titleField"), config.get("detailField")):
        if field and field not in fields:
            fields.append(field)
    values = []
    for field in fields:
        value = record.get(field)
        if value not in (None, ""):
            values.append(str(value))
    return " ".join(values).casefold()


def search_records(records, config, query, limit):
    query = str(query or "").strip()
    if not query:
        return []
    query_folded = query.casefold()
    matches = []
    for index, record in enumerate(records):
        identity = record_identity(record, config)
        exact_identity = identity is not None and identity.casefold() == query_folded
        text = record_search_text(record, config)
        if exact_identity or query_folded in text:
            matches.append(
                (
                    0 if exact_identity else 1,
                    index,
                    {
                        **record,
                        "__index": index,
                        "__recordId": identity,
                    },
                )
            )
    matches.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in matches[:limit]]


def latest_artifact_payload(sink):
    if not sink["latestArtifactPath"]:
        return {"config": sink["config"], "data": []}
    artifact_path = Path(sink["latestArtifactPath"]).resolve()
    if DATA_ROOT not in artifact_path.parents or not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found.")
    try:
        return read_json_file(artifact_path, "Stored artifact")
    except StoredDataError as error:
        raise HTTPException(status_code=500, detail="Stored artifact is invalid.") from error


@app.get("/api/help", dependencies=[Depends(require_auth)])
@app.get("/api", dependencies=[Depends(require_auth)])
def get_api_help():
    return api_help_payload()


@app.get("/api/status", dependencies=[Depends(require_auth)])
def get_api_status():
    return system_status_payload()


@app.post(
    "/api/data-graph",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_auth)],
)
def create_data_graph(payload: dict = Body(...)):
    try:
        config = validate_config(payload.get("config"))
        rows = payload.get("data", [])
        transformed_rows, _ = transformed_rows_for_config(config, rows)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if transformed_rows:
        ensure_processing_ready(config)

    graph_id = new_id("dg")
    created_at = now_iso()
    base_dir = sink_dir(graph_id)
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
                graph_id,
                config["name"],
                json_dumps(config),
                "created",
                created_at,
                created_at,
            ),
        )
        sink = load_sink(db, graph_id)
        if rows:
            persist_batch(db, sink, rows)
            revision = mark_sink_processing(db, graph_id)
        db.commit()
    if rows:
        schedule_artifact_rebuild(graph_id, revision)

    return {
        "dataGraphId": graph_id,
        "viewUrl": public_url(f"/clusters/{graph_id}"),
        "ingestUrl": public_url(f"/api/data-graph/{graph_id}/data"),
        "latestArtifactUrl": public_url(f"/api/data-graph/{graph_id}/artifact/latest"),
    }


@app.get("/api/data-graph/{graph_id}", dependencies=[Depends(require_auth)])
def get_data_graph(graph_id: str):
    if not ID_PATTERN.match(graph_id):
        raise HTTPException(status_code=404, detail="Data graph not found.")
    with connect_db() as db:
        sink = load_sink(db, graph_id)
    if not sink:
        raise HTTPException(status_code=404, detail="Data graph not found.")
    return sink


@app.get("/api/data-graph/{graph_id}/status", dependencies=[Depends(require_auth)])
def get_data_graph_status(graph_id: str):
    if not ID_PATTERN.match(graph_id):
        raise HTTPException(status_code=404, detail="Data graph not found.")
    return get_data_graph_status_payload(graph_id)


@app.get("/api/data-graph/{graph_id}/help", dependencies=[Depends(require_auth)])
def get_data_graph_help(graph_id: str):
    if not ID_PATTERN.match(graph_id):
        raise HTTPException(status_code=404, detail="Data graph not found.")
    with connect_db() as db:
        sink = load_sink(db, graph_id)
    if not sink:
        raise HTTPException(status_code=404, detail="Data graph not found.")
    return sink_help_payload(sink)


@app.get("/api/data-graph/{graph_id}/artifact/latest", dependencies=[Depends(require_auth)])
def get_latest_artifact(graph_id: str):
    if not ID_PATTERN.match(graph_id):
        raise HTTPException(status_code=404, detail="Data graph not found.")
    with connect_db() as db:
        sink = load_sink(db, graph_id)
    if not sink:
        raise HTTPException(status_code=404, detail="Data graph not found.")
    return latest_artifact_payload(sink)


@app.get("/api/data-graph/{graph_id}/records/search", dependencies=[Depends(require_auth)])
def search_data_graph_records(
    graph_id: str,
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=25, ge=1, le=100),
):
    if not ID_PATTERN.match(graph_id):
        raise HTTPException(status_code=404, detail="Data graph not found.")
    with connect_db() as db:
        sink = load_sink(db, graph_id)
    if not sink:
        raise HTTPException(status_code=404, detail="Data graph not found.")
    payload = latest_artifact_payload(sink)
    records = search_records(payload.get("data") or [], payload.get("config") or sink["config"], q, limit)
    return {
        "dataGraphId": graph_id,
        "query": q,
        "recordIdField": (payload.get("config") or sink["config"]).get("recordIdField"),
        "count": len(records),
        "records": records,
    }


@app.post(
    "/api/data-graph/{graph_id}/data",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_auth)],
)
def append_rows(graph_id: str, payload: dict = Body(...)):
    if not ID_PATTERN.match(graph_id):
        raise HTTPException(status_code=404, detail="Data graph not found.")
    with connect_db() as db:
        sink = load_sink(db, graph_id)
        if not sink:
            raise HTTPException(status_code=404, detail="Data graph not found.")
        try:
            rows = validate_raw_rows(payload.get("data"))
            transformed_rows, _ = transformed_rows_for_config(sink["config"], rows)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if transformed_rows:
            ensure_processing_ready(sink["config"])

        batch_id = persist_batch(db, sink, rows)
        revision = mark_sink_processing(db, graph_id)
        db.commit()
    schedule_artifact_rebuild(graph_id, revision)

    return {
        "dataGraphId": graph_id,
        "batchId": batch_id,
        "rowCount": len(rows),
        "status": "processing",
        "processAfterSeconds": PROCESS_DEBOUNCE_SECONDS,
        "viewUrl": public_url(f"/clusters/{graph_id}"),
    }


@app.patch("/api/data-graph/{graph_id}/schema", dependencies=[Depends(require_auth)])
def update_schema(graph_id: str, payload: dict = Body(...)):
    if not ID_PATTERN.match(graph_id):
        raise HTTPException(status_code=404, detail="Data graph not found.")
    try:
        config = validate_config(payload.get("config"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    with connect_db() as db:
        sink = load_sink(db, graph_id)
        if not sink:
            raise HTTPException(status_code=404, detail="Data graph not found.")
        existing_rows = read_all_rows(db, graph_id)
        try:
            transformed_rows, _ = transformed_rows_for_config(config, existing_rows)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if transformed_rows:
            ensure_processing_ready(config)
        updated_at = now_iso()
        db.execute(
            "UPDATE data_sinks SET name = ?, config_json = ?, updated_at = ? WHERE id = ?",
            (config["name"], json_dumps(config), updated_at, graph_id),
        )
        revision = mark_sink_processing(db, graph_id)
        updated_sink = load_sink(db, graph_id)
        try:
            processed_rows, processor_metadata = process_sink_records(
                db, updated_sink, existing_rows
            )
        except ProcessingError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        write_artifact(
            db,
            updated_sink,
            processed_rows,
            expected_revision=revision,
            processor_metadata=processor_metadata,
        )
        db.commit()

    return {"dataGraphId": graph_id, "viewUrl": public_url(f"/clusters/{graph_id}")}


@app.delete("/api/data-graph/{graph_id}/data", dependencies=[Depends(require_auth)])
def clear_rows(graph_id: str):
    if not ID_PATTERN.match(graph_id):
        raise HTTPException(status_code=404, detail="Data graph not found.")
    cancel_scheduled_rebuild(graph_id)
    with connect_db() as db:
        sink = load_sink(db, graph_id)
        if not sink:
            raise HTTPException(status_code=404, detail="Data graph not found.")

        raw_paths = [
            Path(row["raw_path"]).resolve()
            for row in db.execute(
                "SELECT raw_path FROM data_batches WHERE sink_id = ?", (graph_id,)
            ).fetchall()
        ]
        artifact_paths = [
            Path(row["artifact_path"]).resolve()
            for row in db.execute(
                "SELECT artifact_path FROM artifacts WHERE sink_id = ?", (graph_id,)
            ).fetchall()
        ]

        db.execute("DELETE FROM data_batches WHERE sink_id = ?", (graph_id,))
        db.execute("DELETE FROM artifacts WHERE sink_id = ?", (graph_id,))
        revision = mark_sink_processing(db, graph_id)
        updated_sink = load_sink(db, graph_id)
        processed_rows, processor_metadata = process_sink_records(db, updated_sink, [])
        artifact_path = write_artifact(
            db,
            updated_sink,
            processed_rows,
            expected_revision=revision,
            processor_metadata=processor_metadata,
        )
        db.commit()

    for path in raw_paths + artifact_paths:
        if DATA_ROOT in path.parents and path.exists():
            path.unlink()

    return {
        "dataGraphId": graph_id,
        "cleared": True,
        "rowCount": 0,
        "artifactPath": artifact_path.name if artifact_path else None,
        "viewUrl": public_url(f"/clusters/{graph_id}"),
    }


def index_file():
    path = PUBLIC_ROOT / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Build the frontend first with npm run build.")
    return path


@app.get("/clusters/{graph_id}")
def serve_cluster(graph_id: str):
    return FileResponse(index_file(), media_type="text/html")


@app.get("/")
def serve_root():
    return FileResponse(index_file(), media_type="text/html")


@app.get("/favicon.ico")
def serve_favicon():
    return Response(status_code=204)


if (PUBLIC_ROOT / "assets").exists():
    app.mount("/assets", StaticFiles(directory=PUBLIC_ROOT / "assets"), name="assets")


@app.get("/sample-data/{sample_name}")
def serve_sample_data(sample_name: str):
    if sample_name not in PUBLIC_SAMPLE_FILES:
        raise HTTPException(status_code=404, detail="Sample data not found.")
    path = ROOT / "sample-data" / sample_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Sample data not found.")
    return FileResponse(path, media_type="application/json")


def main():
    parser = argparse.ArgumentParser(
        description="Run the local Data Graph API and FastAPI server."
    )
    parser.add_argument(
        "--host", default=os.environ.get("DATA_GRAPH_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("DATA_GRAPH_PORT", "8080"))
    )
    args = parser.parse_args()

    print(f"Data Graph local server: http://{args.host}:{args.port}")
    print(f"SQLite: {DB_PATH}")
    print(f"Storage: {DATA_ROOT}")
    if not os.environ.get("DATA_GRAPH_API_TOKEN"):
        print(
            "DATA_GRAPH_API_TOKEN is not set; APIs will reject requests.",
            file=sys.stderr,
        )

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
