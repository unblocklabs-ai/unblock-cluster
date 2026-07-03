from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

Bucket = Literal["day", "week", "month"]


@dataclass(frozen=True)
class TrendPoint:
    cluster_id: int
    bucket_start: str
    count: int
    total: int
    share: float
    baseline_mean: float
    baseline_std: float
    spike_score: float


@dataclass(frozen=True)
class TrendComputation:
    bucket: Bucket
    buckets: list[str]
    points: list[TrendPoint]
    summary: dict


def bucket_start(timestamp_ms: int, bucket: Bucket) -> str:
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    if bucket == "day":
        return dt.date().isoformat()
    if bucket == "week":
        monday = dt.date() - timedelta(days=dt.weekday())
        return monday.isoformat()
    if bucket == "month":
        return f"{dt.year:04d}-{dt.month:02d}-01"
    raise ValueError(f"unsupported bucket: {bucket}")


def compute_trends(
    memberships: list[dict],
    *,
    bucket: Bucket,
    window_start: str | None = None,
    window_end: str | None = None,
) -> TrendComputation:
    if not memberships:
        summary = _summary([], {}, {}, window_start, window_end, bucket)
        return TrendComputation(bucket=bucket, buckets=[], points=[], summary=summary)

    all_bucket_values = [bucket_start(int(row["timestamp_ms"]), bucket) for row in memberships]
    buckets = _bucket_range(min(all_bucket_values), max(all_bucket_values), bucket)
    totals = {bucket_value: 0 for bucket_value in buckets}
    clusters = sorted(
        {
            int(row["cluster_id"])
            for row in memberships
            if int(row["cluster_id"]) != -1 and not bool(row.get("is_noise"))
        }
    )
    counts = {
        cluster_id: {bucket_value: 0 for bucket_value in buckets}
        for cluster_id in clusters
    }
    for row in memberships:
        current_bucket = bucket_start(int(row["timestamp_ms"]), bucket)
        totals[current_bucket] += 1
        cluster_id = int(row["cluster_id"])
        if cluster_id != -1 and not bool(row.get("is_noise")):
            counts[cluster_id][current_bucket] += 1

    points: list[TrendPoint] = []
    by_cluster: dict[int, list[TrendPoint]] = defaultdict(list)
    for cluster_id in clusters:
        prior_counts: list[int] = []
        for current_bucket in buckets:
            count = counts[cluster_id][current_bucket]
            baseline = prior_counts[-8:]
            mean = sum(baseline) / len(baseline) if baseline else 0.0
            std = _population_std(baseline, mean) if baseline else 0.0
            denominator = max(std, math.sqrt(mean), 1.0)
            point = TrendPoint(
                cluster_id=cluster_id,
                bucket_start=current_bucket,
                count=count,
                total=totals[current_bucket],
                share=count / totals[current_bucket] if totals[current_bucket] else 0.0,
                baseline_mean=mean,
                baseline_std=std,
                spike_score=(count - mean) / denominator,
            )
            points.append(point)
            by_cluster[cluster_id].append(point)
            prior_counts.append(count)

    resolved_window_start = window_start or (buckets[0] if buckets else None)
    resolved_window_end = window_end or (buckets[-1] if buckets else None)
    summary = _summary(
        buckets,
        by_cluster,
        totals,
        resolved_window_start,
        resolved_window_end,
        bucket,
    )
    return TrendComputation(bucket=bucket, buckets=buckets, points=points, summary=summary)


def _summary(
    buckets: list[str],
    by_cluster: dict[int, list[TrendPoint]],
    totals: dict[str, int],
    window_start: str | None,
    window_end: str | None,
    bucket: Bucket,
) -> dict:
    window_buckets = [
        bucket_value
        for bucket_value in buckets
        if (window_start is None or bucket_value >= window_start)
        and (window_end is None or bucket_value <= window_end)
    ]
    window_set = set(window_buckets)
    window = {
        "start": window_start,
        "end": window_end,
    }
    if not buckets or not window_buckets:
        return {
            "window": window,
            "bucket": bucket,
            "surprisingTopics": [],
            "newTopics": [],
            "vanishingTopics": [],
            "risingTopics": [],
            "fallingTopics": [],
        }

    window_start_index = buckets.index(window_buckets[0])
    previous_buckets = buckets[max(0, window_start_index - 8) : window_start_index]
    has_pre_window = bool(previous_buckets)
    surprising = []
    new_topics = []
    vanishing = []
    rising = []
    falling = []

    for cluster_id, points in by_cluster.items():
        points_by_bucket = {point.bucket_start: point for point in points}
        window_points = [points_by_bucket[bucket_value] for bucket_value in window_buckets]
        previous_points = [points_by_bucket[bucket_value] for bucket_value in previous_buckets]
        max_point = max(
            window_points,
            key=lambda point: (point.spike_score, point.count, point.bucket_start),
        )
        if max_point.spike_score > 0:
            surprising.append(
                {
                    "clusterId": cluster_id,
                    "spikeScore": max_point.spike_score,
                    "topBucket": max_point.bucket_start,
                    "count": max_point.count,
                }
            )

        nonzero_buckets = [point.bucket_start for point in points if point.count > 0]
        if nonzero_buckets:
            first_nonzero = nonzero_buckets[0]
            window_count = sum(point.count for point in window_points)
            if (
                first_nonzero in window_set
                and first_nonzero != buckets[0]
                and window_count >= 5
            ):
                new_topics.append(
                    {
                        "clusterId": cluster_id,
                        "firstBucket": first_nonzero,
                        "count": window_count,
                    }
                )

        if has_pre_window:
            previous_mean_count = (
                sum(point.count for point in previous_points) / len(previous_points)
                if previous_points
                else 0.0
            )
            window_count = sum(point.count for point in window_points)
            if window_count == 0 and previous_mean_count >= 1.0:
                vanishing.append(
                    {
                        "clusterId": cluster_id,
                        "baselineMean": previous_mean_count,
                        "windowCount": 0,
                    }
                )

            window_mean_share = sum(point.share for point in window_points) / len(window_points)
            previous_mean_share = (
                sum(point.share for point in previous_points) / len(previous_points)
                if previous_points
                else 0.0
            )
            delta = window_mean_share - previous_mean_share
            entry = {
                "clusterId": cluster_id,
                "deltaShare": delta,
                "windowMeanShare": window_mean_share,
                "previousMeanShare": previous_mean_share,
            }
            if delta > 0:
                rising.append(entry)
            elif delta < 0:
                falling.append(entry)

    return {
        "window": window,
        "bucket": bucket,
        "surprisingTopics": sorted(
            surprising,
            key=lambda row: (-row["spikeScore"], -row["count"], row["clusterId"]),
        ),
        "newTopics": sorted(new_topics, key=lambda row: (row["firstBucket"], row["clusterId"])),
        "vanishingTopics": sorted(
            vanishing,
            key=lambda row: (-row["baselineMean"], row["clusterId"]),
        ),
        "risingTopics": sorted(rising, key=lambda row: (-row["deltaShare"], row["clusterId"])),
        "fallingTopics": sorted(falling, key=lambda row: (row["deltaShare"], row["clusterId"])),
    }


def _bucket_range(start: str, end: str, bucket: Bucket) -> list[str]:
    current = _parse_bucket(start)
    last = _parse_bucket(end)
    values = []
    while current <= last:
        values.append(_format_bucket(current, bucket))
        current = _next_bucket(current, bucket)
    return values


def _parse_bucket(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _format_bucket(value: datetime, bucket: Bucket) -> str:
    if bucket == "month":
        return f"{value.year:04d}-{value.month:02d}-01"
    return value.date().isoformat()


def _next_bucket(value: datetime, bucket: Bucket) -> datetime:
    if bucket == "day":
        return value + timedelta(days=1)
    if bucket == "week":
        return value + timedelta(days=7)
    if bucket == "month":
        if value.month == 12:
            return value.replace(year=value.year + 1, month=1, day=1)
        return value.replace(month=value.month + 1, day=1)
    raise ValueError(f"unsupported bucket: {bucket}")


def _population_std(values: list[int], mean: float) -> float:
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
