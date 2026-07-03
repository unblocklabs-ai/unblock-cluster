from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from datagraph.core.ids import new_id, now_iso
from datagraph.core.labeling import (
    LabelProvider,
    LabelResult,
    label_with_retry,
    make_label_provider,
)
from datagraph.core.openai_client import SleepFunc
from datagraph.db import connect, fetch_all, fetch_one

LabelProviderFactory = Callable[[dict[str, Any]], LabelProvider]
ProgressCallback = Callable[[str, dict[str, Any]], None]
StatsCallback = Callable[[str, dict[str, Any]], None]

RECORD_TEXT_LIMIT = 700
DEFAULT_LABEL_PROMPT = """You are labeling a cluster of consumer feedback records for a supplement
direct-to-consumer brand.

Given representative records from one semantic cluster, produce:
1. A short topic label, 3-8 words.
2. A concise summary of the common customer issue or theme.
3. Key symptoms, phrases, or product references that justify the label.
4. Suggested tags.

Avoid overfitting to one record. Prefer labels that a support operations or
customer insights lead would understand in a dashboard. If the examples are
incoherent or weakly related, say so."""


async def execute_label_run(
    *,
    db_path: Path,
    run_id: str,
    graph_id: str,
    view_id: str,
    cluster_run_id: str,
    cluster_ids: list[int] | None,
    labeling_config: dict[str, Any],
    cancel_event: asyncio.Event,
    update_progress: ProgressCallback,
    update_stats: StatsCallback,
    provider_factory: LabelProviderFactory | None,
    openai_api_key: str | None,
    sleep: SleepFunc,
) -> None:
    _load_cluster_run(db_path, graph_id, view_id, cluster_run_id)
    effective_prompt = effective_label_prompt(labeling_config)
    prompt_hash = prompt_sha256(effective_prompt)
    top_k = int(labeling_config["topK"])
    targets = _load_targets(db_path, cluster_run_id, cluster_ids, top_k=top_k)
    total = len(targets)
    if total == 0:
        raise RuntimeError("label run has no target clusters to label")

    update_progress(run_id, {"labeled": 0, "failed": 0, "total": total})
    provider = (
        provider_factory(labeling_config)
        if provider_factory is not None
        else make_label_provider(labeling_config, api_key=openai_api_key)
    )
    provider_requests = 0

    class CountingProvider:
        async def label_cluster(self, prompt: str, representatives: list[str]) -> LabelResult:
            nonlocal provider_requests
            provider_requests += 1
            return await provider.label_cluster(prompt, representatives)

    counting_provider = CountingProvider()
    semaphore = asyncio.Semaphore(4)
    queue: asyncio.Queue[LabelTarget | None] = asyncio.Queue()
    for target in targets:
        queue.put_nowait(target)
    worker_count = min(4, total)
    for _ in range(worker_count):
        queue.put_nowait(None)

    labeled = 0
    failed = 0
    failed_cluster_ids: list[int] = []
    progress_lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal labeled, failed, provider_requests
        while True:
            target = await queue.get()
            if target is None or cancel_event.is_set():
                return
            representatives = build_representative_blocks(target.records, top_k=top_k)
            try:
                async with semaphore:
                    result, _attempts = await label_with_retry(
                        counting_provider,
                        effective_prompt,
                        representatives,
                        sleep=sleep,
                    )
                if cancel_event.is_set():
                    return
                _persist_label(
                    db_path,
                    run_id=run_id,
                    cluster_run_id=cluster_run_id,
                    cluster_id=target.cluster_id,
                    model=labeling_config["model"],
                    prompt_hash=prompt_hash,
                    top_k=top_k,
                    result=result,
                )
                async with progress_lock:
                    labeled += 1
                    update_progress(run_id, {"labeled": labeled, "failed": failed, "total": total})
            except Exception:  # noqa: BLE001 - per-cluster failures are tracked in run stats.
                async with progress_lock:
                    failed += 1
                    failed_cluster_ids.append(target.cluster_id)
                    update_progress(run_id, {"labeled": labeled, "failed": failed, "total": total})
            if cancel_event.is_set():
                return

    tasks = [asyncio.create_task(worker()) for _ in range(worker_count)]
    try:
        await asyncio.gather(*tasks)
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    stats = {
        "targets": total,
        "labeled": labeled,
        "failed": failed,
        "failedClusterIds": sorted(failed_cluster_ids),
        "providerRequests": provider_requests,
        "model": labeling_config["model"],
        "promptHash": prompt_hash,
    }
    update_stats(run_id, stats)
    if labeled == 0 and not cancel_event.is_set():
        raise RuntimeError(
            f"all target clusters failed labeling; failedClusterIds={sorted(failed_cluster_ids)}"
        )
    if not cancel_event.is_set():
        _set_default_label_run(db_path, graph_id, view_id, run_id)


def effective_label_prompt(labeling_config: dict[str, Any]) -> str:
    prompt = labeling_config.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt
    return DEFAULT_LABEL_PROMPT


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def build_representative_blocks(records: list[dict[str, Any]], *, top_k: int) -> list[str]:
    blocks = []
    for index, record in enumerate(records[:top_k], start=1):
        lines = [f"Record {index}", f"sourceType: {record['source_type']}"]
        if record.get("title"):
            lines.append(f"title: {_truncate(record['title'])}")
        lines.append(f"customerText: {_truncate(record['customer_text'])}")
        blocks.append("\n".join(lines))
    return blocks


def _truncate(value: str, limit: int = RECORD_TEXT_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


class LabelTarget:
    def __init__(self, cluster_id: int, records: list[dict[str, Any]]) -> None:
        self.cluster_id = cluster_id
        self.records = records


def _load_cluster_run(db_path: Path, graph_id: str, view_id: str, cluster_run_id: str) -> dict:
    with connect(db_path) as conn:
        row = fetch_one(
            conn,
            """
            SELECT *
              FROM runs
             WHERE id = ? AND graph_id = ? AND view_id = ?
               AND type = 'cluster' AND status = 'succeeded'
            """,
            (cluster_run_id, graph_id, view_id),
        )
    if row is None:
        raise RuntimeError("clusterRunId must reference a succeeded cluster run for this view")
    return row


def _load_targets(
    db_path: Path,
    cluster_run_id: str,
    cluster_ids: list[int] | None,
    *,
    top_k: int,
) -> list[LabelTarget]:
    query = "SELECT * FROM cluster_summaries WHERE run_id = ?"
    params: list[object] = [cluster_run_id]
    if cluster_ids is not None:
        if not cluster_ids:
            return []
        placeholders = ", ".join("?" for _ in cluster_ids)
        query += f" AND cluster_id IN ({placeholders})"
        params.extend(cluster_ids)
    query += " ORDER BY cluster_id ASC"
    with connect(db_path) as conn:
        summaries = fetch_all(conn, query, tuple(params))
        targets = []
        for summary in summaries:
            representative_ids = json.loads(summary["representative_record_ids_json"])[:top_k]
            records = _load_records(
                conn,
                cluster_run_id,
                int(summary["cluster_id"]),
                representative_ids,
            )
            targets.append(LabelTarget(int(summary["cluster_id"]), records))
    return targets


def _load_records(
    conn: Any,
    cluster_run_id: str,
    cluster_id: int,
    record_ids: list[str],
) -> list[dict[str, Any]]:
    if not record_ids:
        return []
    placeholders = ", ".join("?" for _ in record_ids)
    rows = fetch_all(
        conn,
        f"""
        SELECT r.id, r.source_type, r.title, r.customer_text
          FROM cluster_memberships cm
          JOIN records r ON r.id = cm.record_id
         WHERE cm.run_id = ? AND cm.cluster_id = ? AND cm.record_id IN ({placeholders})
        """,
        (cluster_run_id, cluster_id, *record_ids),
    )
    by_id = {row["id"]: row for row in rows}
    return [by_id[record_id] for record_id in record_ids if record_id in by_id]


def _persist_label(
    db_path: Path,
    *,
    run_id: str,
    cluster_run_id: str,
    cluster_id: int,
    model: str,
    prompt_hash: str,
    top_k: int,
    result: LabelResult,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO cluster_labels (
              id, label_run_id, cluster_run_id, cluster_id, model,
              prompt_hash, top_k, label, summary, key_signals_json,
              tags_json, coherent, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("lbl"),
                run_id,
                cluster_run_id,
                cluster_id,
                model,
                prompt_hash,
                top_k,
                result.label,
                result.summary,
                json.dumps(result.key_signals, sort_keys=True),
                json.dumps(result.tags, sort_keys=True),
                1 if result.coherent else 0,
                now_iso(),
            ),
        )
        conn.commit()


def _set_default_label_run(db_path: Path, graph_id: str, view_id: str, run_id: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE views
               SET default_label_run_id = ?, updated_at = ?
             WHERE id = ? AND graph_id = ?
            """,
            (run_id, now_iso(), view_id, graph_id),
        )
        conn.commit()
