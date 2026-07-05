from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from datagraph.core.ids import new_id, now_iso
from datagraph.core.labeling import (
    LabelProvider,
    LabelResult,
    label_with_retry,
    make_label_provider,
)
from datagraph.core.openai_client import (
    SleepFunc,
    add_token_usage,
    token_usage_dict,
    zero_token_usage,
)
from datagraph.db import connect, fetch_all, fetch_one

LabelProviderFactory = Callable[[dict[str, Any]], LabelProvider]
ProgressCallback = Callable[[str, dict[str, Any]], None]
StatsCallback = Callable[[str, dict[str, Any]], None]

RECORD_TEXT_LIMIT = 700
TEXT_SOURCE_RAW = "raw_customer_text"
TEXT_SOURCE_SUMMARY = "summary_rendered_text"
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
    set_default: bool = True,
) -> None:
    cluster_run = _load_cluster_run(db_path, graph_id, view_id, cluster_run_id)
    effective_prompt = effective_label_prompt(labeling_config)
    prompt_hash = prompt_sha256(effective_prompt)
    top_k = int(labeling_config["topK"])
    example_text_limit = int(labeling_config.get("exampleTextLimit", RECORD_TEXT_LIMIT))
    text_source = resolve_label_text_source(
        db_path,
        graph_id=graph_id,
        cluster_run=cluster_run,
        requested=labeling_config.get("textSource", "auto"),
    )
    targets = _load_targets(
        db_path,
        cluster_run_id,
        cluster_ids,
        top_k=top_k,
        text_source=text_source,
    )
    total = len(targets)
    if total == 0:
        raise RuntimeError("label run has no target clusters to label")
    fallback_raw_count = sum(
        1
        for target in targets
        for record in target.records[:top_k]
        if record.get("label_text_source") == TEXT_SOURCE_RAW
        and text_source.resolved == TEXT_SOURCE_SUMMARY
    )

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
    token_usage = zero_token_usage()
    progress_lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal labeled, failed, provider_requests, token_usage
        while True:
            target = await queue.get()
            if target is None or cancel_event.is_set():
                return
            representatives = build_representative_blocks(
                target.records,
                top_k=top_k,
                example_text_limit=example_text_limit,
            )
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
                    token_usage = add_token_usage(token_usage, result.token_usage)
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
        "textSource": text_source.resolved,
        "fallbackRawCount": fallback_raw_count,
        "exampleTextLimit": example_text_limit,
        "tokenUsage": token_usage_dict(token_usage),
    }
    update_stats(run_id, stats)
    if labeled == 0 and not cancel_event.is_set():
        raise RuntimeError(
            f"all target clusters failed labeling; failedClusterIds={sorted(failed_cluster_ids)}"
        )
    if set_default and not cancel_event.is_set():
        _set_default_label_run(db_path, graph_id, view_id, run_id)


def effective_label_prompt(labeling_config: dict[str, Any]) -> str:
    prompt = labeling_config.get("prompt")
    base = prompt if isinstance(prompt, str) and prompt.strip() else DEFAULT_LABEL_PROMPT
    prompt_append = labeling_config.get("promptAppend")
    if isinstance(prompt_append, str) and prompt_append.strip():
        return f"{base}\n\nAdditional brand instructions:\n{prompt_append}"
    return base


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def build_representative_blocks(
    records: list[dict[str, Any]],
    *,
    top_k: int,
    example_text_limit: int = RECORD_TEXT_LIMIT,
) -> list[str]:
    return [
        detail["block"]
        for detail in representative_block_details(
            records,
            top_k=top_k,
            example_text_limit=example_text_limit,
        )
    ]


def representative_block_details(
    records: list[dict[str, Any]],
    *,
    top_k: int,
    example_text_limit: int = RECORD_TEXT_LIMIT,
) -> list[dict[str, Any]]:
    blocks = []
    for index, record in enumerate(records[:top_k], start=1):
        lines = [f"Record {index}", f"sourceType: {record['source_type']}"]
        if record.get("title"):
            lines.append(f"title: {_truncate(record['title'], example_text_limit)}")
        text = record.get("label_text", record["customer_text"])
        truncated = _truncate(text, example_text_limit)
        lines.append(f"customerText: {truncated}")
        blocks.append(
            {
                "id": record.get("id"),
                "recordId": record.get("record_key", record.get("id")),
                "titlePresent": bool(record.get("title")),
                "textSource": record.get("label_text_source", TEXT_SOURCE_RAW),
                "truncationApplied": len(text) > example_text_limit,
                "block": "\n".join(lines),
            }
        )
    return blocks


def _truncate(value: str, limit: int = RECORD_TEXT_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


class LabelTarget:
    def __init__(self, cluster_id: int, records: list[dict[str, Any]]) -> None:
        self.cluster_id = cluster_id
        self.records = records


@dataclass(frozen=True)
class LabelTextSource:
    resolved: str
    summarize_run_id: str | None = None


class LabelTextSourceError(RuntimeError):
    pass


def resolve_label_text_source(
    db_path: Path | str,
    *,
    graph_id: str,
    cluster_run: dict[str, Any],
    requested: str,
) -> LabelTextSource:
    if requested == "raw":
        return LabelTextSource(TEXT_SOURCE_RAW)
    summarize_run_id = _summarize_run_id_for_cluster_run(db_path, graph_id, cluster_run)
    if requested == "summary" and summarize_run_id is None:
        raise LabelTextSourceError(
            "summary label text requires a summary-backed embedding run; "
            "POST /api/graphs/{gid}/summarize first, then embed with representation=summary"
        )
    if requested == "summary" or summarize_run_id is not None:
        return LabelTextSource(TEXT_SOURCE_SUMMARY, summarize_run_id=summarize_run_id)
    return LabelTextSource(TEXT_SOURCE_RAW)


def _summarize_run_id_for_cluster_run(
    db_path: Path | str,
    graph_id: str,
    cluster_run: dict[str, Any],
) -> str | None:
    refs = json.loads(cluster_run["input_refs_json"])
    embedding_run_id = refs.get("embeddingRunId")
    if not embedding_run_id:
        return None
    with connect(db_path) as conn:
        row = fetch_one(
            conn,
            """
            SELECT input_refs_json
              FROM runs
             WHERE id = ? AND graph_id = ? AND type = 'embed' AND status = 'succeeded'
            """,
            (embedding_run_id, graph_id),
        )
    if row is None:
        return None
    return json.loads(row["input_refs_json"]).get("summarizeRunId")


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
    text_source: LabelTextSource | None = None,
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
                text_source=text_source or LabelTextSource(TEXT_SOURCE_RAW),
            )
            targets.append(LabelTarget(int(summary["cluster_id"]), records))
    return targets


def _load_records(
    conn: Any,
    cluster_run_id: str,
    cluster_id: int,
    record_ids: list[str],
    *,
    text_source: LabelTextSource,
) -> list[dict[str, Any]]:
    if not record_ids:
        return []
    placeholders = ", ".join("?" for _ in record_ids)
    summary_join = ""
    summary_select = "NULL AS summary_text"
    params: tuple[object, ...]
    if text_source.resolved == TEXT_SOURCE_SUMMARY and text_source.summarize_run_id:
        summary_select = "rs.rendered_text AS summary_text"
        summary_join = """
          LEFT JOIN summary_items si
            ON si.run_id = ? AND si.record_id = r.id
          LEFT JOIN runs summarize_run
            ON summarize_run.id = si.run_id
          LEFT JOIN record_summaries rs
            ON rs.model = json_extract(summarize_run.params_json, '$.summarization.model')
           AND rs.prompt_hash = json_extract(summarize_run.stats_json, '$.promptHash')
           AND rs.text_hash = si.text_hash
        """
        params = (
            text_source.summarize_run_id,
            cluster_run_id,
            cluster_id,
            *record_ids,
        )
    else:
        params = (cluster_run_id, cluster_id, *record_ids)
    rows = fetch_all(
        conn,
        f"""
        SELECT r.id, r.record_key, r.source_type, r.title, r.customer_text, {summary_select}
          FROM cluster_memberships cm
          JOIN records r ON r.id = cm.record_id
          {summary_join}
         WHERE cm.run_id = ? AND cm.cluster_id = ? AND cm.record_id IN ({placeholders})
        """,
        params,
    )
    by_id = {}
    for row in rows:
        item = dict(row)
        if text_source.resolved == TEXT_SOURCE_SUMMARY and item["summary_text"]:
            item["label_text"] = item["summary_text"]
            item["label_text_source"] = TEXT_SOURCE_SUMMARY
        else:
            item["label_text"] = item["customer_text"]
            item["label_text_source"] = TEXT_SOURCE_RAW
        by_id[item["id"]] = item
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


def label_run_report(db_path: Path | str, graph_id: str, run_id: str) -> dict[str, Any]:
    with connect(db_path) as conn:
        run = fetch_one(
            conn,
            """
            SELECT *
              FROM runs
             WHERE id = ? AND graph_id = ? AND type = 'label'
            """,
            (run_id, graph_id),
        )
        if run is None:
            raise KeyError("label run not found")
        params = json.loads(run["params_json"])
        cluster_run = fetch_one(
            conn,
            """
            SELECT *
              FROM runs
             WHERE id = ? AND graph_id = ? AND type = 'cluster'
            """,
            (params["clusterRunId"], graph_id),
        )
        if cluster_run is None:
            raise KeyError("cluster run not found")

    labeling_config = params["labeling"]
    top_k = int(labeling_config["topK"])
    example_text_limit = int(labeling_config.get("exampleTextLimit", RECORD_TEXT_LIMIT))
    text_source = resolve_label_text_source(
        db_path,
        graph_id=graph_id,
        cluster_run=cluster_run,
        requested=labeling_config.get("textSource", "auto"),
    )
    targets = _load_targets(
        Path(db_path),
        params["clusterRunId"],
        params.get("clusterIds"),
        top_k=top_k,
        text_source=text_source,
    )
    prompt_hash = prompt_sha256(effective_label_prompt(labeling_config))
    clusters = [
        {
            "clusterId": target.cluster_id,
            "model": labeling_config["model"],
            "promptHash": prompt_hash,
            "textSource": text_source.resolved,
            "representatives": representative_block_details(
                target.records,
                top_k=top_k,
                example_text_limit=example_text_limit,
            ),
        }
        for target in targets
    ]
    labels = _labels_for_run(db_path, run_id)
    return {
        "model": labeling_config["model"],
        "promptHash": prompt_hash,
        "textSource": text_source.resolved,
        "exampleTextLimit": example_text_limit,
        "clusters": clusters,
        "labelQuality": label_quality(labels),
    }


def _labels_for_run(db_path: Path | str, run_id: str) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = fetch_all(
            conn,
            """
            SELECT cluster_id, label
              FROM cluster_labels
             WHERE label_run_id = ?
             ORDER BY cluster_id ASC
            """,
            (run_id,),
        )
    return [{"clusterId": int(row["cluster_id"]), "label": row["label"]} for row in rows]


def label_quality(labels: list[dict[str, Any]]) -> dict[str, Any]:
    by_normalized: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in labels:
        by_normalized[_normalize_label(row["label"])].append(row)
    exact_duplicate_groups = [
        {
            "label": rows[0]["label"],
            "clusterIds": [row["clusterId"] for row in rows],
        }
        for _, rows in sorted(by_normalized.items())
        if len(rows) > 1
    ]

    near_duplicates = []
    for index, left in enumerate(labels):
        for right in labels[index + 1 :]:
            left_norm = _normalize_label(left["label"])
            right_norm = _normalize_label(right["label"])
            if left_norm == right_norm:
                continue
            score = _token_overlap(left_norm, right_norm)
            if score >= 0.8:
                near_duplicates.append(
                    {
                        "clusterIds": [left["clusterId"], right["clusterId"]],
                        "labels": [left["label"], right["label"]],
                        "overlap": round(score, 3),
                    }
                )

    generic_flags = []
    for row in labels:
        tokens = _label_tokens(row["label"])
        reasons = []
        if len(tokens) < 3:
            reasons.append("very_short")
        if _is_generic_label(tokens):
            reasons.append("generic")
        if reasons:
            generic_flags.append(
                {
                    "clusterId": row["clusterId"],
                    "label": row["label"],
                    "reasons": reasons,
                }
            )
    return {
        "exactDuplicateGroups": exact_duplicate_groups,
        "nearDuplicates": sorted(
            near_duplicates,
            key=lambda row: (-row["overlap"], row["clusterIds"]),
        ),
        "genericFlags": generic_flags,
    }


def _normalize_label(value: str) -> str:
    return " ".join(_label_tokens(value))


def _label_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _token_overlap(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))


def _is_generic_label(tokens: list[str]) -> bool:
    if not tokens:
        return True
    generic_tokens = {
        "customer",
        "customers",
        "feedback",
        "general",
        "issue",
        "issues",
        "misc",
        "miscellaneous",
        "other",
        "request",
        "requests",
        "support",
        "topic",
        "topics",
    }
    return set(tokens).issubset(generic_tokens)
