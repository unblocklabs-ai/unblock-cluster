from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DIST_DIR = Path(__file__).resolve().parents[1] / "dist"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    port: int
    openai_api_key: str | None = None
    read_only: bool = False
    dist_dir: Path = DEFAULT_DIST_DIR
    worker_idle_timeout: float = 1.0
    inline_cpu_runs: bool = False

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
    read_only = _env_bool(os.environ.get("DATAGRAPH_READ_ONLY"))
    inline_cpu_runs = _env_bool(
        os.environ.get("DATAGRAPH_INLINE_CPU_RUNS")
        or os.environ.get("DATA_GRAPH_INLINE_CPU_RUNS")
    )
    return Settings(
        data_dir=data_dir,
        port=port,
        openai_api_key=api_key,
        read_only=read_only,
        inline_cpu_runs=inline_cpu_runs,
    )


def _env_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}
