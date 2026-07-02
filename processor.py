import hashlib
import json
import os
import sys
import urllib.error
import urllib.request


OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
DEFAULT_TEXT_FEATURE_METHOD = "tfidf"
DEFAULT_EMBEDDING_PROVIDER = "openai"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_BATCH_SIZE = 64
DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 30.0
TEXT_FEATURE_METHODS = {"tfidf", "embedding"}


class ProcessingError(Exception):
    pass


class EmbeddingProcessingError(ProcessingError):
    pass


def text_for_record(record, fields):
    parts = []
    for field in fields:
        value = record.get(field)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif isinstance(value, dict):
            parts.append(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
        elif value is not None:
            parts.append(str(value))
    return " ".join(parts)


def normalize_embedding_text(text):
    return " ".join(str(text or "").split())


def text_hash(text):
    return hashlib.sha256(normalize_embedding_text(text).encode("utf-8")).hexdigest()


def group_value_for_record(record, grouping_fields):
    if len(grouping_fields) == 1:
        value = record.get(grouping_fields[0])
        return str(value) if value not in (None, "") else "Unknown"
    return " / ".join(
        f"{field}: {record.get(field) if record.get(field) not in (None, '') else 'Unknown'}"
        for field in grouping_fields
    )


def most_common_label(records, field, fallback):
    counts = {}
    for record in records:
        value = record.get(field)
        if value not in (None, ""):
            text = str(value)
            counts[text] = counts.get(text, 0) + 1
    if not counts:
        return fallback
    return max(counts.items(), key=lambda item: item[1])[0]


def cluster_label(records, label, grouping_fields, cluster_config=None):
    cluster_config = cluster_config or {}
    overrides = cluster_config.get("labelOverrides") or {}

    def apply_override(label_name):
        return str(overrides.get(label_name, label_name))

    if str(label) in overrides:
        return str(overrides[str(label)])
    if label == -1:
        return "Outliers"
    strategy = cluster_config.get("labelStrategy", "groupingField")
    if strategy == "clusterId":
        return f"Cluster {label}"
    if strategy == "labelField":
        label_field = cluster_config.get("labelField")
        if label_field:
            return apply_override(
                most_common_label(records, label_field, f"Cluster {label}")
            )
    counts = {}
    for record in records:
        value = group_value_for_record(record, grouping_fields)
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return f"Cluster {label}"
    label_name = max(counts.items(), key=lambda item: item[1])[0]
    return apply_override(label_name)


def default_feature_fields(config):
    schema = config["dataSchema"]
    return [
        field
        for field, field_type in schema.items()
        if field_type in {"String", "Boolean", "Object", "Array"}
    ]


def default_numeric_fields(config):
    return [
        field
        for field, field_type in config["dataSchema"].items()
        if field_type == "Number"
    ]


def config_or_env(cluster_config, config_keys, env_keys, default=None):
    if isinstance(config_keys, str):
        config_keys = (config_keys,)
    for key in config_keys:
        value = cluster_config.get(key)
        if value not in (None, ""):
            return value
    for key in env_keys:
        value = os.environ.get(key)
        if value not in (None, ""):
            return value
    return default


def parse_positive_int(value, name, default=None):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ProcessingError(f"{name} must be a positive integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ProcessingError(f"{name} must be a positive integer.") from error
    if parsed <= 0:
        raise ProcessingError(f"{name} must be a positive integer.")
    return parsed


def parse_positive_float(value, name, default=None):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ProcessingError(f"{name} must be a positive number.")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ProcessingError(f"{name} must be a positive number.") from error
    if parsed <= 0:
        raise ProcessingError(f"{name} must be a positive number.")
    return parsed


def normalize_text_feature_method(value):
    method = str(value or DEFAULT_TEXT_FEATURE_METHOD).strip().lower()
    aliases = {
        "tf-idf": "tfidf",
        "tf_idf": "tfidf",
        "openai": "embedding",
        "embeddings": "embedding",
    }
    method = aliases.get(method, method)
    if method not in TEXT_FEATURE_METHODS:
        raise ProcessingError(
            "textFeatureMethod must be one of: "
            + ", ".join(sorted(TEXT_FEATURE_METHODS))
            + "."
        )
    return method


def resolve_processor_options(config):
    cluster_config = config.get("cluster") or {}
    method = normalize_text_feature_method(
        config_or_env(
            cluster_config,
            "textFeatureMethod",
            ("DATA_GRAPH_TEXT_FEATURE_METHOD",),
            DEFAULT_TEXT_FEATURE_METHOD,
        )
    )
    provider = config_or_env(
        cluster_config,
        "embeddingProvider",
        ("DATA_GRAPH_EMBEDDING_PROVIDER",),
        DEFAULT_EMBEDDING_PROVIDER,
    )
    provider = str(provider).strip().lower()
    if provider != DEFAULT_EMBEDDING_PROVIDER:
        raise ProcessingError("embeddingProvider must be 'openai'.")

    model = config_or_env(
        cluster_config,
        "embeddingModel",
        ("DATA_GRAPH_EMBEDDING_MODEL",),
        DEFAULT_EMBEDDING_MODEL,
    )
    model = str(model).strip()
    if not model:
        raise ProcessingError("embeddingModel must be a non-empty string.")

    return {
        "textFeatureMethod": method,
        "embeddingProvider": provider,
        "embeddingModel": model,
        "embeddingDimensions": parse_positive_int(
            config_or_env(
                cluster_config,
                "embeddingDimensions",
                ("DATA_GRAPH_EMBEDDING_DIMENSIONS",),
            ),
            "embeddingDimensions",
        ),
        "embeddingBatchSize": parse_positive_int(
            os.environ.get("DATA_GRAPH_EMBEDDING_BATCH_SIZE"),
            "DATA_GRAPH_EMBEDDING_BATCH_SIZE",
            DEFAULT_EMBEDDING_BATCH_SIZE,
        ),
        "embeddingTimeoutSeconds": parse_positive_float(
            os.environ.get("DATA_GRAPH_EMBEDDING_TIMEOUT_SECONDS"),
            "DATA_GRAPH_EMBEDDING_TIMEOUT_SECONDS",
            DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
        ),
    }


def resolve_processor_config(config):
    resolved = resolve_processor_options(config)
    return {
        **resolved,
        "embeddingConfigured": bool(os.environ.get("OPENAI_API_KEY")),
    }


def update_metadata(metadata, values):
    if metadata is not None:
        metadata.update(values)


def metadata_defaults(rows, options, numeric_fields, feature_fields, cluster_config=None):
    cluster_config = cluster_config or {}
    metadata = {
        "recordCount": len(rows),
        "layoutMethod": "PaCMAP",
        "clusterMethod": "HDBSCAN",
        "fallbackUsed": False,
        "numericFields": numeric_fields,
        "featureFields": feature_fields,
        "textFeatureMethod": options["textFeatureMethod"],
        "clusterLabelStrategy": cluster_config.get("labelStrategy", "groupingField"),
    }
    if cluster_config.get("labelField"):
        metadata["clusterLabelField"] = cluster_config["labelField"]
    if cluster_config.get("labelOverrides"):
        metadata["clusterLabelOverrideCount"] = len(cluster_config["labelOverrides"])
    if options["textFeatureMethod"] == "embedding":
        metadata.update(
            {
                "embeddingProvider": options["embeddingProvider"],
                "embeddingModel": options["embeddingModel"],
                "embeddingDimensions": options["embeddingDimensions"],
                "embeddingBatchSize": options["embeddingBatchSize"],
                "embeddingTimeoutSeconds": options["embeddingTimeoutSeconds"],
                "embeddingCacheHits": 0,
                "embeddingCacheMisses": 0,
                "embeddingRequestCount": 0,
                "embeddingCacheKeyVersion": "provider:model:dimensions:text_sha256",
            }
        )
    return metadata


def ensure_embedding_configured(options):
    if options["textFeatureMethod"] == "embedding" and not os.environ.get("OPENAI_API_KEY"):
        raise EmbeddingProcessingError(
            "OPENAI_API_KEY is required when textFeatureMethod is 'embedding'."
        )


def normalize_points(points):
    import numpy as np
    from sklearn.preprocessing import StandardScaler

    if len(points) <= 1:
        return np.asarray(points, dtype=float)
    scaled = StandardScaler().fit_transform(points)
    return scaled * 1000


def numeric_feature_matrix(rows, numeric_fields):
    if not numeric_fields:
        return None

    import numpy as np
    from sklearn.preprocessing import StandardScaler

    numeric = np.array(
        [[float(record.get(field) or 0) for field in numeric_fields] for record in rows],
        dtype=float,
    )
    return StandardScaler().fit_transform(numeric)


def tfidf_text_feature_matrix(rows, feature_fields):
    if not feature_fields:
        return None

    import numpy as np
    from sklearn.feature_extraction.text import TfidfVectorizer

    corpus = [text_for_record(record, feature_fields) for record in rows]
    if not any(text.strip() for text in corpus):
        return None
    text_matrix = TfidfVectorizer(max_features=512, stop_words="english").fit_transform(corpus)
    return np.asarray(text_matrix.toarray(), dtype=float)


def validate_embedding_vector(value, source):
    if not isinstance(value, list) or not value:
        raise EmbeddingProcessingError(f"{source} returned an invalid embedding vector.")
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError) as error:
        raise EmbeddingProcessingError(f"{source} returned a non-numeric embedding vector.") from error


def openai_error_message(status, body):
    details = []
    if body:
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
            upstream_error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(upstream_error, dict):
                for field in ("type", "code"):
                    value = upstream_error.get(field)
                    if value not in (None, ""):
                        details.append(f"{field}={str(value)[:120]}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    suffix = f" ({'; '.join(details)})" if details else ""
    return f"OpenAI embeddings request failed with status {status}{suffix}."


def request_openai_embeddings(inputs, options):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EmbeddingProcessingError(
            "OPENAI_API_KEY is required when textFeatureMethod is 'embedding'."
        )

    payload = {
        "model": options["embeddingModel"],
        "input": inputs,
        "encoding_format": "float",
    }
    if options["embeddingDimensions"] is not None:
        payload["dimensions"] = options["embeddingDimensions"]

    request = urllib.request.Request(
        OPENAI_EMBEDDINGS_URL,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=options["embeddingTimeoutSeconds"],
        ) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read(8192)
        raise EmbeddingProcessingError(openai_error_message(error.code, body)) from error
    except TimeoutError as error:
        raise EmbeddingProcessingError("OpenAI embeddings request timed out.") from error
    except urllib.error.URLError as error:
        raise EmbeddingProcessingError("OpenAI embeddings request failed.") from error
    except json.JSONDecodeError as error:
        raise EmbeddingProcessingError("OpenAI embeddings response was not valid JSON.") from error

    data = response_payload.get("data") if isinstance(response_payload, dict) else None
    if not isinstance(data, list) or len(data) != len(inputs):
        raise EmbeddingProcessingError("OpenAI embeddings response had an unexpected shape.")

    ordered = [None] * len(inputs)
    for item in data:
        if not isinstance(item, dict):
            raise EmbeddingProcessingError("OpenAI embeddings response had an unexpected item.")
        index = item.get("index")
        if not isinstance(index, int) or index < 0 or index >= len(inputs):
            raise EmbeddingProcessingError("OpenAI embeddings response had an invalid index.")
        ordered[index] = validate_embedding_vector(item.get("embedding"), "OpenAI")
    if any(embedding is None for embedding in ordered):
        raise EmbeddingProcessingError("OpenAI embeddings response was missing an item.")
    return ordered


def batches(values, size):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def embedding_text_feature_matrix(
    rows,
    feature_fields,
    options,
    embedding_cache=None,
    embed_texts=request_openai_embeddings,
    metadata=None,
):
    if not feature_fields:
        return None

    import numpy as np

    corpus = [normalize_embedding_text(text_for_record(record, feature_fields)) for record in rows]
    nonblank_indexes = [index for index, text in enumerate(corpus) if text]
    if not nonblank_indexes:
        return None

    provider = options["embeddingProvider"]
    model = options["embeddingModel"]
    dimensions = options["embeddingDimensions"]
    text_by_hash = {}
    indexes_by_hash = {}
    for index in nonblank_indexes:
        digest = text_hash(corpus[index])
        text_by_hash.setdefault(digest, corpus[index])
        indexes_by_hash.setdefault(digest, []).append(index)

    cached = {}
    if embedding_cache is not None:
        try:
            cached = embedding_cache.get_many(
                provider,
                model,
                dimensions,
                list(text_by_hash.keys()),
            )
        except Exception as error:
            raise EmbeddingProcessingError("Embedding cache lookup failed.") from error
        cached = {
            digest: validate_embedding_vector(embedding, "Embedding cache")
            for digest, embedding in cached.items()
        }

    missing_hashes = [digest for digest in text_by_hash if digest not in cached]
    fetched = {}
    request_count = 0
    for hash_batch in batches(missing_hashes, options["embeddingBatchSize"]):
        text_batch = [text_by_hash[digest] for digest in hash_batch]
        embeddings = embed_texts(text_batch, options)
        if not isinstance(embeddings, list) or len(embeddings) != len(hash_batch):
            raise EmbeddingProcessingError(
                "Embedding provider returned an unexpected number of vectors."
            )
        request_count += 1
        fetched.update(
            {
                digest: validate_embedding_vector(embedding, "OpenAI")
                for digest, embedding in zip(hash_batch, embeddings)
            }
        )

    if embedding_cache is not None and fetched:
        try:
            embedding_cache.set_many(provider, model, dimensions, fetched)
        except Exception as error:
            raise EmbeddingProcessingError("Embedding cache write failed.") from error

    embeddings_by_hash = {**cached, **fetched}
    first_embedding = next(iter(embeddings_by_hash.values()), None)
    if first_embedding is None:
        return None
    width = len(first_embedding)
    embeddings = [None] * len(corpus)
    for digest, indexes in indexes_by_hash.items():
        embedding = embeddings_by_hash[digest]
        if len(embedding) != width:
            raise EmbeddingProcessingError("Embedding vectors must all have the same dimensions.")
        for index in indexes:
            embeddings[index] = embedding
    for index, embedding in enumerate(embeddings):
        if embedding is None:
            embeddings[index] = [0.0] * width

    update_metadata(
        metadata,
        {
            "embeddingCacheHits": len(cached),
            "embeddingCacheMisses": len(missing_hashes),
            "embeddingRequestCount": request_count,
            "embeddingVectorDimensions": width,
        },
    )
    return np.asarray(embeddings, dtype=float)


def build_feature_parts(
    rows,
    numeric_fields,
    feature_fields,
    options,
    embedding_cache=None,
    embed_texts=request_openai_embeddings,
    metadata=None,
):
    feature_parts = []

    numeric = numeric_feature_matrix(rows, numeric_fields)
    if numeric is not None:
        feature_parts.append(numeric)

    if options["textFeatureMethod"] == "embedding":
        text_features = embedding_text_feature_matrix(
            rows,
            feature_fields,
            options,
            embedding_cache=embedding_cache,
            embed_texts=embed_texts,
            metadata=metadata,
        )
    else:
        text_features = tfidf_text_feature_matrix(rows, feature_fields)

    if text_features is not None:
        feature_parts.append(text_features)

    return feature_parts


def reduce_to_points(features):
    import pacmap

    reducer = pacmap.PaCMAP(n_components=2, MN_ratio=0.5, FP_ratio=2.0, random_state=42)
    return normalize_points(reducer.fit_transform(features))


def cluster_points(points, row_count, cluster_config):
    import hdbscan

    min_cluster_size = int(
        cluster_config.get("minClusterSize", max(3, min(12, row_count // 6 or 3)))
    )
    min_cluster_size = max(2, min(min_cluster_size, row_count))
    return hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, min_samples=2).fit_predict(points)


def attach_layout_and_labels(rows, points, labels, grouping_fields, cluster_config=None):
    grouped_records = {}
    for record, label in zip(rows, labels):
        grouped_records.setdefault(int(label), []).append(record)
    label_names = {
        label: cluster_label(cluster_records, label, grouping_fields, cluster_config)
        for label, cluster_records in grouped_records.items()
    }

    processed = []
    for record, point, label in zip(rows, points, labels):
        label = int(label)
        processed.append(
            {
                **record,
                "x": float(point[0]),
                "y": float(point[1]),
                "clusterId": label,
                "clusterLabel": label_names[label],
                "groupValue": group_value_for_record(record, grouping_fields),
            }
        )
    return processed


def fallback_layout(records, grouping_fields, cluster_config=None):
    import math

    grouped = {}
    for record in records:
        grouped.setdefault(group_value_for_record(record, grouping_fields), []).append(record)
    groups = list(grouped.items())
    group_count = max(len(groups), 1)
    processed = []
    for group_index, (group_value, items) in enumerate(groups):
        label = cluster_label(items, group_index, grouping_fields, cluster_config)
        center_angle = (math.pi * 2 * group_index) / group_count
        center_radius = 0 if group_count == 1 else 1200
        center_x = math.cos(center_angle) * center_radius
        center_y = math.sin(center_angle) * center_radius
        for item_index, record in enumerate(items):
            item_angle = item_index * 2.399963229728653
            item_radius = 70 + (item_index**0.5) * 52
            processed.append(
                {
                    **record,
                    "x": center_x + math.cos(item_angle) * item_radius,
                    "y": center_y + math.sin(item_angle) * item_radius,
                    "clusterId": group_index,
                    "clusterLabel": label,
                    "groupValue": group_value,
                }
            )
    return processed


def process_records(
    sink,
    rows,
    *,
    embedding_cache=None,
    embed_texts=request_openai_embeddings,
    metadata=None,
):
    processor_metadata = metadata if metadata is not None else {}
    config = sink["config"]
    cluster_config = config.get("cluster") or {}
    grouping_fields = config["groupingFields"]
    options = resolve_processor_options(config)
    numeric_fields = cluster_config.get("numericFields", default_numeric_fields(config))
    feature_fields = cluster_config.get("featureFields", default_feature_fields(config))
    numeric_fields = [field for field in numeric_fields if field in config["dataSchema"]]
    feature_fields = [field for field in feature_fields if field in config["dataSchema"]]
    update_metadata(
        processor_metadata,
        metadata_defaults(rows, options, numeric_fields, feature_fields, cluster_config),
    )

    if not rows:
        return []
    ensure_embedding_configured(options)

    if len(rows) < 3:
        update_metadata(
            processor_metadata,
            {"fallbackUsed": True, "fallbackReason": "row_count_below_3"},
        )
        return fallback_layout(rows, grouping_fields, cluster_config)

    try:
        import numpy as np

        feature_parts = build_feature_parts(
            rows,
            numeric_fields,
            feature_fields,
            options,
            embedding_cache=embedding_cache,
            embed_texts=embed_texts,
            metadata=processor_metadata,
        )

        if not feature_parts:
            update_metadata(
                processor_metadata,
                {"fallbackUsed": True, "fallbackReason": "no_feature_values"},
            )
            return fallback_layout(rows, grouping_fields, cluster_config)

        features = np.concatenate(feature_parts, axis=1)
        update_metadata(processor_metadata, {"featureDimensions": int(features.shape[1])})
        points = reduce_to_points(features)
        labels = cluster_points(points, len(rows), cluster_config)
        return attach_layout_and_labels(rows, points, labels, grouping_fields, cluster_config)
    except EmbeddingProcessingError:
        raise
    except ProcessingError:
        raise
    except Exception as error:
        if options["textFeatureMethod"] == "embedding":
            raise ProcessingError("Embedding processor failed after feature generation.") from error
        print(f"PaCMAP/HDBSCAN processing failed; using fallback layout: {error}", file=sys.stderr)
        update_metadata(
            processor_metadata,
            {"fallbackUsed": True, "fallbackReason": "algorithm_failure"},
        )
        return fallback_layout(rows, grouping_fields, cluster_config)
