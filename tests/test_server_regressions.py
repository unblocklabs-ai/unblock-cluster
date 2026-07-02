import asyncio
import importlib
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
            )
        }
        temp_path = Path(self.tempdir.name)
        os.environ["DATA_GRAPH_ENV"] = str(temp_path / "missing.env")
        os.environ["DATA_GRAPH_STORAGE"] = str(temp_path / "storage")
        os.environ["DATA_GRAPH_DB"] = str(temp_path / "test.sqlite3")
        os.environ["DATA_GRAPH_API_TOKEN"] = "test-token"
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
        manifest = json.loads((self.server.ROOT / "sample-manifest.json").read_text())
        default_sample = Path(manifest["defaultSamplePath"]).name

        self.assertIn(default_sample, self.server.PUBLIC_SAMPLE_FILES)

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
