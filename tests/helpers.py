from __future__ import annotations

from pathlib import Path
from typing import Any

from datagraph.settings import Settings


def test_settings(data_dir: Path, **overrides: Any) -> Settings:
    return Settings(
        data_dir=data_dir,
        port=0,
        worker_idle_timeout=0.01,
        inline_cpu_runs=True,
        **overrides,
    )


test_settings.__test__ = False
