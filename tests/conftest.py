from __future__ import annotations

import json
from pathlib import Path

from datagraph.core.ids import now_iso
from datagraph.db import connect


def insert_graph_for_tests(db_path: Path | str, graph_id: str, *, name: str = "Test Graph") -> None:
    now = now_iso()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO graphs (id, name, config_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                graph_id,
                name,
                json.dumps({"embedding": {"textFields": ["customerText"]}}),
                now,
                now,
            ),
        )
        conn.commit()
