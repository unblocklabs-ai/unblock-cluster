import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import hdbscan
import numpy as np
import pacmap
from sklearn.preprocessing import StandardScaler


def text_for_record(record, fields):
    parts = []
    for field in fields:
        value = record.get(field)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif value is not None:
            parts.append(str(value))
    return " ".join(parts)


def normalize_points(points):
    scaler = StandardScaler()
    scaled = scaler.fit_transform(points)
    return scaled * 1000


def cluster_label(records, label, grouping_field):
    if label == -1:
        return "Outliers"
    if records and "cylinders" in records[0]:
        cylinders = average(records, "cylinders")
        mpg = average(records, "milesPerGallon")
        weight = average(records, "weightInLbs")
        year = average(records, "year")

        if cylinders >= 7 and mpg < 18:
            return "Heavy V8s"
        if cylinders >= 7:
            return "V8 cruisers"
        if cylinders >= 5.5:
            return "Midweight 6-cyl"
        if mpg >= 28 and year >= 1976:
            return "Later economy"
        if mpg >= 26:
            return "Efficient 4-cyl"
        if weight <= 2400:
            return "Light 4-cyl"
        return "Classic 4-cyl"
    values = [str(record.get(grouping_field, "Unknown")) for record in records]
    name, _ = Counter(values).most_common(1)[0]
    return name


def average(records, field):
    values = []
    for record in records:
        value = record.get(field)
        if value is None or value == "":
            continue
        try:
            values.append(float(value))
        except ValueError:
            continue
    if not values:
        return 0
    return sum(values) / len(values)


def main():
    parser = argparse.ArgumentParser(description="Generate PaCMAP/HDBSCAN layout JSON.")
    parser.add_argument("--input", default="sample-data/cars.json")
    parser.add_argument("--output", default="sample-data/cars.processed.json")
    args = parser.parse_args()

    source_path = Path(args.input)
    dataset = json.loads(source_path.read_text())
    records = dataset["records"]
    grouping_field = dataset.get("groupingField", "genre")
    title_field = dataset.get("titleField", "title")
    detail_field = dataset.get("detailField", "summary")
    feature_fields = dataset.get(
        "featureFields",
        [title_field, detail_field, grouping_field, "creativeType", "source", "director", "mpaaRating"],
    )
    numeric_fields = dataset.get("numericFields", [])

    feature_parts = []

    if numeric_fields:
        numeric = np.array(
            [
                [float(record.get(field) or 0) for field in numeric_fields]
                for record in records
            ],
            dtype=float,
        )
        feature_parts.append(StandardScaler().fit_transform(numeric))

    if feature_fields:
        from sklearn.feature_extraction.text import TfidfVectorizer

        corpus = [text_for_record(record, feature_fields) for record in records]
        text_matrix = TfidfVectorizer(max_features=512, stop_words="english").fit_transform(corpus)
        text_features = np.asarray(text_matrix.toarray(), dtype=float)  # type: ignore[attr-defined]
        feature_parts.append(text_features)

    if not feature_parts:
        raise ValueError("Dataset must provide numericFields or featureFields for clustering.")

    features = np.concatenate(feature_parts, axis=1)

    reducer = pacmap.PaCMAP(n_components=2, MN_ratio=0.5, FP_ratio=2.0, random_state=42)
    points = normalize_points(reducer.fit_transform(features))

    min_cluster_size = int(dataset.get("minClusterSize", max(4, min(12, len(records) // 18))))
    labels = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, min_samples=2).fit_predict(points)

    grouped = defaultdict(list)
    for record, label in zip(records, labels):
        grouped[int(label)].append(record)

    label_names = {
        label: cluster_label(cluster_records, label, grouping_field)
        for label, cluster_records in grouped.items()
    }

    processed_records = []
    for record, point, label in zip(records, points, labels):
        processed = dict(record)
        processed["x"] = float(point[0])
        processed["y"] = float(point[1])
        processed["clusterId"] = int(label)
        processed["clusterLabel"] = label_names[int(label)]
        processed["groupValue"] = str(record.get(grouping_field, "Unknown"))
        processed_records.append(processed)

    output = {
        **dataset,
        "sourceFile": str(source_path),
        "layout": {
            "method": "PaCMAP",
            "clusterMethod": "HDBSCAN",
            "featureFields": feature_fields,
            "numericFields": numeric_fields,
            "minClusterSize": min_cluster_size,
        },
        "records": processed_records,
    }
    Path(args.output).write_text(json.dumps(output, indent=2))
    print(f"Wrote {args.output} with {len(processed_records)} records and {len(grouped)} clusters.")


if __name__ == "__main__":
    main()
