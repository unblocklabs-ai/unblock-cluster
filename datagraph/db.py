from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def run_migrations(conn: sqlite3.Connection) -> None:
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    migrations = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    for migration in migrations:
        version = int(migration.name.split("_", 1)[0])
        if version <= current_version:
            continue
        conn.executescript(migration.read_text())
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        current_version = version


def initialize_database(db_path: Path | str) -> None:
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        run_migrations(conn)


def fetch_one(conn: sqlite3.Connection, query: str, params: tuple[object, ...]) -> dict | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def fetch_all(conn: sqlite3.Connection, query: str, params: tuple[object, ...] = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]
