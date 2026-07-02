import asyncio
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException


class ServerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.saved_env = {
            key: os.environ.get(key)
            for key in (
                "DATA_GRAPH_ENV",
                "DATA_GRAPH_STORAGE",
                "DATA_GRAPH_DB",
                "DATA_GRAPH_API_TOKEN",
                "OPENAI_API_KEY",
                "DATA_GRAPH_TEXT_FEATURE_METHOD",
                "DATA_GRAPH_EMBEDDING_MODEL",
                "DATA_GRAPH_EMBEDDING_DIMENSIONS",
                "DATA_GRAPH_EMBEDDING_BATCH_SIZE",
                "DATA_GRAPH_EMBEDDING_TIMEOUT_SECONDS",
            )
        }
        temp_path = Path(self.tempdir.name)
        os.environ["DATA_GRAPH_ENV"] = str(temp_path / "missing.env")
        os.environ["DATA_GRAPH_STORAGE"] = str(temp_path / "storage")
        os.environ["DATA_GRAPH_DB"] = str(temp_path / "test.sqlite3")
        os.environ["DATA_GRAPH_API_TOKEN"] = os.urandom(16).hex()
        for key in (
            "OPENAI_API_KEY",
            "DATA_GRAPH_TEXT_FEATURE_METHOD",
            "DATA_GRAPH_EMBEDDING_MODEL",
            "DATA_GRAPH_EMBEDDING_DIMENSIONS",
            "DATA_GRAPH_EMBEDDING_BATCH_SIZE",
            "DATA_GRAPH_EMBEDDING_TIMEOUT_SECONDS",
        ):
            os.environ.pop(key, None)
        sys.modules.pop("server", None)
        self.server = importlib.import_module("server")
        self.server.init_storage()

    def tearDown(self):
        sys.modules.pop("server", None)
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tempdir.cleanup()

    def test_body_limit_rejects_chunked_body_without_content_length(self):
        sent = []

        async def app(scope, receive, send):
            while True:
                message = await receive()
                if not message.get("more_body", False):
                    break
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = self.server.BodyLimitMiddleware(app, max_body_bytes=10)
        messages = [
            {"type": "http.request", "body": b"123456", "more_body": True},
            {"type": "http.request", "body": b"78901", "more_body": False},
        ]

        async def receive():
            return messages.pop(0)

        async def send(message):
            sent.append(message)

        asyncio.run(
            middleware(
                {"type": "http", "headers": []},
                receive,
                send,
            )
        )

        self.assertEqual(sent[0]["status"], 413)

    def test_body_limit_preserves_allowed_request_chunks(self):
        observed_chunks = []
        sent = []

        async def app(scope, receive, send):
            while True:
                message = await receive()
                observed_chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = self.server.BodyLimitMiddleware(app, max_body_bytes=10)
        messages = [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"45", "more_body": False},
        ]

        async def receive():
            return messages.pop(0)

        async def send(message):
            sent.append(message)

        asyncio.run(
            middleware(
                {"type": "http", "headers": []},
                receive,
                send,
            )
        )

        self.assertEqual(observed_chunks, [b"123", b"45"])
        self.assertEqual(sent[0]["status"], 204)

    def test_body_limit_rejects_oversized_content_length_before_reading_body(self):
        app_called = False
        receive_called = False

        async def app(scope, receive, send):
            nonlocal app_called
            app_called = True

        middleware = self.server.BodyLimitMiddleware(app, max_body_bytes=10)
        sent = []

        async def receive():
            nonlocal receive_called
            receive_called = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(
            middleware(
                {"type": "http", "headers": [(b"content-length", b"11")]},
                receive,
                send,
            )
        )

        self.assertFalse(app_called)
        self.assertFalse(receive_called)
        self.assertEqual(sent[0]["status"], 413)

    def test_sample_data_route_only_serves_allowlisted_files(self):
        with self.assertRaises(HTTPException) as context:
            self.server.serve_sample_data("private.json")

        self.assertEqual(context.exception.status_code, 404)

    def test_frontend_default_sample_is_allowlisted(self):
        manifest = json.loads(
            (self.server.ROOT / "sample-manifest.json").read_text(encoding="utf-8")
        )
        default_sample = Path(manifest["defaultSamplePath"]).name

        self.assertIn(default_sample, self.server.PUBLIC_SAMPLE_FILES)

    def test_load_sink_rejects_invalid_stored_config_json(self):
        graph_id = self.server.new_id("dg")
        now = self.server.now_iso()
        with self.server.connect_db() as db:
            db.execute(
                """
                INSERT INTO data_sinks
                  (id, name, config_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (graph_id, "Broken Config", "{", "created", now, now),
            )
            db.commit()

            with self.assertRaises(self.server.StoredDataError) as context:
                self.server.load_sink(db, graph_id)

        self.assertIn("Stored config", str(context.exception))

    def test_read_all_rows_rejects_invalid_stored_batch_json(self):
        graph_id = self.server.new_id("dg")
        batch_path = self.server.sink_dir(graph_id) / "raw" / "broken.json"
        self.server.ensure_private_dir(batch_path.parent)
        batch_path.write_text("{", encoding="utf-8")
        now = self.server.now_iso()
        config = {
            "name": "Broken Batch",
            "dataSchema": {"name": "String"},
            "groupingFields": ["name"],
        }
        with self.server.connect_db() as db:
            db.execute(
                """
                INSERT INTO data_sinks
                  (id, name, config_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    graph_id,
                    config["name"],
                    self.server.json_dumps(config),
                    "created",
                    now,
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO data_batches
                  (id, sink_id, raw_path, row_count, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self.server.new_id("batch"), graph_id, str(batch_path), 0, now),
            )
            db.commit()

            with self.assertRaises(self.server.StoredDataError) as context:
                self.server.read_all_rows(db, graph_id)

        self.assertIn("Stored data batch", str(context.exception))

    def test_read_all_rows_rejects_wrong_stored_batch_shape(self):
        graph_id = self.server.new_id("dg")
        batch_path = self.server.sink_dir(graph_id) / "raw" / "wrong-shape.json"
        self.server.ensure_private_dir(batch_path.parent)
        batch_path.write_text("[]", encoding="utf-8")
        now = self.server.now_iso()
        config = {
            "name": "Wrong Shape Batch",
            "dataSchema": {"name": "String"},
            "groupingFields": ["name"],
        }
        with self.server.connect_db() as db:
            db.execute(
                """
                INSERT INTO data_sinks
                  (id, name, config_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    graph_id,
                    config["name"],
                    self.server.json_dumps(config),
                    "created",
                    now,
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO data_batches
                  (id, sink_id, raw_path, row_count, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self.server.new_id("batch"), graph_id, str(batch_path), 0, now),
            )
            db.commit()

            with self.assertRaises(self.server.StoredDataError) as context:
                self.server.read_all_rows(db, graph_id)

        self.assertIn("data array", str(context.exception))

    def test_latest_artifact_rejects_invalid_stored_json(self):
        graph_id = self.server.new_id("dg")
        artifact_path = self.server.sink_dir(graph_id) / "processed" / "broken.json"
        self.server.ensure_private_dir(artifact_path.parent)
        artifact_path.write_text("{", encoding="utf-8")

        with self.assertRaises(HTTPException) as context:
            self.server.latest_artifact_payload(
                {
                    "config": {"name": "Broken Artifact"},
                    "latestArtifactPath": str(artifact_path),
                }
            )

        self.assertEqual(context.exception.status_code, 500)
        self.assertEqual(context.exception.detail, "Stored artifact is invalid.")

    def test_invalid_cluster_config_is_rejected(self):
        with self.assertRaises(ValueError) as context:
            self.server.validate_config(
                {
                    "name": "Bad Config",
                    "dataSchema": {"name": "String", "score": "Number"},
                    "groupingFields": ["name"],
                    "cluster": {
                        "textFeatureMethod": "bogus",
                    },
                }
            )

        self.assertIn("textFeatureMethod", str(context.exception))

        with self.assertRaises(ValueError) as numeric_context:
            self.server.validate_config(
                {
                    "name": "Bad Config",
                    "dataSchema": {"name": "String", "score": "Number"},
                    "groupingFields": ["name"],
                    "cluster": {
                        "numericFields": ["name"],
                    },
                }
            )

        self.assertIn("numericFields", str(numeric_context.exception))

        with self.assertRaises(ValueError) as label_context:
            self.server.validate_config(
                {
                    "name": "Bad Label Config",
                    "dataSchema": {"name": "String", "score": "Number"},
                    "groupingFields": ["name"],
                    "cluster": {
                        "labelField": "missing",
                    },
                }
            )

        self.assertIn("labelField", str(label_context.exception))

    def test_pipeline_transforms_filter_and_validates_rows(self):
        config = self.server.validate_config(
            {
                "name": "Pipeline Graph",
                "dataSchema": {
                    "id": "String",
                    "title": "String",
                    "kind": "String",
                    "archived": "Boolean",
                },
                "groupingFields": ["kind"],
                "recordIdField": "id",
                "titleField": "title",
                "detailField": "kind",
                "pipeline": {
                    "transforms": [
                        {"type": "copyField", "from": "ticket", "to": "id"},
                        {"type": "trim", "field": "title"},
                    ],
                    "filters": [
                        {"field": "archived", "op": "notEquals", "value": True},
                    ],
                },
            }
        )

        rows, metadata = self.server.transformed_rows_for_config(
            config,
            [
                {
                    "ticket": "BUG-1",
                    "title": "  Login fails  ",
                    "kind": "bug",
                    "archived": False,
                },
                {
                    "ticket": "BUG-2",
                    "title": "Old issue",
                    "kind": "bug",
                    "archived": True,
                },
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "BUG-1")
        self.assertEqual(rows[0]["title"], "Login fails")
        self.assertEqual(metadata["filteredRecordCount"], 1)

    def test_pipeline_rejects_unknown_target_field(self):
        with self.assertRaises(ValueError) as context:
            self.server.validate_config(
                {
                    "name": "Bad Pipeline",
                    "dataSchema": {"id": "String", "kind": "String"},
                    "groupingFields": ["kind"],
                    "pipeline": {
                        "transforms": [
                            {"type": "copyField", "from": "ticket", "to": "missing"},
                        ],
                    },
                }
            )

        self.assertIn("dataSchema", str(context.exception))

    def test_pipeline_rejects_set_field_value_with_wrong_type(self):
        with self.assertRaises(ValueError) as context:
            self.server.validate_config(
                {
                    "name": "Bad Set Field",
                    "dataSchema": {"score": "Number", "kind": "String"},
                    "groupingFields": ["kind"],
                    "pipeline": {
                        "transforms": [
                            {"type": "setField", "field": "score", "value": "bad"},
                        ],
                    },
                }
            )

        self.assertIn("must be Number", str(context.exception))

    def test_embedding_create_all_filtered_rows_does_not_require_api_key(self):
        response = self.server.create_data_graph(
            {
                "config": {
                    "name": "Filtered Embedding",
                    "dataSchema": {
                        "name": "String",
                        "kind": "String",
                        "archived": "Boolean",
                    },
                    "groupingFields": ["kind"],
                    "pipeline": {
                        "filters": [
                            {"field": "archived", "op": "equals", "value": False},
                        ],
                    },
                    "cluster": {"textFeatureMethod": "embedding"},
                },
                "data": [{"name": "old", "kind": "a", "archived": True}],
            }
        )
        self.server.cancel_scheduled_rebuild(response["dataGraphId"])

        with self.server.connect_db() as db:
            sink_count = db.execute("SELECT COUNT(*) FROM data_sinks").fetchone()[0]
            batch_count = db.execute("SELECT COUNT(*) FROM data_batches").fetchone()[0]

        self.assertEqual(sink_count, 1)
        self.assertEqual(batch_count, 1)

    def test_config_rejects_api_key_fields(self):
        with self.assertRaises(ValueError) as context:
            self.server.validate_config(
                {
                    "name": "Secret Config",
                    "dataSchema": {"name": "String", "kind": "String"},
                    "groupingFields": ["kind"],
                    "openaiApiKey": "sk-should-not-store",
                }
            )

        self.assertIn("must not contain API keys", str(context.exception))

        with self.assertRaises(ValueError) as cluster_context:
            self.server.validate_config(
                {
                    "name": "Secret Cluster Config",
                    "dataSchema": {"name": "String", "kind": "String"},
                    "groupingFields": ["kind"],
                    "cluster": {"openaiKey": "secret"},
                }
            )

        self.assertIn("must not contain API keys", str(cluster_context.exception))

        with self.assertRaises(ValueError) as nested_context:
            self.server.validate_config(
                {
                    "name": "Nested Secret Config",
                    "dataSchema": {"name": "String", "kind": "String"},
                    "groupingFields": ["kind"],
                    "credentials": {"openaiApiKey": "sk-should-not-store"},
                }
            )

        self.assertIn("must not contain API keys", str(nested_context.exception))

        with self.assertRaises(ValueError) as source_context:
            self.server.validate_config(
                {
                    "name": "Source Secret Config",
                    "dataSchema": {"name": "String", "kind": "String"},
                    "groupingFields": ["kind"],
                    "source": {"openaiApiKey": "sk-should-not-store"},
                }
            )

        self.assertIn("must not contain API keys", str(source_context.exception))

        with self.assertRaises(ValueError) as source_value_context:
            self.server.validate_config(
                {
                    "name": "Source Secret Value",
                    "dataSchema": {"name": "String", "kind": "String"},
                    "groupingFields": ["kind"],
                    "source": "sk-should-not-store",
                }
            )

        self.assertIn("must not contain API keys", str(source_value_context.exception))

        with self.assertRaises(ValueError) as model_value_context:
            self.server.validate_config(
                {
                    "name": "Model Secret Value",
                    "dataSchema": {"name": "String", "kind": "String"},
                    "groupingFields": ["kind"],
                    "cluster": {"embeddingModel": "sk-should-not-store"},
                }
            )

        self.assertIn("must not contain API keys", str(model_value_context.exception))

        with self.assertRaises(ValueError) as schema_key_context:
            self.server.validate_config(
                {
                    "name": "Schema Secret Key",
                    "dataSchema": {"openaiApiKey": "String", "kind": "String"},
                    "groupingFields": ["kind"],
                }
            )

        self.assertIn("must not contain API keys", str(schema_key_context.exception))

    def test_embedding_create_preflight_fails_before_persisting_rows(self):
        with self.assertRaises(HTTPException) as context:
            self.server.create_data_graph(
                {
                    "config": {
                        "name": "Embedding Graph",
                        "dataSchema": {"name": "String", "kind": "String"},
                        "groupingFields": ["kind"],
                        "cluster": {"textFeatureMethod": "embedding"},
                    },
                    "data": [{"name": "alpha", "kind": "a"}],
                }
            )

        self.assertEqual(context.exception.status_code, 400)
        with self.server.connect_db() as db:
            sink_count = db.execute("SELECT COUNT(*) FROM data_sinks").fetchone()[0]
            batch_count = db.execute("SELECT COUNT(*) FROM data_batches").fetchone()[0]
        self.assertEqual(sink_count, 0)
        self.assertEqual(batch_count, 0)

    def test_embedding_append_preflight_fails_before_persisting_rows(self):
        config = self.server.validate_config(
            {
                "name": "Embedding Append",
                "dataSchema": {"name": "String", "kind": "String"},
                "groupingFields": ["kind"],
                "cluster": {"textFeatureMethod": "embedding"},
            }
        )
        graph_id = self.server.new_id("dg")
        base_dir = self.server.sink_dir(graph_id)
        self.server.ensure_private_dir(base_dir)
        self.server.ensure_private_dir(base_dir / "raw")
        self.server.ensure_private_dir(base_dir / "processed")
        with self.server.connect_db() as db:
            now = self.server.now_iso()
            db.execute(
                """
                INSERT INTO data_sinks
                  (id, name, config_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (graph_id, config["name"], self.server.json_dumps(config), "created", now, now),
            )
            db.commit()

        with self.assertRaises(HTTPException) as context:
            self.server.append_rows(
                graph_id,
                {"data": [{"name": "alpha", "kind": "a"}]},
            )

        self.assertEqual(context.exception.status_code, 400)
        with self.server.connect_db() as db:
            batch_count = db.execute(
                "SELECT COUNT(*) FROM data_batches WHERE sink_id = ?",
                (graph_id,),
            ).fetchone()[0]
        self.assertEqual(batch_count, 0)

    def test_embedding_cache_round_trip(self):
        with self.server.connect_db() as db:
            cache = self.server.SqliteEmbeddingCache(db)
            self.assertEqual(cache.get_many("openai", "model", 2, ["abc"]), {})
            cache.set_many(
                "openai",
                "model",
                2,
                {"abc": [1.0, 2.0], "def": [3.0, 4.0]},
            )
            self.assertEqual(
                cache.get_many("openai", "model", 2, ["abc", "def", "missing"]),
                {"abc": [1.0, 2.0], "def": [3.0, 4.0]},
            )

    def test_processor_metadata_does_not_expose_api_key(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-secret"
        os.environ["DATA_GRAPH_TEXT_FEATURE_METHOD"] = "embedding"
        os.environ["DATA_GRAPH_EMBEDDING_MODEL"] = "text-embedding-3-small"

        payloads = [
            self.server.system_status_payload(),
            self.server.safe_processor_settings(),
        ]
        serialized = json.dumps(payloads)

        self.assertIn("embeddingConfigured", serialized)
        self.assertNotIn("sk-test-secret", serialized)

    def test_record_search_prefers_configured_record_id_field(self):
        records = [
            {
                "id": "internal-1",
                "sourceTicketId": "SUP-100",
                "title": "Login failure",
                "summary": "OAuth callback error",
            },
            {
                "id": "internal-2",
                "sourceTicketId": "SUP-101",
                "title": "Billing",
                "summary": "Invoice search",
            },
        ]
        config = {
            "recordIdField": "sourceTicketId",
            "titleField": "title",
            "detailField": "summary",
        }

        exact = self.server.search_records(records, config, "SUP-101", 10)
        text = self.server.search_records(records, config, "callback", 10)

        self.assertEqual(exact[0]["__recordId"], "SUP-101")
        self.assertEqual(exact[0]["title"], "Billing")
        self.assertEqual(text[0]["__recordId"], "SUP-100")

    def test_embedding_error_keeps_prior_latest_artifact(self):
        config = self.server.validate_config(
            {
                "name": "Embedding Error Test",
                "dataSchema": {"name": "String", "kind": "String"},
                "groupingFields": ["kind"],
                "titleField": "name",
                "detailField": "kind",
            }
        )
        graph_id = self.server.new_id("dg")
        base_dir = self.server.sink_dir(graph_id)
        self.server.ensure_private_dir(base_dir)
        self.server.ensure_private_dir(base_dir / "raw")
        self.server.ensure_private_dir(base_dir / "processed")

        with self.server.connect_db() as db:
            now = self.server.now_iso()
            db.execute(
                """
                INSERT INTO data_sinks
                  (id, name, config_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    graph_id,
                    config["name"],
                    self.server.json_dumps(config),
                    "created",
                    now,
                    now,
                ),
            )
            sink = self.server.load_sink(db, graph_id)
            old_path = self.server.write_artifact(
                db,
                sink,
                [
                    {
                        "name": "old",
                        "kind": "a",
                        "x": 0,
                        "y": 0,
                        "clusterId": 0,
                        "clusterLabel": "a",
                        "groupValue": "a",
                    }
                ],
                expected_revision=0,
                processor_metadata={"textFeatureMethod": "tfidf"},
            )
            embedding_config = {
                **config,
                "cluster": {"textFeatureMethod": "embedding"},
            }
            db.execute(
                "UPDATE data_sinks SET config_json = ? WHERE id = ?",
                (self.server.json_dumps(embedding_config), graph_id),
            )
            updated_sink = self.server.load_sink(db, graph_id)
            self.server.persist_batch(
                db,
                updated_sink,
                [{"name": "new", "kind": "b"}],
            )
            revision = self.server.mark_sink_processing(db, graph_id)
            db.commit()

        with contextlib.redirect_stderr(io.StringIO()):
            self.server.process_pending_sink(graph_id, revision)

        with self.server.connect_db() as db:
            row = db.execute(
                "SELECT status, latest_artifact_path FROM data_sinks WHERE id = ?",
                (graph_id,),
            ).fetchone()

        self.assertEqual(row["status"], "error")
        self.assertEqual(row["latest_artifact_path"], str(old_path))

    def test_clear_embedding_graph_without_key_preserves_file_db_consistency(self):
        config = self.server.validate_config(
            {
                "name": "Clear Embedding",
                "dataSchema": {"name": "String", "kind": "String"},
                "groupingFields": ["kind"],
                "cluster": {"textFeatureMethod": "embedding"},
            }
        )
        graph_id = self.server.new_id("dg")
        base_dir = self.server.sink_dir(graph_id)
        self.server.ensure_private_dir(base_dir)
        self.server.ensure_private_dir(base_dir / "raw")
        self.server.ensure_private_dir(base_dir / "processed")

        with self.server.connect_db() as db:
            now = self.server.now_iso()
            db.execute(
                """
                INSERT INTO data_sinks
                  (id, name, config_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (graph_id, config["name"], self.server.json_dumps(config), "created", now, now),
            )
            sink = self.server.load_sink(db, graph_id)
            self.server.persist_batch(db, sink, [{"name": "old", "kind": "a"}])
            old_artifact = self.server.write_artifact(
                db,
                sink,
                [
                    {
                        "name": "old",
                        "kind": "a",
                        "x": 0,
                        "y": 0,
                        "clusterId": 0,
                        "clusterLabel": "a",
                        "groupValue": "a",
                    }
                ],
                expected_revision=0,
                processor_metadata={"textFeatureMethod": "embedding"},
            )
            raw_path = Path(
                db.execute(
                    "SELECT raw_path FROM data_batches WHERE sink_id = ?",
                    (graph_id,),
                ).fetchone()["raw_path"]
            )
            db.commit()

        response = self.server.clear_rows(graph_id)

        self.assertTrue(response["cleared"])
        self.assertFalse(raw_path.exists())
        self.assertFalse(old_artifact.exists())
        with self.server.connect_db() as db:
            sink = self.server.load_sink(db, graph_id)
            artifact_path = Path(sink["latestArtifactPath"])
            batch_count = db.execute(
                "SELECT COUNT(*) FROM data_batches WHERE sink_id = ?",
                (graph_id,),
            ).fetchone()[0]
        self.assertEqual(batch_count, 0)
        self.assertTrue(artifact_path.exists())

    def test_stale_revision_cannot_become_latest_artifact(self):
        config = self.server.validate_config(
            {
                "name": "Revision Test",
                "dataSchema": {"name": "String", "kind": "String"},
                "groupingFields": ["kind"],
                "titleField": "name",
                "detailField": "kind",
            }
        )
        graph_id = self.server.new_id("dg")
        base_dir = self.server.sink_dir(graph_id)
        self.server.ensure_private_dir(base_dir)
        self.server.ensure_private_dir(base_dir / "raw")
        self.server.ensure_private_dir(base_dir / "processed")

        with self.server.connect_db() as db:
            now = self.server.now_iso()
            db.execute(
                """
                INSERT INTO data_sinks
                  (id, name, config_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    graph_id,
                    config["name"],
                    self.server.json_dumps(config),
                    "created",
                    now,
                    now,
                ),
            )
            sink = self.server.load_sink(db, graph_id)
            self.server.persist_batch(db, sink, [{"name": "old", "kind": "a"}])
            stale_revision = self.server.mark_sink_processing(db, graph_id)
            stale_sink = self.server.load_sink(db, graph_id)
            self.server.persist_batch(db, stale_sink, [{"name": "new", "kind": "b"}])
            current_revision = self.server.mark_sink_processing(db, graph_id)

            stale_path = self.server.write_artifact(
                db,
                stale_sink,
                [
                    {
                        "name": "old",
                        "kind": "a",
                        "x": 0,
                        "y": 0,
                        "clusterId": 0,
                        "clusterLabel": "a",
                        "groupValue": "a",
                    }
                ],
                expected_revision=stale_revision,
            )

            self.assertIsNone(stale_path)
            row = db.execute(
                """
                SELECT latest_artifact_path, processed_revision
                FROM data_sinks
                WHERE id = ?
                """,
                (graph_id,),
            ).fetchone()
            self.assertIsNone(row["latest_artifact_path"])
            self.assertEqual(row["processed_revision"], 0)

            current_sink = self.server.load_sink(db, graph_id)
            current_path = self.server.write_artifact(
                db, current_sink, [], expected_revision=current_revision
            )
            self.assertIsNotNone(current_path)


if __name__ == "__main__":
    unittest.main()
