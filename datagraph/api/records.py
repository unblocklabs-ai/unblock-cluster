from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from datagraph.core.ids import new_id, now_iso
from datagraph.core.provenance import external_provenance_by_record
from datagraph.core.records import ValidatedRecord, validate_record
from datagraph.core.time import TimestampValidationError, parse_timestamp
from datagraph.db import connect, fetch_all, fetch_one

router = APIRouter(prefix="/api/graphs/{graph_id}/records", tags=["records"])


@router.post("")
async def upsert_records(request: Request, graph_id: str, body: dict[str, Any]) -> dict[str, Any]:
    _ensure_graph(request.app.state.settings.db_path, graph_id)
    _reject_unknown_body(body, {"records", "onInvalid"})
    records = body.get("records")
    if not isinstance(records, list):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "records", "message": "must be a list"}],
        )
    if len(records) > 1000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "records", "message": "must contain at most 1000 records"}],
        )
    on_invalid = body.get("onInvalid", "reject")
    if on_invalid not in {"reject", "skip"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": "onInvalid", "message": 'must be "reject" or "skip"'}],
        )

    valid: list[ValidatedRecord] = []
    rejected: list[dict] = []
    for index, record in enumerate(records):
        parsed, error = validate_record(record, index=index)
        if error is not None:
            rejected.append(error)
        elif parsed is not None:
            valid.append(parsed)

    if rejected and on_invalid == "reject":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"rejected": rejected},
        )

    created, updated = _upsert_valid_records(request.app.state.settings.db_path, graph_id, valid)
    return {
        "created": created,
        "updated": updated,
        "rejected": rejected if on_invalid == "skip" else [],
    }


@router.get("")
async def list_records(
    request: Request,
    graph_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    include: str | None = None,
    sourceType: str | None = None,
    product: str | None = None,
    sentiment: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    _ensure_graph(request.app.state.settings.db_path, graph_id)
    return list_records_for_graph(
        request.app.state.settings.db_path,
        graph_id,
        limit=limit,
        offset=offset,
        include_normalized=include_normalized(include),
        filters={
            "sourceType": sourceType,
            "product": product,
            "sentiment": sentiment,
            "start": start,
            "end": end,
        },
    )


@router.get("/{record_id}")
async def get_record(request: Request, graph_id: str, record_id: str) -> dict[str, Any]:
    _ensure_graph(request.app.state.settings.db_path, graph_id)
    with connect(request.app.state.settings.db_path) as conn:
        row = fetch_one(
            conn,
            """
            SELECT *
              FROM records
             WHERE graph_id = ? AND (id = ? OR record_key = ?)
            """,
            (graph_id, record_id, record_id),
        )
        provenance = (
            external_provenance_by_record(conn, [row["id"]]).get(row["id"])
            if row is not None
            else None
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    return record_response(row, provenance=provenance)


def list_records_for_graph(
    db_path: str,
    graph_id: str,
    *,
    limit: int,
    offset: int,
    include_normalized: bool = False,
    filters: dict[str, str | None] | None = None,
    extra_where: str | None = None,
    extra_params: list[object] | None = None,
) -> dict[str, Any]:
    where = ["r.graph_id = ?", "r.is_active = 1"]
    params: list[object] = [graph_id]
    if extra_where:
        where.append(f"({extra_where})")
        params.extend(extra_params or [])
    _add_record_filters(where, params, filters or {})
    where_sql = " AND ".join(where)
    with connect(db_path) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM records r WHERE {where_sql}",
            tuple(params),
        ).fetchone()[0]
        rows = fetch_all(
            conn,
            f"""
            SELECT *
              FROM records r
             WHERE {where_sql}
             ORDER BY r.timestamp_ms ASC, r.id ASC
             LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        provenance = external_provenance_by_record(conn, [row["id"] for row in rows])
    return {
        "records": [
            record_response(
                row,
                include_normalized=include_normalized,
                provenance=provenance.get(row["id"]),
            )
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def record_response(
    row: dict,
    *,
    include_normalized: bool = True,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "id": row["id"],
        "graphId": row["graph_id"],
        "recordId": row["record_key"],
        "sourceType": row["source_type"],
        "sourceName": row["source_name"],
        "sourceRecordId": row["source_record_id"],
        "title": row["title"],
        "customerText": row["customer_text"],
        "recordUrl": row["record_url"],
        "product": row["product"],
        "sku": row["sku"],
        "rating": row["rating"],
        "sentiment": row["sentiment"],
        "tags": json.loads(row["tags_json"]) if row["tags_json"] else None,
        "timestamp": row["timestamp_utc"],
        "timestampMs": row["timestamp_ms"],
        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "active": bool(row.get("is_active", 1)),
    }
    if provenance is not None:
        payload["provenance"] = provenance
    if include_normalized:
        payload["normalized"] = json.loads(row["normalized_json"])
    return payload


def include_normalized(value: str | None) -> bool:
    return value == "normalized"


def _upsert_valid_records(
    db_path: str,
    graph_id: str,
    records: list[ValidatedRecord],
) -> tuple[int, int]:
    created = 0
    updated = 0
    now = now_iso()
    with connect(db_path) as conn:
        for record in records:
            existing = fetch_one(
                conn,
                "SELECT id FROM records WHERE graph_id = ? AND record_key = ?",
                (graph_id, record.record_key),
            )
            values = _record_values(record)
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO records (
                      id, graph_id, record_key, source_type, source_name, source_record_id,
                      title, customer_text, record_url, product, sku, rating, sentiment,
                      tags_json, timestamp_utc, timestamp_ms, metadata_json, normalized_json,
                      created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (new_id("rec"), graph_id, *values, now, now),
                )
                created += 1
            else:
                conn.execute(
                    """
                    UPDATE records
                       SET source_type = ?,
                           source_name = ?,
                           source_record_id = ?,
                           title = ?,
                           customer_text = ?,
                           record_url = ?,
                           product = ?,
                           sku = ?,
                           rating = ?,
                           sentiment = ?,
                           tags_json = ?,
                           timestamp_utc = ?,
                           timestamp_ms = ?,
                           metadata_json = ?,
                           normalized_json = ?,
                           updated_at = ?,
                           is_active = 1
                     WHERE graph_id = ? AND record_key = ?
                    """,
                    (*values[1:], now, graph_id, record.record_key),
                )
                updated += 1
        conn.commit()
    return created, updated


def _record_values(record: ValidatedRecord) -> tuple[object, ...]:
    return (
        record.record_key,
        record.source_type,
        record.source_name,
        record.source_record_id,
        record.title,
        record.customer_text,
        record.record_url,
        record.product,
        record.sku,
        record.rating,
        record.sentiment,
        json.dumps(record.tags, sort_keys=True) if record.tags is not None else None,
        record.timestamp_utc,
        record.timestamp_ms,
        json.dumps(record.metadata, sort_keys=True) if record.metadata is not None else None,
        json.dumps(record.normalized, sort_keys=True),
    )


def _add_record_filters(
    where: list[str],
    params: list[object],
    filters: dict[str, str | None],
) -> None:
    field_map = {
        "sourceType": "r.source_type",
        "product": "r.product",
        "sentiment": "r.sentiment",
    }
    for key, column in field_map.items():
        value = filters.get(key)
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    for key, op in (("start", ">="), ("end", "<=")):
        value = filters.get(key)
        if value is None:
            continue
        try:
            _, timestamp_ms = parse_timestamp(value, field=key)
        except TimestampValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=[{"field": key, "message": str(exc)}],
            ) from exc
        where.append(f"r.timestamp_ms {op} ?")
        params.append(timestamp_ms)


def _ensure_graph(db_path: str, graph_id: str) -> None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM graphs WHERE id = ?", (graph_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")


def _reject_unknown_body(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"field": key, "message": "unknown request key"} for key in unknown],
        )
