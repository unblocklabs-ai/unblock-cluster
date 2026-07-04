from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from datagraph.core.ids import new_id, now_iso
from datagraph.core.labeling import LabelProvider
from datagraph.core.openai_client import ClockFunc, EmbeddingProvider, SleepFunc
from datagraph.core.summarization import SummaryProvider
from datagraph.db import connect, fetch_all, fetch_one
from datagraph.runs.cluster import execute_cluster_job
from datagraph.runs.embed import EmbeddingRunCancelled, execute_embedding_run
from datagraph.runs.label import execute_label_run
from datagraph.runs.layout import execute_layout_job
from datagraph.runs.summarize import execute_summarize_run
from datagraph.runs.trend import execute_trend_run

RUN_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled"}


def _cpu_noop(params: dict[str, Any]) -> dict[str, Any]:
    time.sleep(float(params.get("delaySeconds", 0.01)))
    return {"cpu": True}


@dataclass(frozen=True)
class RunSpec:
    kind: Literal["io", "cpu"]


class RunExecutor:
    def __init__(
        self,
        db_path: Path | str,
        *,
        embedding_provider_factory: Callable[[dict[str, Any]], EmbeddingProvider] | None = None,
        label_provider_factory: Callable[[dict[str, Any]], LabelProvider] | None = None,
        summary_provider_factory: Callable[[dict[str, Any]], SummaryProvider] | None = None,
        openai_api_key: str | None = None,
        clock: ClockFunc | None = None,
        sleep: SleepFunc | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._registry: dict[str, RunSpec] = {
            "noop": RunSpec(kind="io"),
            "cpu_noop": RunSpec(kind="cpu"),
            "embed": RunSpec(kind="io"),
            "summarize": RunSpec(kind="io"),
            "cluster": RunSpec(kind="cpu"),
            "layout": RunSpec(kind="cpu"),
            "label": RunSpec(kind="io"),
            "trend": RunSpec(kind="io"),
        }
        self._embedding_provider_factory = embedding_provider_factory
        self._label_provider_factory = label_provider_factory
        self._summary_provider_factory = summary_provider_factory
        self._openai_api_key = openai_api_key
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._process_pool = ProcessPoolExecutor(max_workers=1)
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._running_cancellations: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        self._process_pool.shutdown(wait=True, cancel_futures=True)

    def recover_interrupted(self) -> None:
        completed = now_iso()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE runs
                   SET status = 'failed',
                       error_text = 'interrupted by restart',
                       completed_at = ?
                 WHERE status = 'running'
                """,
                (completed,),
            )
            conn.commit()

    def enqueue_noop(
        self,
        graph_id: str,
        *,
        delay_seconds: float = 0.05,
        queue_delay_seconds: float = 0.0,
        run_type: str = "noop",
        view_id: str | None = None,
    ) -> str:
        params = {"delaySeconds": delay_seconds, "queueDelaySeconds": queue_delay_seconds}
        return self.enqueue_run(graph_id, run_type=run_type, params=params, view_id=view_id)

    def enqueue_run(
        self,
        graph_id: str,
        *,
        run_type: str,
        params: dict[str, Any] | None = None,
        view_id: str | None = None,
        input_refs: dict[str, Any] | None = None,
    ) -> str:
        if run_type not in self._registry:
            raise ValueError(f"unknown run type: {run_type}")
        run_id = new_id("run")
        created_at = now_iso()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO runs (
                  id, graph_id, view_id, type, status, params_json, progress_json,
                  error_text, input_refs_json, stats_json, created_at, started_at, completed_at
                )
                VALUES (?, ?, ?, ?, 'queued', ?, ?, NULL, ?, ?, ?, NULL, NULL)
                """,
                (
                    run_id,
                    graph_id,
                    view_id,
                    run_type,
                    json.dumps(params or {}, sort_keys=True),
                    json.dumps({"state": "queued"}, sort_keys=True),
                    json.dumps(input_refs or {}, sort_keys=True),
                    json.dumps({}, sort_keys=True),
                    created_at,
                ),
            )
            conn.commit()
        self._wake_event.set()
        return run_id

    def list_runs(
        self,
        graph_id: str,
        *,
        run_type: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM runs WHERE graph_id = ?"
        params: list[object] = [graph_id]
        if run_type is not None:
            query += " AND type = ?"
            params.append(run_type)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC, id ASC"
        with connect(self.db_path) as conn:
            return fetch_all(conn, query, tuple(params))

    def get_run(self, graph_id: str, run_id: str) -> dict | None:
        with connect(self.db_path) as conn:
            return fetch_one(
                conn,
                "SELECT * FROM runs WHERE graph_id = ? AND id = ?",
                (graph_id, run_id),
            )

    def cancel_run(self, graph_id: str, run_id: str) -> dict | None:
        completed = now_iso()
        with connect(self.db_path) as conn:
            row = fetch_one(
                conn,
                "SELECT * FROM runs WHERE graph_id = ? AND id = ?",
                (graph_id, run_id),
            )
            if row is None:
                return None
            if row["status"] == "queued":
                conn.execute(
                    """
                    UPDATE runs
                       SET status = 'cancelled',
                           progress_json = ?,
                           completed_at = ?
                     WHERE graph_id = ? AND id = ? AND status = 'queued'
                    """,
                    (
                        json.dumps({"state": "cancelled"}, sort_keys=True),
                        completed,
                        graph_id,
                        run_id,
                    ),
                )
                conn.commit()
            elif row["status"] == "running":
                event = self._running_cancellations.get(run_id)
                if event is not None:
                    event.set()
        self._wake_event.set()
        return self.get_run(graph_id, run_id)

    async def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            row = self._next_queued_run()
            if row is None:
                self._wake_event.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake_event.wait(), timeout=1.0)
                continue
            await self._run_one(row)

    def _next_queued_run(self) -> dict | None:
        with connect(self.db_path) as conn:
            return fetch_one(
                conn,
                """
                SELECT *
                  FROM runs
                 WHERE status = 'queued'
                 ORDER BY rowid ASC
                 LIMIT 1
                """,
                (),
            )

    async def _run_one(self, row: dict) -> None:
        run_id = row["id"]
        params = json.loads(row["params_json"])
        queue_delay = float(params.get("queueDelaySeconds", 0.0))
        if queue_delay > 0:
            await asyncio.sleep(queue_delay)
        if self._is_terminal(run_id):
            return
        started = now_iso()
        cancel_event = asyncio.Event()
        self._running_cancellations[run_id] = cancel_event
        if not self._mark_running(run_id, started):
            self._running_cancellations.pop(run_id, None)
            return
        try:
            spec = self._registry[row["type"]]
            if spec.kind == "cpu":
                await self._run_cpu(row, params)
            elif row["type"] == "embed":
                await self._run_embed(row, params, cancel_event)
            elif row["type"] == "label":
                await self._run_label(row, params, cancel_event)
            elif row["type"] == "summarize":
                await self._run_summarize(row, params, cancel_event)
            elif row["type"] == "trend":
                self._run_trend(row, params)
            else:
                await self._run_io_noop(row, params, cancel_event)
            if cancel_event.is_set():
                self._mark_cancelled(run_id)
            else:
                self._mark_succeeded(run_id)
        except Exception as exc:  # noqa: BLE001 - executor boundary records all task failures.
            self._mark_failed(run_id, str(exc))
        finally:
            self._running_cancellations.pop(run_id, None)

    async def _run_io_noop(
        self,
        row: dict,
        params: dict[str, Any],
        cancel_event: asyncio.Event,
    ) -> None:
        run_id = row["id"]
        delay = max(float(params.get("delaySeconds", 0.05)), 0.0)
        steps = 4
        for step in range(steps):
            if cancel_event.is_set():
                return
            self._update_progress(
                run_id,
                {"state": "running", "step": step + 1, "totalSteps": steps},
            )
            await asyncio.sleep(delay / steps if delay else 0)

    async def _run_cpu(self, row: dict, params: dict[str, Any]) -> None:
        run_id = row["id"]
        self._update_progress(run_id, {"state": "running", "cpu": True})
        loop = asyncio.get_running_loop()
        if row["type"] == "cluster":
            stats = await loop.run_in_executor(
                self._process_pool,
                execute_cluster_job,
                str(self.db_path),
                run_id,
                row["graph_id"],
                row["view_id"],
                params["embeddingRunId"],
                params["cluster"],
                params.get("setDefault", True),
                params.get("focus"),
            )
        elif row["type"] == "layout":
            stats = await loop.run_in_executor(
                self._process_pool,
                execute_layout_job,
                str(self.db_path),
                run_id,
                row["graph_id"],
                row["view_id"],
                params["embeddingRunId"],
                params["layout"],
                params.get("setDefault", True),
            )
        else:
            stats = await loop.run_in_executor(self._process_pool, _cpu_noop, params)
        self._update_stats(run_id, stats)

    async def _run_embed(
        self,
        row: dict,
        params: dict[str, Any],
        cancel_event: asyncio.Event,
    ) -> None:
        try:
            await execute_embedding_run(
                db_path=self.db_path,
                run_id=row["id"],
                graph_id=row["graph_id"],
                embedding_config=params["embedding"],
                representation=params.get("representation", "raw"),
                include_junk=params.get("includeJunk", False),
                summarize_run_id=params.get("summarizeRunId"),
                cancel_event=cancel_event,
                update_progress=self._update_progress,
                update_stats=self._update_stats,
                provider_factory=self._embedding_provider_factory,
                openai_api_key=self._openai_api_key,
                clock=self._clock,
                sleep=self._sleep,
            )
        except EmbeddingRunCancelled:
            cancel_event.set()

    async def _run_label(
        self,
        row: dict,
        params: dict[str, Any],
        cancel_event: asyncio.Event,
    ) -> None:
        await execute_label_run(
            db_path=self.db_path,
            run_id=row["id"],
            graph_id=row["graph_id"],
            view_id=row["view_id"],
            cluster_run_id=params["clusterRunId"],
            cluster_ids=params.get("clusterIds"),
            labeling_config=params["labeling"],
            set_default=params.get("setDefault", True),
            cancel_event=cancel_event,
            update_progress=self._update_progress,
            update_stats=self._update_stats,
            provider_factory=self._label_provider_factory,
            openai_api_key=self._openai_api_key,
            sleep=self._sleep,
        )

    async def _run_summarize(
        self,
        row: dict,
        params: dict[str, Any],
        cancel_event: asyncio.Event,
    ) -> None:
        await execute_summarize_run(
            db_path=self.db_path,
            run_id=row["id"],
            graph_id=row["graph_id"],
            embedding_config=params["embedding"],
            summarization_config=params["summarization"],
            cancel_event=cancel_event,
            update_progress=self._update_progress,
            update_stats=self._update_stats,
            provider_factory=self._summary_provider_factory,
            openai_api_key=self._openai_api_key,
            clock=self._clock,
            sleep=self._sleep,
        )

    def _run_trend(self, row: dict, params: dict[str, Any]) -> None:
        stats = execute_trend_run(
            db_path=self.db_path,
            run_id=row["id"],
            graph_id=row["graph_id"],
            view_id=row["view_id"],
            cluster_run_id=params["clusterRunId"],
            time_config=params["time"],
            window=params.get("window"),
            set_default=params.get("setDefault", True),
            update_progress=self._update_progress,
        )
        self._update_stats(row["id"], stats)

    def _mark_running(self, run_id: str, started_at: str) -> bool:
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                   SET status = 'running',
                       started_at = ?,
                       progress_json = ?
                 WHERE id = ? AND status = 'queued'
                """,
                (started_at, json.dumps({"state": "running"}, sort_keys=True), run_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    def _mark_succeeded(self, run_id: str) -> None:
        self._finish(run_id, "succeeded", self._terminal_progress(run_id, "succeeded"), None)

    def _mark_failed(self, run_id: str, error_text: str) -> None:
        self._finish(run_id, "failed", self._terminal_progress(run_id, "failed"), error_text)

    def _mark_cancelled(self, run_id: str) -> None:
        self._finish(run_id, "cancelled", self._terminal_progress(run_id, "cancelled"), None)

    def _terminal_progress(self, run_id: str, terminal_state: str) -> dict[str, Any]:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT progress_json FROM runs WHERE id = ?", (run_id,)).fetchone()
        progress = json.loads(row["progress_json"]) if row else {}
        progress["state"] = terminal_state
        return progress

    def _finish(
        self,
        run_id: str,
        status: str,
        progress: dict[str, Any],
        error_text: str | None,
    ) -> None:
        completed = now_iso()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE runs
                   SET status = ?,
                       progress_json = ?,
                       error_text = ?,
                       completed_at = ?
                 WHERE id = ?
                """,
                (status, json.dumps(progress, sort_keys=True), error_text, completed, run_id),
            )
            conn.commit()

    def _update_progress(self, run_id: str, progress: dict[str, Any]) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE runs SET progress_json = ? WHERE id = ?",
                (json.dumps(progress, sort_keys=True), run_id),
            )
            conn.commit()

    def _update_stats(self, run_id: str, stats: dict[str, Any]) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE runs SET stats_json = ? WHERE id = ?",
                (json.dumps(stats, sort_keys=True), run_id),
            )
            conn.commit()

    def _is_terminal(self, run_id: str) -> bool:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            return row is None or row["status"] in {"succeeded", "failed", "cancelled"}
