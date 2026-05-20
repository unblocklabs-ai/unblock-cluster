import json
import sys


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


def group_value_for_record(record, grouping_fields):
    if len(grouping_fields) == 1:
        value = record.get(grouping_fields[0])
        return str(value) if value not in (None, "") else "Unknown"
    return " / ".join(
        f"{field}: {record.get(field) if record.get(field) not in (None, '') else 'Unknown'}"
        for field in grouping_fields
    )


def cluster_label(records, label, grouping_fields):
    if label == -1:
        return "Outliers"
    counts = {}
    for record in records:
        value = group_value_for_record(record, grouping_fields)
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return f"Cluster {label}"
    return max(counts.items(), key=lambda item: item[1])[0]


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


def normalize_points(points):
    import numpy as np
    from sklearn.preprocessing import StandardScaler

    if len(points) <= 1:
        return np.asarray(points, dtype=float)
    scaled = StandardScaler().fit_transform(points)
    return scaled * 1000


def fallback_layout(records, grouping_fields):
    import math

    grouped = {}
    for record in records:
        grouped.setdefault(group_value_for_record(record, grouping_fields), []).append(record)
    groups = list(grouped.items())
    group_count = max(len(groups), 1)
    processed = []
    for group_index, (group_value, items) in enumerate(groups):
        center_angle = (math.pi * 2 * group_index) / group_count
        center_radius = 0 if group_count == 1 else 1200
        center_x = math.cos(center_angle) * center_radius
        center_y = math.sin(center_angle) * center_radius
        for item_index, record in enumerate(items):
            item_angle = item_index * 2.399963229728653
            item_radius = 70 + (item_index ** 0.5) * 52
            processed.append(
                {
                    **record,
                    "x": center_x + math.cos(item_angle) * item_radius,
                    "y": center_y + math.sin(item_angle) * item_radius,
                    "clusterId": group_index,
                    "clusterLabel": group_value,
                    "groupValue": group_value,
                }
            )
    return processed


def process_records(sink, rows):
    if not rows:
        return []

    config = sink["config"]
    cluster_config = config.get("cluster") or {}
    grouping_fields = config["groupingFields"]
    numeric_fields = cluster_config.get("numericFields", default_numeric_fields(config))
    feature_fields = cluster_config.get("featureFields", default_feature_fields(config))
    numeric_fields = [field for field in numeric_fields if field in config["dataSchema"]]
    feature_fields = [field for field in feature_fields if field in config["dataSchema"]]

    if len(rows) < 3:
        return fallback_layout(rows, grouping_fields)

    try:
        import hdbscan
        import numpy as np
        import pacmap
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.preprocessing import StandardScaler

        feature_parts = []

        if numeric_fields:
            numeric = np.array(
                [[float(record.get(field) or 0) for field in numeric_fields] for record in rows],
                dtype=float,
            )
            feature_parts.append(StandardScaler().fit_transform(numeric))

        if feature_fields:
            corpus = [text_for_record(record, feature_fields) for record in rows]
            if any(text.strip() for text in corpus):
                text_matrix = TfidfVectorizer(max_features=512, stop_words="english").fit_transform(corpus)
                feature_parts.append(np.asarray(text_matrix.toarray(), dtype=float))

        if not feature_parts:
            return fallback_layout(rows, grouping_fields)

        features = np.concatenate(feature_parts, axis=1)
        reducer = pacmap.PaCMAP(n_components=2, MN_ratio=0.5, FP_ratio=2.0, random_state=42)
        points = normalize_points(reducer.fit_transform(features))

        min_cluster_size = int(
            cluster_config.get("minClusterSize", max(3, min(12, len(rows) // 6 or 3)))
        )
        min_cluster_size = max(2, min(min_cluster_size, len(rows)))
        labels = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, min_samples=2).fit_predict(points)

        grouped_records = {}
        for record, label in zip(rows, labels):
            grouped_records.setdefault(int(label), []).append(record)
        label_names = {
            label: cluster_label(cluster_records, label, grouping_fields)
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
    except Exception as error:
        print(f"PaCMAP/HDBSCAN processing failed; using fallback layout: {error}", file=sys.stderr)
        return fallback_layout(rows, grouping_fields)
