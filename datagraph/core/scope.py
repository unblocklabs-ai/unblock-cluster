from __future__ import annotations

import json
from typing import Any

from datagraph.core.time import TimestampValidationError, parse_timestamp

SCOPE_KEYS = {
    "sourceTypes",
    "sourceNames",
    "products",
    "skus",
    "sentiments",
    "ratings",
    "timeRange",
    "tagsAny",
    "metadataEquals",
}


class ScopeValidationError(ValueError):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("invalid scope")


def compile_scope(scope: Any, *, alias: str = "r") -> tuple[str, list[object]]:
    if scope is None:
        scope = {}
    if not isinstance(scope, dict):
        raise ScopeValidationError([{"field": "scope", "message": "must be an object"}])

    errors: list[dict[str, str]] = []
    unknown = sorted(set(scope) - SCOPE_KEYS)
    for key in unknown:
        errors.append({"field": f"scope.{key}", "message": "unknown scope key"})

    clauses: list[str] = [f"{alias}.is_active = 1"]
    params: list[object] = []

    _add_in_clause(scope, "sourceTypes", f"{alias}.source_type", clauses, params, errors)
    _add_in_clause(scope, "sourceNames", f"{alias}.source_name", clauses, params, errors)
    _add_in_clause(scope, "products", f"{alias}.product", clauses, params, errors)
    _add_in_clause(scope, "skus", f"{alias}.sku", clauses, params, errors)
    _add_in_clause(scope, "sentiments", f"{alias}.sentiment", clauses, params, errors)
    _add_ratings(scope, alias, clauses, params, errors)
    _add_time_range(scope, alias, clauses, params, errors)
    _add_tags_any(scope, alias, clauses, params, errors)
    _add_metadata_equals(scope, alias, clauses, params, errors)

    if errors:
        raise ScopeValidationError(errors)
    return (" AND ".join(clauses) if clauses else "1 = 1", params)


def validate_scope(scope: Any) -> dict[str, Any]:
    compile_scope(scope)
    return {} if scope is None else scope


def _add_in_clause(
    scope: dict[str, Any],
    key: str,
    column: str,
    clauses: list[str],
    params: list[object],
    errors: list[dict[str, str]],
) -> None:
    if key not in scope:
        return
    values = scope[key]
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        errors.append({"field": f"scope.{key}", "message": "must be a list of strings"})
        return
    if not values:
        clauses.append("0 = 1")
        return
    placeholders = ", ".join("?" for _ in values)
    clauses.append(f"{column} IN ({placeholders})")
    params.extend(values)


def _add_ratings(
    scope: dict[str, Any],
    alias: str,
    clauses: list[str],
    params: list[object],
    errors: list[dict[str, str]],
) -> None:
    if "ratings" not in scope:
        return
    ratings = scope["ratings"]
    if not isinstance(ratings, dict):
        errors.append({"field": "scope.ratings", "message": "must be an object"})
        return
    unknown = sorted(set(ratings) - {"min", "max"})
    for key in unknown:
        errors.append({"field": f"scope.ratings.{key}", "message": "unknown ratings key"})
    for key, op in (("min", ">="), ("max", "<=")):
        if key not in ratings or ratings[key] is None:
            continue
        value = ratings[key]
        if isinstance(value, bool) or not isinstance(value, int | float):
            errors.append({"field": f"scope.ratings.{key}", "message": "must be a number"})
            continue
        clauses.append(f"{alias}.rating {op} ?")
        params.append(float(value))


def _add_time_range(
    scope: dict[str, Any],
    alias: str,
    clauses: list[str],
    params: list[object],
    errors: list[dict[str, str]],
) -> None:
    if "timeRange" not in scope:
        return
    time_range = scope["timeRange"]
    if not isinstance(time_range, dict):
        errors.append({"field": "scope.timeRange", "message": "must be an object"})
        return
    unknown = sorted(set(time_range) - {"start", "end"})
    for key in unknown:
        errors.append({"field": f"scope.timeRange.{key}", "message": "unknown timeRange key"})
    for key, op in (("start", ">="), ("end", "<=")):
        if key not in time_range or time_range[key] is None:
            continue
        try:
            _, timestamp_ms = parse_timestamp(time_range[key], field=f"scope.timeRange.{key}")
        except TimestampValidationError as exc:
            errors.append({"field": f"scope.timeRange.{key}", "message": str(exc)})
            continue
        clauses.append(f"{alias}.timestamp_ms {op} ?")
        params.append(timestamp_ms)


def _add_tags_any(
    scope: dict[str, Any],
    alias: str,
    clauses: list[str],
    params: list[object],
    errors: list[dict[str, str]],
) -> None:
    if "tagsAny" not in scope:
        return
    tags = scope["tagsAny"]
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        errors.append({"field": "scope.tagsAny", "message": "must be a list of strings"})
        return
    if not tags:
        clauses.append("0 = 1")
        return
    placeholders = ", ".join("?" for _ in tags)
    clauses.append(
        f"""
        EXISTS (
          SELECT 1 FROM json_each(COALESCE({alias}.tags_json, '[]'))
           WHERE json_each.value IN ({placeholders})
        )
        """
    )
    params.extend(tags)


def _add_metadata_equals(
    scope: dict[str, Any],
    alias: str,
    clauses: list[str],
    params: list[object],
    errors: list[dict[str, str]],
) -> None:
    if "metadataEquals" not in scope:
        return
    metadata_equals = scope["metadataEquals"]
    if not isinstance(metadata_equals, dict):
        errors.append({"field": "scope.metadataEquals", "message": "must be an object"})
        return
    for key, value in metadata_equals.items():
        if not isinstance(key, str) or not key:
            errors.append(
                {"field": "scope.metadataEquals", "message": "keys must be non-empty strings"}
            )
            continue
        clauses.append(f"json_extract({alias}.metadata_json, ?) = ?")
        params.append(f"$.{key}")
        params.append(_metadata_sql_value(value))


def _metadata_sql_value(value: Any) -> object:
    if isinstance(value, bool):
        return 1 if value else 0
    if value is None or isinstance(value, int | float | str):
        return value
    return json.dumps(value, sort_keys=True)
