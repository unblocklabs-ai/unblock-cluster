from __future__ import annotations

import json
from collections import Counter
from typing import Any

from fastapi import HTTPException, status

from datagraph.db import fetch_all

NONE_BUCKET = "(none)"
OTHER_BUCKET = "(other)"
TOP_FACET_VALUES = 20
ALLOWED_FACET_FORMS = (
    "sourceType, sourceName, product, sku, sentiment, rating, tags, or metadata.<key>"
)
FIELD_COLUMNS = {
    "sourceType": "r.source_type",
    "sourceName": "r.source_name",
    "product": "r.product",
    "sku": "r.sku",
    "sentiment": "r.sentiment",
    "rating": "r.rating",
}


def validate_facet_by(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _raise_invalid_facet("must be a non-empty string")
    facet_by = value.strip()
    if facet_by in FIELD_COLUMNS or facet_by == "tags":
        return facet_by
    if facet_by.startswith("metadata."):
        key = facet_by.removeprefix("metadata.")
        if key and "." not in key:
            return facet_by
    _raise_invalid_facet(f"must be one of {ALLOWED_FACET_FORMS}")


def facet_counts_by_cluster(
    conn: Any,
    cluster_run_id: str,
    facet_by: str | None,
    *,
    cluster_ids: list[int] | None = None,
) -> dict[int, dict[str, int]]:
    if facet_by is None:
        return {}
    where = "cm.run_id = ? AND cm.cluster_id != -1"
    params: list[Any] = [cluster_run_id]
    if cluster_ids:
        placeholders = ", ".join("?" for _ in cluster_ids)
        where += f" AND cm.cluster_id IN ({placeholders})"
        params.extend(cluster_ids)
    value_expr = _value_expression(facet_by)
    rows = fetch_all(
        conn,
        f"""
        SELECT cm.cluster_id, {value_expr} AS facet_value
          FROM cluster_memberships cm
          JOIN records r ON r.id = cm.record_id
         WHERE {where}
        """,
        tuple(params),
    )
    counts: dict[int, Counter[str]] = {}
    for row in rows:
        cluster_id = int(row["cluster_id"])
        counts.setdefault(cluster_id, Counter())
        for bucket in _buckets(facet_by, row["facet_value"]):
            counts[cluster_id][bucket] += 1
    return {cluster_id: _cap_counts(counter) for cluster_id, counter in counts.items()}


def _value_expression(facet_by: str) -> str:
    if facet_by in FIELD_COLUMNS:
        return FIELD_COLUMNS[facet_by]
    if facet_by == "tags":
        return "r.tags_json"
    return "r.metadata_json"


def _buckets(facet_by: str, value: Any) -> list[str]:
    if facet_by == "tags":
        tags = _loads_json(value, default=[])
        if not isinstance(tags, list) or not tags:
            return [NONE_BUCKET]
        buckets = [_bucket_value(tag) for tag in tags]
        return buckets or [NONE_BUCKET]
    if facet_by.startswith("metadata."):
        metadata = _loads_json(value, default={})
        key = facet_by.removeprefix("metadata.")
        if not isinstance(metadata, dict) or key not in metadata:
            return [NONE_BUCKET]
        return [_bucket_value(metadata.get(key))]
    return [_bucket_value(value)]


def _bucket_value(value: Any) -> str:
    if value is None or value == "":
        return NONE_BUCKET
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return value if value else NONE_BUCKET
    return json.dumps(value, sort_keys=True)


def _cap_counts(counter: Counter[str]) -> dict[str, int]:
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    top = ordered[:TOP_FACET_VALUES]
    other = sum(count for _, count in ordered[TOP_FACET_VALUES:])
    result = {value: count for value, count in top}
    if other:
        result[OTHER_BUCKET] = other
    return result


def _loads_json(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _raise_invalid_facet(message: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=[{"field": "facetBy", "message": message}],
    )
