from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_GRAPH_CONFIG: dict[str, Any] = {
    "embedding": {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "dimensions": None,
        "textFields": None,
        "textTemplate": None,
        "requestsPerMinute": 500,
        "maxConcurrency": 4,
        "maxInputTokens": 8000,
    },
    "cluster": {
        "space": {
            "method": "umap",
            "nComponents": 25,
            "nNeighbors": 15,
            "metric": "cosine",
            "minDist": 0.1,
        },
        "hdbscan": {
            "minClusterSize": None,
            "minSamples": None,
            "clusterSelectionMethod": "eom",
            "clusterSelectionEpsilon": 0.0,
            "allowSingleCluster": False,
        },
        "seed": 42,
    },
    "layout": {"method": "umap", "nNeighbors": 30, "minDist": 0.1},
    "labeling": {"provider": "openai", "model": "gpt-5.4-mini", "topK": 12, "prompt": None},
    "time": {"timestampField": "timestamp", "bucket": "week"},
}


class ConfigValidationError(ValueError):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("invalid graph config")


def validate_graph_config(config: Any, *, require_text_fields: bool = True) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    if not isinstance(config, dict):
        raise ConfigValidationError([{"field": "config", "message": "must be an object"}])

    normalized = _merge_section(DEFAULT_GRAPH_CONFIG, config, "config", errors)
    text_fields = normalized["embedding"].get("textFields")
    if require_text_fields and not _is_non_empty_string_list(text_fields):
        errors.append(
            {
                "field": "config.embedding.textFields",
                "message": "is required and must be a non-empty list of strings",
            }
        )

    method = normalized["cluster"]["space"].get("method")
    if method not in {"umap", "none"}:
        errors.append(
            {
                "field": "config.cluster.space.method",
                "message": 'must be "umap" or "none"',
            }
        )
    min_dist = normalized["cluster"]["space"].get("minDist")
    if (
        not isinstance(min_dist, int | float)
        or isinstance(min_dist, bool)
        or float(min_dist) < 0
    ):
        errors.append(
            {
                "field": "config.cluster.space.minDist",
                "message": "must be a number greater than or equal to 0",
            }
        )

    layout_method = normalized["layout"].get("method")
    if layout_method != "umap":
        errors.append(
            {
                "field": "config.layout.method",
                "message": 'must be "umap"',
            }
        )

    provider = normalized["embedding"].get("provider")
    if provider not in {"openai", "mock"}:
        errors.append(
            {
                "field": "config.embedding.provider",
                "message": 'must be "openai" or "mock"',
            }
        )

    labeling = normalized["labeling"]
    labeling_provider = labeling.get("provider")
    if labeling_provider != "openai":
        errors.append(
            {
                "field": "config.labeling.provider",
                "message": 'must be "openai"',
            }
        )
    if not isinstance(labeling.get("model"), str) or not labeling["model"].strip():
        errors.append(
            {"field": "config.labeling.model", "message": "must be a non-empty string"}
        )
    top_k = labeling.get("topK")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 20:
        errors.append(
            {"field": "config.labeling.topK", "message": "must be an integer from 1 to 20"}
        )
    prompt = labeling.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        errors.append(
            {"field": "config.labeling.prompt", "message": "must be a string or null"}
        )

    time_config = normalized["time"]
    timestamp_field = time_config.get("timestampField")
    if timestamp_field != "timestamp":
        errors.append(
            {"field": "config.time.timestampField", "message": 'must be "timestamp"'}
        )
    bucket = time_config.get("bucket")
    if bucket not in {"day", "week", "month"}:
        errors.append(
            {"field": "config.time.bucket", "message": 'must be "day", "week", or "month"'}
        )

    if errors:
        raise ConfigValidationError(errors)
    return normalized


def apply_embedding_overrides(
    current: dict[str, Any],
    overrides: Any,
) -> dict[str, Any]:
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ConfigValidationError([{"field": "embedding", "message": "must be an object"}])
    unknown = sorted(set(overrides) - set(DEFAULT_GRAPH_CONFIG["embedding"]))
    if unknown:
        raise ConfigValidationError(
            [
                {"field": f"embedding.{key}", "message": "unknown embedding config key"}
                for key in unknown
            ]
        )
    merged = deepcopy(current)
    merged["embedding"] = {**merged["embedding"], **overrides}
    return validate_graph_config(merged, require_text_fields=True)


def apply_cluster_overrides(
    current: dict[str, Any],
    overrides: Any,
) -> dict[str, Any]:
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ConfigValidationError([{"field": "cluster", "message": "must be an object"}])
    unknown = sorted(set(overrides) - set(DEFAULT_GRAPH_CONFIG["cluster"]))
    if unknown:
        raise ConfigValidationError(
            [
                {"field": f"cluster.{key}", "message": "unknown cluster config key"}
                for key in unknown
            ]
        )
    merged = deepcopy(current)
    errors: list[dict[str, str]] = []
    merged["cluster"] = _merge_section(
        DEFAULT_GRAPH_CONFIG["cluster"],
        _deep_merge(merged["cluster"], overrides),
        "cluster",
        errors,
    )
    if errors:
        raise ConfigValidationError(errors)
    return validate_graph_config(merged, require_text_fields=True)


def apply_layout_overrides(
    current: dict[str, Any],
    overrides: Any,
) -> dict[str, Any]:
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ConfigValidationError([{"field": "layout", "message": "must be an object"}])
    unknown = sorted(set(overrides) - set(DEFAULT_GRAPH_CONFIG["layout"]))
    if unknown:
        raise ConfigValidationError(
            [{"field": f"layout.{key}", "message": "unknown layout config key"} for key in unknown]
        )
    merged = deepcopy(current)
    merged["layout"] = {**merged["layout"], **overrides}
    return validate_graph_config(merged, require_text_fields=True)


def apply_labeling_overrides(
    current: dict[str, Any],
    overrides: Any,
) -> dict[str, Any]:
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ConfigValidationError([{"field": "labeling", "message": "must be an object"}])
    unknown = sorted(set(overrides) - set(DEFAULT_GRAPH_CONFIG["labeling"]))
    if unknown:
        raise ConfigValidationError(
            [
                {"field": f"labeling.{key}", "message": "unknown labeling config key"}
                for key in unknown
            ]
        )
    merged = deepcopy(current)
    merged["labeling"] = {**merged["labeling"], **overrides}
    return validate_graph_config(merged, require_text_fields=True)


def apply_time_overrides(
    current: dict[str, Any],
    overrides: Any,
) -> dict[str, Any]:
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ConfigValidationError([{"field": "time", "message": "must be an object"}])
    unknown = sorted(set(overrides) - set(DEFAULT_GRAPH_CONFIG["time"]))
    if unknown:
        raise ConfigValidationError(
            [{"field": f"time.{key}", "message": "unknown time config key"} for key in unknown]
        )
    merged = deepcopy(current)
    merged["time"] = {**merged["time"], **overrides}
    return validate_graph_config(merged, require_text_fields=True)


def _deep_merge(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def apply_config_patch(current: dict[str, Any], patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise ConfigValidationError([{"field": "config", "message": "must be an object"}])
    unknown = sorted(set(patch) - set(DEFAULT_GRAPH_CONFIG))
    if unknown:
        raise ConfigValidationError(
            [{"field": f"config.{key}", "message": "unknown config key"} for key in unknown]
        )
    merged = deepcopy(current)
    for section, value in patch.items():
        merged[section] = value
    return validate_graph_config(merged, require_text_fields=True)


def _merge_section(default: Any, override: Any, field: str, errors: list[dict[str, str]]) -> Any:
    if isinstance(default, dict):
        if not isinstance(override, dict):
            errors.append({"field": field, "message": "must be an object"})
            return deepcopy(default)
        unknown = sorted(set(override) - set(default))
        for key in unknown:
            errors.append({"field": f"{field}.{key}", "message": "unknown config key"})
        merged: dict[str, Any] = {}
        for key, default_value in default.items():
            if key in override:
                merged[key] = _merge_section(default_value, override[key], f"{field}.{key}", errors)
            else:
                merged[key] = deepcopy(default_value)
        return merged
    return deepcopy(override)


def _is_non_empty_string_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) for item in value)
