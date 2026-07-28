from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from datagraph.core.embedding_text import render_embedding_text
from datagraph.core.openai_client import (
    ClockFunc,
    EmbeddingProvider,
    SleepFunc,
    TokenBucketRateLimiter,
    embed_with_retry,
    make_embedding_provider,
    pack_embedding_batches,
)
from datagraph.core.vectors import existing_vector_hashes, pack_vector
from datagraph.db import connect, fetch_all

ProviderFactory = Callable[[dict[str, Any]], EmbeddingProvider]
ProgressCallback = Callable[[str, dict[str, Any]], None]
StatsCallback = Callable[[str, dict[str, Any]], None]


class EmbeddingRunCancelled(Exception):
    pass


async def execute_embedding_run(
    *,
    db_path: Path,
    run_id: str,
    graph_id: str,
    embedding_config: dict[str, Any],
    cancel_event: asyncio.Event,
    update_progress: ProgressCallback,
    update_stats: StatsCallback,
    provider_factory: ProviderFactory | None,
    openai_api_key: str | None,
    clock: ClockFunc,
    sleep: SleepFunc,
    representation: str = "raw",
    include_junk: bool = False,
    summarize_run_id: str | None = None,
) -> None:
    model = embedding_config["model"]
    dimensions = int(embedding_config.get("dimensions") or 1536)
    if representation == "summary":
        records, representation_stats = _load_summary_snapshot(
            db_path,
            graph_id,
            embedding_config,
            summarize_run_id,
            include_junk=include_junk,
        )
    else:
        records = _load_raw_snapshot(db_path, graph_id, embedding_config)
        representation_stats = {
            "representation": "raw",
            "includeJunk": False,
            "missingSummaries": 0,
            "skippedJunk": 0,
            "sourceRecords": len(records),
        }
    total = len(records)
    update_progress(run_id, {"embedded": 0, "reused": 0, "total": total})
    if total == 0:
        update_stats(
            run_id,
            {
                "records": 0,
                "uniqueTexts": 0,
                "reused": 0,
                "providerRequests": 0,
                "providerRetries": 0,
                "model": model,
                "dimensions": dimensions,
                "tokenUsage": _embedding_token_usage(0),
                **representation_stats,
            },
        )
        return

    rendered = []
    by_hash: dict[str, list[str]] = defaultdict(list)
    text_by_hash: dict[str, str] = {}
    for row in records:
        rendered.append((row["id"], row["textHash"], row["text"]))
        by_hash[row["textHash"]].append(row["id"])
        text_by_hash.setdefault(row["textHash"], row["text"])

    _insert_embedding_items(db_path, run_id, rendered)
    existing_hashes = _existing_vector_hashes(db_path, model, dimensions, list(by_hash))
    reused = sum(len(by_hash[text_hash]) for text_hash in existing_hashes)
    if reused:
        _update_item_statuses(db_path, run_id, existing_hashes, "reused")
    update_progress(run_id, {"embedded": 0, "reused": reused, "total": total})

    missing_hashes = [text_hash for text_hash in text_by_hash if text_hash not in existing_hashes]
    if not missing_hashes:
        update_stats(
            run_id,
            {
                "records": total,
                "uniqueTexts": len(text_by_hash),
                "reused": reused,
                "providerRequests": 0,
                "providerRetries": 0,
                "model": model,
                "dimensions": dimensions,
                "tokenUsage": _embedding_token_usage(0),
                **representation_stats,
            },
        )
        return

    provider = (
        provider_factory(embedding_config)
        if provider_factory is not None
        else make_embedding_provider(embedding_config, api_key=openai_api_key)
    )
    limiter = TokenBucketRateLimiter(
        requests_per_minute=int(embedding_config["requestsPerMinute"]),
        clock=clock,
        sleep=sleep,
    )
    semaphore = asyncio.Semaphore(max(int(embedding_config["maxConcurrency"]), 1))
    batches = pack_embedding_batches([text_by_hash[text_hash] for text_hash in missing_hashes])
    batch_hashes = _batch_hashes(missing_hashes, batches)
    embedded_items = 0
    provider_requests = 0
    provider_retries = 0
    prompt_tokens_total = 0
    progress_lock = asyncio.Lock()
    batch_queue: asyncio.Queue[tuple[list[str], list[str]] | None] = asyncio.Queue()
    for hashes, batch in zip(batch_hashes, batches, strict=True):
        batch_queue.put_nowait((hashes, batch.texts))
    worker_count = min(max(int(embedding_config["maxConcurrency"]), 1), len(batches))
    for _ in range(worker_count):
        batch_queue.put_nowait(None)

    async def worker() -> None:
        nonlocal embedded_items, provider_requests, provider_retries, prompt_tokens_total
        while True:
            item = await batch_queue.get()
            if item is None:
                return
            hashes, texts = item
            if cancel_event.is_set():
                raise EmbeddingRunCancelled
            async with semaphore:
                await limiter.acquire()
                if cancel_event.is_set():
                    raise EmbeddingRunCancelled
                vectors, attempts, prompt_tokens = await embed_with_retry(
                    provider,
                    texts,
                    sleep=sleep,
                )
                provider_requests += 1
                provider_retries += attempts - 1
                prompt_tokens_total += prompt_tokens
            _store_vectors(db_path, model, dimensions, hashes, vectors)
            _update_item_statuses(db_path, run_id, hashes, "embedded")
            async with progress_lock:
                embedded_items += sum(len(by_hash[text_hash]) for text_hash in hashes)
                update_progress(
                    run_id,
                    {"embedded": embedded_items, "reused": reused, "total": total},
                )
            if cancel_event.is_set():
                raise EmbeddingRunCancelled

    tasks = [asyncio.create_task(worker()) for _ in range(worker_count)]
    try:
        await asyncio.gather(*tasks)
    except EmbeddingRunCancelled:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        update_stats(
            run_id,
            {
                "records": total,
                "uniqueTexts": len(text_by_hash),
                "reused": reused,
                "providerRequests": provider_requests,
                "providerRetries": provider_retries,
                "model": model,
                "dimensions": dimensions,
                "tokenUsage": _embedding_token_usage(prompt_tokens_total),
                **representation_stats,
            },
        )
        raise
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    update_stats(
        run_id,
        {
            "records": total,
            "uniqueTexts": len(text_by_hash),
            "reused": reused,
            "providerRequests": provider_requests,
            "providerRetries": provider_retries,
            "model": model,
            "dimensions": dimensions,
            "tokenUsage": _embedding_token_usage(prompt_tokens_total),
            **representation_stats,
        },
    )


def _embedding_token_usage(prompt_tokens: int) -> dict[str, int]:
    return {
        "promptTokens": prompt_tokens,
        "completionTokens": 0,
        "totalTokens": prompt_tokens,
    }


def _load_raw_snapshot(
    db_path: Path,
    graph_id: str,
    embedding_config: dict[str, Any],
) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = fetch_all(
            conn,
            """
            SELECT id, normalized_json
              FROM records
             WHERE graph_id = ? AND is_active = 1
             ORDER BY timestamp_ms ASC, id ASC
            """,
            (graph_id,),
        )
    rendered = []
    for row in rows:
        normalized = json.loads(row["normalized_json"])
        rendered_text = render_embedding_text(normalized, embedding_config)
        rendered.append(
            {"id": row["id"], "textHash": rendered_text.text_hash, "text": rendered_text.text}
        )
    return rendered


def _load_summary_snapshot(
    db_path: Path,
    graph_id: str,
    embedding_config: dict[str, Any],
    summarize_run_id: str | None,
    *,
    include_junk: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if summarize_run_id is None:
        raise RuntimeError("summary representation requires summarizeRunId")
    with connect(db_path) as conn:
        summarize_run = fetch_all(
            conn,
            """
            SELECT *
              FROM runs
             WHERE id = ? AND graph_id = ? AND type = 'summarize' AND status = 'succeeded'
            """,
            (summarize_run_id, graph_id),
        )
        if not summarize_run:
            raise RuntimeError("summarizeRunId must reference a succeeded summarize run")
        stats = json.loads(summarize_run[0]["stats_json"])
        params = json.loads(summarize_run[0]["params_json"])
        rows = fetch_all(
            conn,
            """
            SELECT r.id, r.normalized_json, rs.rendered_text, rs.junk_type
              FROM records r
              LEFT JOIN summary_items si
                ON si.run_id = ? AND si.record_id = r.id
              LEFT JOIN record_summaries rs
                ON rs.model = ?
               AND rs.prompt_hash = ?
               AND rs.text_hash = si.text_hash
             WHERE r.graph_id = ? AND r.is_active = 1
             ORDER BY r.timestamp_ms ASC, r.id ASC
            """,
            (
                summarize_run_id,
                params["summarization"]["model"],
                stats["promptHash"],
                graph_id,
            ),
        )
    rendered = []
    missing = 0
    skipped_junk = 0
    for row in rows:
        if row["rendered_text"] is None:
            missing += 1
            continue
        if not include_junk and row["junk_type"] != "none":
            skipped_junk += 1
            continue
        text = row["rendered_text"]
        rendered.append(
            {
                "id": row["id"],
                "textHash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "text": text,
            }
        )
    return rendered, {
        "representation": "summary",
        "includeJunk": include_junk,
        "summarizeRunId": summarize_run_id,
        "missingSummaries": missing,
        "skippedJunk": skipped_junk,
        "sourceRecords": len(rows),
    }


def _insert_embedding_items(
    db_path: Path,
    run_id: str,
    rendered: list[tuple[str, str, str]],
) -> None:
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO embedding_items (run_id, record_id, text_hash, status)
            VALUES (?, ?, ?, 'pending')
            """,
            [(run_id, record_id, text_hash) for record_id, text_hash, _ in rendered],
        )
        conn.commit()


def _existing_vector_hashes(
    db_path: Path,
    model: str,
    dimensions: int,
    text_hashes: list[str],
) -> set[str]:
    return existing_vector_hashes(
        db_path,
        model=model,
        dimensions=dimensions,
        text_hashes=text_hashes,
    )


def _update_item_statuses(
    db_path: Path,
    run_id: str,
    text_hashes: list[str] | set[str],
    status: str,
) -> None:
    with connect(db_path) as conn:
        conn.executemany(
            """
            UPDATE embedding_items
               SET status = ?
             WHERE run_id = ? AND text_hash = ?
            """,
            [(status, run_id, text_hash) for text_hash in text_hashes],
        )
        conn.commit()


def _store_vectors(
    db_path: Path,
    model: str,
    dimensions: int,
    text_hashes: list[str],
    vectors: list[Any],
) -> None:
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO embedding_vectors (
              model, dimensions, text_hash, vector, created_at
            )
            VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            """,
            [
                (model, dimensions, text_hash, pack_vector(vector))
                for text_hash, vector in zip(text_hashes, vectors, strict=True)
            ],
        )
        conn.commit()


def _batch_hashes(text_hashes: list[str], batches: list[Any]) -> list[list[str]]:
    grouped = []
    index = 0
    for batch in batches:
        grouped.append(text_hashes[index : index + len(batch.texts)])
        index += len(batch.texts)
    return grouped
