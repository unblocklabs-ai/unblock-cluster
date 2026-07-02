import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))
SCRIPT_PATH = SCRIPTS_ROOT / "data_graph_import.py"
EXPORT_SCRIPT_PATH = SCRIPTS_ROOT / "data_graph_export.py"
SPEC = importlib.util.spec_from_file_location("data_graph_import", SCRIPT_PATH)
data_graph_import = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(data_graph_import)
EXPORT_SPEC = importlib.util.spec_from_file_location("data_graph_export", EXPORT_SCRIPT_PATH)
data_graph_export = importlib.util.module_from_spec(EXPORT_SPEC)
EXPORT_SPEC.loader.exec_module(data_graph_export)


class DataGraphImportScriptTests(unittest.TestCase):
    def test_csv_rows_are_coerced_to_schema_types(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "rows.csv"
            path.write_text(
                "\n".join(
                    [
                        "id,score,active,metadata,tags",
                        'r-1,42,true,"{""tier"": ""gold""}","[""urgent""]"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            rows = data_graph_import.load_rows(
                path,
                "csv",
                schema={
                    "id": "String",
                    "score": "Number",
                    "active": "Boolean",
                    "metadata": "Object",
                    "tags": "Array",
                },
            )

        self.assertEqual(
            rows,
            [
                {
                    "id": "r-1",
                    "score": 42,
                    "active": True,
                    "metadata": {"tier": "gold"},
                    "tags": ["urgent"],
                }
            ],
        )

    def test_csv_invalid_boolean_exits_before_request(self):
        with self.assertRaises(SystemExit) as context:
            data_graph_import.coerce_value("maybe", "Boolean")

        self.assertIn("Boolean", str(context.exception))

    def test_csv_preserves_source_fields_for_pipeline_transforms(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "rows.csv"
            path.write_text(
                "\n".join(
                    [
                        "source_ticket_id,title,team,summary,archived",
                        "SUP-1, Login issue ,Platform,OAuth callback fails,false",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            rows = data_graph_import.load_rows(
                path,
                "csv",
                schema={
                    "ticketId": "String",
                    "title": "String",
                    "team": "String",
                    "summary": "String",
                    "archived": "Boolean",
                },
            )

        self.assertEqual(rows[0]["source_ticket_id"], "SUP-1")
        self.assertNotIn("ticketId", rows[0])
        self.assertIs(rows[0]["archived"], False)

    def test_csv_missing_schema_fields_are_not_fabricated(self):
        row = data_graph_import.coerce_row(
            {"title": "Hello"},
            {"ticketId": "String", "title": "String"},
        )

        self.assertEqual(row, {"title": "Hello"})

    def test_json_input_errors_are_reported_before_request(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "rows.json"
            path.write_text("{", encoding="utf-8")

            with self.assertRaises(SystemExit) as context:
                data_graph_import.load_rows(path, "json")

        self.assertIn("not valid JSON", str(context.exception))

    def test_base_url_must_be_http_or_https(self):
        self.assertEqual(
            data_graph_import.validated_base_url("http://127.0.0.1:8080/"),
            "http://127.0.0.1:8080",
        )
        self.assertEqual(
            data_graph_export.validated_base_url("https://example.test/api/"),
            "https://example.test/api",
        )

        with self.assertRaises(SystemExit):
            data_graph_import.validated_base_url("file:///tmp/data.json")

        with self.assertRaises(SystemExit):
            data_graph_export.validated_base_url("ftp://example.test/data")


if __name__ == "__main__":
    unittest.main()
