from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datagraph.db import initialize_database  # noqa: E402
from datagraph.external_vectors import import_external_vectors  # noqa: E402
from datagraph.settings import load_settings  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import externally generated vectors")
    parser.add_argument("--format", required=True, dest="format_name")
    parser.add_argument("--input", required=True, type=Path, dest="input_path")
    parser.add_argument("--dataset", required=True)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Data directory (defaults to DATAGRAPH_DATA_DIR or data)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    data_dir = args.data_dir or settings.data_dir
    db_path = data_dir / "datagraph.sqlite3"
    initialize_database(db_path)
    result = import_external_vectors(
        db_path,
        format_name=args.format_name,
        input_path=args.input_path,
        dataset=args.dataset,
    )
    payload = result.as_dict()
    payload["vizUrl"] = (
        f"http://127.0.0.1:{settings.port}/?graphId={result.graph_id}&viewId={result.view_id}"
    )
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
