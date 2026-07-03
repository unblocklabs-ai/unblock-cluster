from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from datagraph.core.time import TimestampValidationError, parse_timestamp

REQUIRED_FIELDS = {
    "recordId",
    "sourceType",
    "sourceName",
    "sourceRecordId",
    "customerText",
    "timestamp",
}
OPTIONAL_FIELDS = {
    "title",
    "recordUrl",
    "product",
    "sku",
    "rating",
    "sentiment",
    "tags",
    "metadata",
}
ALLOWED_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS


@dataclass(frozen=True)
class ValidatedRecord:
    record_key: str
    source_type: str
    source_name: str
    source_record_id: str
    title: str | None
    customer_text: str
    record_url: str | None
    product: str | None
    sku: str | None
    rating: float | None
    sentiment: str | None
    tags: list[str] | None
    timestamp_utc: str
    timestamp_ms: int
    metadata: dict[str, Any] | None
    normalized: dict[str, Any]


def validate_record(value: Any, *, index: int) -> tuple[ValidatedRecord | None, dict | None]:
    errors: list[dict[str, str]] = []
    if not isinstance(value, dict):
        return None, {
            "index": index,
            "recordId": None,
            "errors": [{"field": "", "message": "must be an object"}],
        }

    record_id = value.get("recordId")
    parseable_record_id = record_id if isinstance(record_id, str) and record_id.strip() else None

    unknown = sorted(set(value) - ALLOWED_FIELDS)
    for key in unknown:
        errors.append(
            {"field": key, "message": "unknown record key; use metadata for custom fields"}
        )

    record_key = _required_string(value, "recordId", errors)
    source_type = _required_string(value, "sourceType", errors)
    source_name = _required_string(value, "sourceName", errors)
    source_record_id = _required_string(value, "sourceRecordId", errors)
    customer_text = _required_string(value, "customerText", errors)
    if customer_text is not None and not customer_text.strip():
        errors.append({"field": "customerText", "message": "must be non-empty after trimming"})

    timestamp_utc = ""
    timestamp_ms = 0
    if "timestamp" not in value or value.get("timestamp") is None:
        errors.append({"field": "timestamp", "message": "is required"})
    else:
        try:
            timestamp_utc, timestamp_ms = parse_timestamp(value["timestamp"])
        except TimestampValidationError as exc:
            errors.append({"field": "timestamp", "message": str(exc)})

    title = _optional_string(value, "title", errors)
    record_url = _optional_string(value, "recordUrl", errors)
    product = _optional_string(value, "product", errors)
    sku = _optional_string(value, "sku", errors)
    sentiment = _optional_string(value, "sentiment", errors)
    rating = _optional_number(value, "rating", errors)
    tags = _optional_tags(value, errors)
    metadata = _optional_metadata(value, errors)

    if errors:
        return None, {"index": index, "recordId": parseable_record_id, "errors": errors}

    return (
        ValidatedRecord(
            record_key=record_key or "",
            source_type=source_type or "",
            source_name=source_name or "",
            source_record_id=source_record_id or "",
            title=title,
            customer_text=(customer_text or "").strip(),
            record_url=record_url,
            product=product,
            sku=sku,
            rating=rating,
            sentiment=sentiment,
            tags=tags,
            timestamp_utc=timestamp_utc,
            timestamp_ms=timestamp_ms,
            metadata=metadata,
            normalized=dict(value),
        ),
        None,
    )


def _required_string(value: dict[str, Any], field: str, errors: list[dict[str, str]]) -> str | None:
    if field not in value or value.get(field) is None:
        errors.append({"field": field, "message": "is required"})
        return None
    item = value[field]
    if not isinstance(item, str):
        errors.append({"field": field, "message": "must be a string"})
        return None
    if not item.strip():
        errors.append({"field": field, "message": "must be non-empty"})
        return None
    return item


def _optional_string(value: dict[str, Any], field: str, errors: list[dict[str, str]]) -> str | None:
    if field not in value or value[field] is None:
        return None
    item = value[field]
    if not isinstance(item, str):
        errors.append({"field": field, "message": "must be a string when provided"})
        return None
    return item


def _optional_number(
    value: dict[str, Any],
    field: str,
    errors: list[dict[str, str]],
) -> float | None:
    if field not in value or value[field] is None:
        return None
    item = value[field]
    if isinstance(item, bool) or not isinstance(item, int | float):
        errors.append({"field": field, "message": "must be a number when provided"})
        return None
    return float(item)


def _optional_tags(value: dict[str, Any], errors: list[dict[str, str]]) -> list[str] | None:
    if "tags" not in value or value["tags"] is None:
        return None
    tags = value["tags"]
    if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
        errors.append({"field": "tags", "message": "must be a list of strings when provided"})
        return None
    return tags


def _optional_metadata(
    value: dict[str, Any],
    errors: list[dict[str, str]],
) -> dict[str, Any] | None:
    if "metadata" not in value or value["metadata"] is None:
        return None
    metadata = value["metadata"]
    if not isinstance(metadata, dict):
        errors.append({"field": "metadata", "message": "must be an object when provided"})
        return None
    return metadata
