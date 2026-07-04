from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from datagraph.core.embedding_text import render_embedding_text
from datagraph.core.ids import now_iso
from datagraph.core.openai_client import (
    ClockFunc,
    SleepFunc,
    TokenBucketRateLimiter,
)
from datagraph.core.summarization import (
    SummaryProvider,
    SummaryRetryExhausted,
    effective_summarization_prompt,
    make_summary_provider,
    prompt_sha256,
    render_summary_text,
    summarize_with_retry,
    summary_result_to_json,
)
from datagraph.db import connect, fetch_all

SummaryProviderFactory = Callable[[dict[str, Any]], SummaryProvider]
ProgressCallback = Callable[[str, dict[str, Any]], None]
StatsCallback = Callable[[str, dict[str, Any]], None]
FAILED_RECORD_ID_LIMIT = 50


async def execute_summarize_run(
    *,
    db_path: Path,
    run_id: str,
    graph_id: str,
    embedding_config: dict[str, Any],
    summarization_config: dict[str, Any],
    cancel_event: asyncio.Event,
    update_progress: ProgressCallback,
    update_stats: StatsCallback,
    provider_factory: SummaryProviderFactory | None,
    openai_api_key: str | None,
    clock: ClockFunc,
    sleep: SleepFunc,
) -> None:
    model = summarization_config["model"]
    effective_prompt = effective_summarization_prompt(summarization_config)
    prompt_hash = prompt_sha256(effective_prompt)
    records = _load_record_snapshot(db_path, graph_id)
    total = len(records)
    update_progress(run_id, {"summarized": 0, "reused": 0, "failed": 0, "total": total})
    if total == 0:
        update_stats(
            run_id,
            _stats(
                total=0,
                summarized=0,
                reused=0,
                failed=0,
                failed_record_ids=[],
                provider_requests=0,
                provider_retries=0,
                model=model,
                prompt_hash=prompt_hash,
            ),
        )
        return

    rendered = []
    for row in records:
        normalized = json.loads(row["normalized_json"])
        rendered_text = render_embedding_text(normalized, embedding_config)
        rendered.append(
            {
                "recordId": row["id"],
                "recordKey": row["record_key"],
                "textHash": rendered_text.text_hash,
                "text": rendered_text.text,
            }
        )
    _insert_summary_items(db_path, run_id, rendered)

    existing_hashes = _existing_summary_hashes(
        db_path,
        model=model,
        prompt_hash=prompt_hash,
        text_hashes=[row["textHash"] for row in rendered],
    )
    reused = 0
    if existing_hashes:
        reused = sum(1 for row in rendered if row["textHash"] in existing_hashes)
        _update_item_statuses(db_path, run_id, existing_hashes, "reused")
    update_progress(run_id, {"summarized": 0, "reused": reused, "failed": 0, "total": total})

    pending = [row for row in rendered if row["textHash"] not in existing_hashes]
    if not pending:
        update_stats(
            run_id,
            _stats(
                total=total,
                summarized=0,
                reused=reused,
                failed=0,
                failed_record_ids=[],
                provider_requests=0,
                provider_retries=0,
                model=model,
                prompt_hash=prompt_hash,
            ),
        )
        return

    provider = (
        provider_factory(summarization_config)
        if provider_factory is not None
        else make_summary_provider(summarization_config, api_key=openai_api_key)
    )
    limiter = TokenBucketRateLimiter(
        requests_per_minute=int(summarization_config["requestsPerMinute"]),
        clock=clock,
        sleep=sleep,
    )
    worker_count = min(max(int(summarization_config["maxConcurrency"]), 1), len(pending))
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    for row in pending:
        queue.put_nowait(row)
    for _ in range(worker_count):
        queue.put_nowait(None)

    summarized = 0
    failed = 0
    failed_record_ids: list[str] = []
    provider_requests = 0
    provider_retries = 0
    progress_lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal summarized, failed, provider_requests, provider_retries
        while True:
            item = await queue.get()
            if item is None or cancel_event.is_set():
                return
            try:
                await limiter.acquire()
                if cancel_event.is_set():
                    return
                result, attempts = await summarize_with_retry(
                    provider,
                    effective_prompt,
                    item["text"],
                    sleep=sleep,
                )
                provider_requests += attempts
                provider_retries += attempts - 1
                _persist_summary(
                    db_path,
                    model=model,
                    prompt_hash=prompt_hash,
                    text_hash=item["textHash"],
                    result_json=summary_result_to_json(result),
                    rendered_text=render_summary_text(result),
                    junk_type=result.junk_type,
                )
                _update_record_item_status(db_path, run_id, item["recordId"], "summarized")
                async with progress_lock:
                    summarized += 1
                    update_progress(
                        run_id,
                        {
                            "summarized": summarized,
                            "reused": reused,
                            "failed": failed,
                            "total": total,
                        },
                    )
            except SummaryRetryExhausted as exc:
                provider_requests += exc.attempts
                provider_retries += exc.attempts - 1
                _update_record_item_status(db_path, run_id, item["recordId"], "failed")
                async with progress_lock:
                    failed += 1
                    if len(failed_record_ids) < FAILED_RECORD_ID_LIMIT:
                        failed_record_ids.append(item["recordId"])
                    update_progress(
                        run_id,
                        {
                            "summarized": summarized,
                            "reused": reused,
                            "failed": failed,
                            "total": total,
                        },
                    )
            except Exception:  # noqa: BLE001 - per-record failures are isolated in run stats.
                provider_requests += 1
                _update_record_item_status(db_path, run_id, item["recordId"], "failed")
                async with progress_lock:
                    failed += 1
                    if len(failed_record_ids) < FAILED_RECORD_ID_LIMIT:
                        failed_record_ids.append(item["recordId"])
                    update_progress(
                        run_id,
                        {
                            "summarized": summarized,
                            "reused": reused,
                            "failed": failed,
                            "total": total,
                        },
                    )

    tasks = [asyncio.create_task(worker()) for _ in range(worker_count)]
    try:
        await asyncio.gather(*tasks)
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    update_stats(
        run_id,
        _stats(
            total=total,
            summarized=summarized,
            reused=reused,
            failed=failed,
            failed_record_ids=failed_record_ids,
            provider_requests=provider_requests,
            provider_retries=provider_retries,
            model=model,
            prompt_hash=prompt_hash,
        ),
    )
    if summarized + reused == 0 and not cancel_event.is_set():
        raise RuntimeError("all records failed summarization")


def _stats(
    *,
    total: int,
    summarized: int,
    reused: int,
    failed: int,
    failed_record_ids: list[str],
    provider_requests: int,
    provider_retries: int,
    model: str,
    prompt_hash: str,
) -> dict[str, Any]:
    return {
        "records": total,
        "summarized": summarized,
        "reused": reused,
        "failed": failed,
        "failedRecordIds": failed_record_ids,
        "providerRequests": provider_requests,
        "providerRetries": provider_retries,
        "model": model,
        "promptHash": prompt_hash,
    }


def summarize_run_report(db_path: Path | str, run_id: str) -> dict[str, Any]:
    with connect(db_path) as conn:
        rows = fetch_all(
            conn,
            """
            SELECT si.status, r.id, r.record_key, rs.summary_json, rs.junk_type
              FROM summary_items si
              JOIN records r ON r.id = si.record_id
              LEFT JOIN runs run ON run.id = si.run_id
              LEFT JOIN record_summaries rs
                ON rs.model = json_extract(run.params_json, '$.summarization.model')
               AND rs.prompt_hash = json_extract(run.stats_json, '$.promptHash')
               AND rs.text_hash = si.text_hash
             WHERE si.run_id = ?
             ORDER BY r.timestamp_ms ASC, r.id ASC
            """,
            (run_id,),
        )
    records = []
    junk_counts: Counter[str] = Counter()
    for row in rows:
        summary = json.loads(row["summary_json"]) if row["summary_json"] else None
        junk_type = row["junk_type"] if row["junk_type"] is not None else None
        if junk_type is not None:
            junk_counts[junk_type] += 1
        record = {
            "id": row["id"],
            "recordId": row["record_key"],
            "status": row["status"],
            "junkType": junk_type,
        }
        if summary is not None:
            record["summary"] = summary
        records.append(record)
    return {
        "junkCounts": dict(sorted(junk_counts.items())),
        "records": records,
    }


def _load_record_snapshot(db_path: Path, graph_id: str) -> list[dict]:
    with connect(db_path) as conn:
        return fetch_all(
            conn,
            """
            SELECT id, record_key, normalized_json
              FROM records
             WHERE graph_id = ?
             ORDER BY timestamp_ms ASC, id ASC
            """,
            (graph_id,),
        )


def _insert_summary_items(db_path: Path, run_id: str, rendered: list[dict[str, Any]]) -> None:
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO summary_items (run_id, record_id, text_hash, status)
            VALUES (?, ?, ?, 'pending')
            """,
            [(run_id, row["recordId"], row["textHash"]) for row in rendered],
        )
        conn.commit()


def _existing_summary_hashes(
    db_path: Path,
    *,
    model: str,
    prompt_hash: str,
    text_hashes: list[str],
) -> set[str]:
    if not text_hashes:
        return set()
    existing: set[str] = set()
    with connect(db_path) as conn:
        for chunk_start in range(0, len(text_hashes), 500):
            chunk = text_hashes[chunk_start : chunk_start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT text_hash
                  FROM record_summaries
                 WHERE model = ? AND prompt_hash = ? AND text_hash IN ({placeholders})
                """,
                (model, prompt_hash, *chunk),
            ).fetchall()
            existing.update(row["text_hash"] for row in rows)
    return existing


def _update_item_statuses(
    db_path: Path,
    run_id: str,
    text_hashes: set[str],
    status: str,
) -> None:
    with connect(db_path) as conn:
        conn.executemany(
            """
            UPDATE summary_items
               SET status = ?
             WHERE run_id = ? AND text_hash = ?
            """,
            [(status, run_id, text_hash) for text_hash in text_hashes],
        )
        conn.commit()


def _update_record_item_status(db_path: Path, run_id: str, record_id: str, status: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE summary_items
               SET status = ?
             WHERE run_id = ? AND record_id = ?
            """,
            (status, run_id, record_id),
        )
        conn.commit()


def _persist_summary(
    db_path: Path,
    *,
    model: str,
    prompt_hash: str,
    text_hash: str,
    result_json: dict[str, Any],
    rendered_text: str,
    junk_type: str,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO record_summaries (
              model, prompt_hash, text_hash, summary_json,
              rendered_text, junk_type, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model,
                prompt_hash,
                text_hash,
                json.dumps(result_json, sort_keys=True),
                rendered_text,
                junk_type,
                now_iso(),
            ),
        )
        conn.commit()
