from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    port: int
    openai_api_key: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "datagraph.sqlite3"


def load_settings() -> Settings:
    data_dir = Path(
        os.environ.get("DATAGRAPH_DATA_DIR")
        or os.environ.get("DATA_GRAPH_DATA_DIR")
        or "data"
    )
    port = int(os.environ.get("DATAGRAPH_PORT") or os.environ.get("DATA_GRAPH_PORT") or "8080")
    api_key = os.environ.get("OPENAI_API_KEY") or None
    return Settings(data_dir=data_dir, port=port, openai_api_key=api_key)

