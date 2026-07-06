const LIST_PAGE_SIZE = 500;
const TOPIC_SORTS = new Set(["size", "spike", "name"]);
export const NOISE_TOPIC_ID = -1;

export function parseViewParams(search = "") {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  return {
    graphId: params.get("graphId"),
    viewId: params.get("viewId"),
    mode: params.get("mode"),
    topicId: params.get("topicId"),
    recordId: params.get("recordId"),
  };
}

export function viewSearchParams(state, mode = "map") {
  const params = new URLSearchParams();
  if (state?.artifact?.graphId) params.set("graphId", state.artifact.graphId);
  if (state?.artifact?.viewId) params.set("viewId", state.artifact.viewId);
  if (mode === "list") params.set("mode", "list");
  if (state?.selectedTopicId !== null && state?.selectedTopicId !== undefined) {
    params.set("topicId", String(state.selectedTopicId));
  }
  if (state?.selectedRecordId) {
    params.set("recordId", state.selectedRecordId);
  }
  return `?${params.toString()}`;
}

export function topicLabel(topic) {
  return topic?.label || `Topic ${topic?.clusterId ?? "unknown"}`;
}

export function buildViewState(artifact, options = {}) {
  const topics = [...(artifact?.topics || [])].sort(
    (a, b) => b.size - a.size || a.clusterId - b.clusterId,
  );
  const topicById = new Map(topics.map((topic) => [topic.clusterId, topic]));
  const recordById = new Map((artifact?.data || []).map((record) => [record.id, record]));
  const sourceTypes = [
    ...new Set((artifact?.data || []).map((record) => record.sourceType).filter(Boolean)),
  ].sort((a, b) => a.localeCompare(b));
  const timestamps = (artifact?.data || [])
    .map((record) => Date.parse(record.timestamp))
    .filter((value) => Number.isFinite(value));
  const minRecordTimestamp = timestamps.length ? Math.min(...timestamps) : null;
  const maxRecordTimestamp = timestamps.length ? Math.max(...timestamps) : null;
  const timeExtent = timestamps.length
    ? {
        min: new Date(minRecordTimestamp).toISOString().slice(0, 10),
        max: new Date(maxRecordTimestamp).toISOString().slice(0, 10),
      }
    : { min: "", max: "" };
  return {
    artifact,
    topics,
    topicById,
    recordById,
    sourceTypes,
    timeExtent,
    minRecordTimestamp,
    maxRecordTimestamp,
    filters: {
      query: options.query || "",
      topicId: normalizeTopicId(options.topicId),
      sourceType: options.sourceType || "",
      start: options.start || "",
      end: options.end || "",
    },
    selectedTopicId: normalizeNullableTopicId(options.selectedTopicId),
    selectedRecordId: normalizeRecordId(options.selectedRecordId),
    noiseSampleIds: Array.isArray(options.noiseSampleIds) ? options.noiseSampleIds : [],
    listLimit: normalizeListLimit(options.listLimit),
    topicSort: normalizeTopicSort(options.topicSort),
    topicSearch: normalizeSearch(options.topicSearch),
  };
}

export function updateFilters(state, patch) {
  return {
    ...state,
    listLimit: LIST_PAGE_SIZE,
    filters: {
      ...state.filters,
      ...patch,
      topicId:
        "topicId" in patch ? normalizeTopicId(patch.topicId) : state.filters.topicId,
    },
  };
}

export function selectTopic(state, topicId) {
  const normalizedTopicId = normalizeTopicId(topicId);
  return {
    ...state,
    selectedTopicId: normalizedTopicId === "" ? null : normalizedTopicId,
    selectedRecordId: null,
    noiseSampleIds: normalizedTopicId === NOISE_TOPIC_ID ? state.noiseSampleIds : [],
  };
}

export function selectNoise(state, records = visibleRecords(state), options = {}) {
  return {
    ...state,
    selectedTopicId: NOISE_TOPIC_ID,
    selectedRecordId: null,
    noiseSampleIds: sampleNoiseRecordIds(records, options),
  };
}

export function selectRecord(state, recordId) {
  const normalizedRecordId = normalizeRecordId(recordId);
  const record = normalizedRecordId ? state.recordById.get(normalizedRecordId) : null;
  if (!record) return state;
  return {
    ...state,
    selectedTopicId: record.clusterId,
    selectedRecordId: normalizedRecordId,
  };
}

export function updateTopicPanel(state, patch) {
  return {
    ...state,
    topicSort:
      "topicSort" in patch ? normalizeTopicSort(patch.topicSort) : state.topicSort,
    topicSearch:
      "topicSearch" in patch ? normalizeSearch(patch.topicSearch) : state.topicSearch,
  };
}

export function topicPanelTopics(state, records = visibleRecords(state)) {
  const visibleCounts = new Map();
  for (const record of records) {
    visibleCounts.set(record.clusterId, (visibleCounts.get(record.clusterId) || 0) + 1);
  }
  const query = normalizeSearch(state.topicSearch).toLowerCase();
  return [...state.topics]
    .filter((topic) => {
      if (!query) return true;
      const haystack = [
        topicLabel(topic),
        topic.summary,
        Object.keys(topic.sourceMix || {}).join(" "),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    })
    .sort(topicComparator(state.topicSort))
    .map((topic) => ({
      topic,
      visibleCount: visibleCounts.get(topic.clusterId) || 0,
      selected: topic.clusterId === state.selectedTopicId,
    }));
}

export function backToTopic(state) {
  if (state.selectedTopicId === null || state.selectedTopicId === undefined) {
    return state;
  }
  return {
    ...state,
    selectedRecordId: null,
  };
}

export function clearTopicSelection(state) {
  return {
    ...state,
    selectedTopicId: null,
    selectedRecordId: null,
    noiseSampleIds: [],
  };
}

export function visibleRecords(state) {
  const records = state.artifact?.data || [];
  return records.filter((record) => recordMatchesFilters(record, state.filters));
}

export function listWindow(records, state) {
  const total = records.length;
  const limit = normalizeListLimit(state?.listLimit);
  const showing = Math.min(total, limit);
  return {
    records: records.slice(0, showing),
    showing,
    total,
    remaining: Math.max(0, total - showing),
  };
}

export function showMoreListRecords(state, pageSize = LIST_PAGE_SIZE) {
  return {
    ...state,
    listLimit: normalizeListLimit(state.listLimit) + pageSize,
  };
}

function recordMatchesFilters(record, filters) {
  if (filters.topicId !== "" && record.clusterId !== Number(filters.topicId)) {
    return false;
  }
  if (filters.sourceType && record.sourceType !== filters.sourceType) {
    return false;
  }
  if (filters.start && record.timestamp.slice(0, 10) < filters.start) {
    return false;
  }
  if (filters.end && record.timestamp.slice(0, 10) > filters.end) {
    return false;
  }
  const query = filters.query.trim().toLowerCase();
  if (query) {
    const haystack = [
      record.recordId,
      record.title,
      record.customerText,
      record.sourceType,
      record.product,
      record.sentiment,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    if (!haystack.includes(query)) return false;
  }
  return true;
}

export function representativeRecords(topic, records) {
  const byId = new Map(records.map((record) => [record.id, record]));
  return (topic?.representativeRecordIds || [])
    .map((id) => byId.get(id))
    .filter(Boolean);
}

export function noiseRecords(records) {
  return records.filter((record) => record.isNoise || record.clusterId === NOISE_TOPIC_ID);
}

export function sampleNoiseRecordIds(records, options = {}) {
  const limit = positiveInteger(options.limit, 20);
  const rng = typeof options.rng === "function" ? options.rng : Math.random;
  return noiseRecords(records)
    .map((record) => ({ id: record.id, score: Number(rng()) }))
    .sort((left, right) => left.score - right.score || left.id.localeCompare(right.id))
    .slice(0, limit)
    .map((entry) => entry.id);
}

export function clusterColor(clusterId) {
  if (clusterId === -1) return "#8b949e";
  const palette = [
    "#2563eb",
    "#16a34a",
    "#d97706",
    "#dc2626",
    "#7c3aed",
    "#0891b2",
    "#be123c",
    "#4f46e5",
    "#0f766e",
    "#a16207",
    "#9333ea",
    "#15803d",
  ];
  return palette[Math.abs(Number(clusterId)) % palette.length];
}

export function spikeBadge(topic, threshold = 0) {
  return windowedSpikeBadge(topic, [], {}, { threshold, bucket: topic?.trend?.bucket });
}

export function windowedSpikeBadge(topic, buckets = [], filters = {}, options = {}) {
  const threshold = Number(options.threshold ?? 0);
  const bucketUnit = options.bucket || topic?.trend?.bucket || "week";
  const hasWindow = Boolean(filters?.start || filters?.end);
  if (!hasWindow) {
    if (!topic?.trend || topic.trend.spikeScore <= threshold || !topic.trend.topBucket) {
      return null;
    }
    return {
      text: `Spike ${topic.trend.spikeScore.toFixed(1)} · ${trendBucketReadoutLabel(topic.trend.topBucket, bucketUnit, { includeYearWhenDifferent: true, now: options.now })}`,
      bucket: topic.trend.topBucket,
      score: topic.trend.spikeScore,
    };
  }
  const windowed = trendWindowBuckets(buckets, filters, { bucket: bucketUnit });
  if (!windowed.length) {
    return null;
  }
  const best = windowed.reduce((winner, bucket) => {
    const score = Number(bucket.spikeScore || 0);
    if (!winner || score > winner.score) {
      return { bucket: bucket.bucketStart, score };
    }
    return winner;
  }, null);
  if (!best || best.score <= threshold || !best.bucket) return null;
  return {
    text: `Spike ${best.score.toFixed(1)} · ${trendBucketReadoutLabel(best.bucket, bucketUnit, { includeYearWhenDifferent: true, now: options.now })}`,
    bucket: best.bucket,
    score: best.score,
  };
}

export function trendWindowBuckets(buckets = [], filters = {}, options = {}) {
  const bucketUnit = options.bucket || "week";
  const start = parseDateOnly(filters?.start);
  const end = parseDateOnly(filters?.end);
  if (!start && !end) return Array.isArray(buckets) ? buckets : [];
  const windowStart = start ? start.getTime() : -Infinity;
  const windowEnd = end ? addUtcDays(end, 1).getTime() : Infinity;
  return (Array.isArray(buckets) ? buckets : []).filter((bucket) => {
    const bucketStart = parseDateOnly(bucket?.bucketStart);
    if (!bucketStart) return false;
    const bucketEnd = bucketEndDate(bucketStart, bucketUnit);
    return bucketStart.getTime() < windowEnd && bucketEnd.getTime() > windowStart;
  });
}

export function compactSourceMix(sourceMix = {}, limit = 2) {
  const entries = Object.entries(sourceMix || {})
    .filter(([, count]) => Number.isFinite(Number(count)) && Number(count) > 0)
    .sort(
      ([leftSource, leftCount], [rightSource, rightCount]) =>
        Number(rightCount) - Number(leftCount) || leftSource.localeCompare(rightSource),
    );
  if (!entries.length) return "";
  const visibleLimit = Math.max(1, Math.trunc(Number(limit) || 2));
  const visible = entries
    .slice(0, visibleLimit)
    .map(([source, count]) => `${source} ${formatInteger(count)}`);
  const remaining = entries.length - visible.length;
  return remaining > 0 ? `${visible.join(" · ")} · +${remaining} more` : visible.join(" · ");
}

export function listTopicCell(record, topic) {
  const noise = record?.isNoise || record?.clusterId === NOISE_TOPIC_ID || !topic;
  const label = noise ? "Noise" : topicLabel(topic);
  const color = noise ? "#8b949e" : clusterColor(record.clusterId);
  return `
    <span class="topic-cell-content" title="${escapeAttr(label)}">
      <span class="topic-swatch list-topic-swatch${noise ? " noise-swatch" : ""}" style="background:${escapeAttr(color)}"></span>
      <span class="topic-cell-label">${escapeHtml(label)}</span>
    </span>
  `;
}

export function listRecordTitleCell(record) {
  const title = record?.title || record?.recordId || "";
  return `<span class="single-line" title="${escapeAttr(title)}">${escapeHtml(title)}</span>`;
}

export function sentimentCell(sentiment) {
  const value = String(sentiment ?? "").trim();
  if (!value) return "";
  const normalized = value.toLowerCase();
  const known = {
    positive: { glyph: "●", className: "positive" },
    neutral: { glyph: "●", className: "neutral" },
    negative: { glyph: "●", className: "negative" },
  }[normalized];
  if (!known) {
    return `<span class="sentiment-text" title="${escapeAttr(value)}">${escapeHtml(value)}</span>`;
  }
  return `<span class="sentiment-icon sentiment-${known.className}" title="${escapeAttr(value)}" aria-label="${escapeAttr(value)}">${known.glyph}</span>`;
}

export function trendSparklinePath(buckets = [], options = {}) {
  const parts = trendSparklineParts(buckets, options);
  return parts.linePath;
}

export function trendSparklinePartialPath(buckets = [], options = {}) {
  const parts = trendSparklineParts(buckets, options);
  return parts.partialPath;
}

export function trendSparklineTitle(buckets = [], options = {}) {
  const prepared = prepareSparkline(buckets, options);
  if (!prepared.buckets.length) return "";
  const peak = prepared.buckets.reduce((best, bucket) =>
    bucket.count > best.count ? bucket : best,
  );
  const first = prepared.buckets[0];
  const last = prepared.buckets[prepared.buckets.length - 1];
  const startLabel = `${prepared.firstBucketPartial ? "(partial) " : ""}${formatBucketLabel(first.bucketStart)}`;
  const endLabel = `${formatBucketLabel(last.bucketStart)}${prepared.finalBucketPartial ? " (partial)" : ""}`;
  return `${startLabel} – ${endLabel} · peak ${formatInteger(peak.count)} (${formatBucketLabel(peak.bucketStart)})`;
}

export function trendSparklineParts(buckets = [], options = {}) {
  const prepared = prepareSparkline(buckets, options);
  const points = prepared.points;
  if (!points.length) return { linePath: "", partialPath: "", title: "" };
  const firstPartial = prepared.firstBucketPartial && points.length > 1;
  const finalPartial = prepared.finalBucketPartial && points.length > 1;
  const linePoints = points.slice(firstPartial ? 1 : 0, finalPartial ? -1 : points.length);
  const linePath = pointsToPath(linePoints);
  const partialSegments = [
    firstPartial ? pointsToPath(points.slice(0, 2)) : "",
    finalPartial ? pointsToPath(points.slice(-2)) : "",
  ].filter(Boolean);
  const partialPoints = [
    firstPartial ? points[0] : null,
    finalPartial ? points[points.length - 1] : null,
  ].filter(Boolean);
  return {
    linePath,
    fullPath: pointsToPath(points),
    partialPath: partialSegments.join(" "),
    title: trendSparklineTitle(buckets, options),
    partialPoint: partialPoints[partialPoints.length - 1] || null,
    partialPoints,
    bucketPoints: prepared.bucketPoints,
    buckets: prepared.buckets,
  };
}

export function trendSparklineBucketIndexAtX(buckets = [], x = 0, options = {}) {
  const prepared = prepareSparkline(buckets, options);
  const count = prepared.buckets.length;
  if (!count) return null;
  if (count === 1) return 0;
  const width = positiveNumber(options.width, 96);
  const padding = Math.max(0, Number(options.padding ?? 2));
  const usableWidth = Math.max(1, width - padding * 2);
  const clampedX = Math.min(width - padding, Math.max(padding, Number(x) || 0));
  const ratio = (clampedX - padding) / usableWidth;
  return Math.min(count - 1, Math.max(0, Math.round(ratio * (count - 1))));
}

export function trendSparklinePointAtIndex(buckets = [], index = 0, options = {}) {
  const prepared = prepareSparkline(buckets, options);
  if (!prepared.buckets.length) return null;
  const clampedIndex = clampIndex(index, prepared.buckets.length);
  return {
    index: clampedIndex,
    bucket: prepared.buckets[clampedIndex],
    point: prepared.bucketPoints[clampedIndex],
    partial: isPreparedBucketPartial(prepared, clampedIndex),
  };
}

export function trendSparklinePointAtX(buckets = [], x = 0, options = {}) {
  const index = trendSparklineBucketIndexAtX(buckets, x, options);
  return index === null ? null : trendSparklinePointAtIndex(buckets, index, options);
}

export function trendSparklineKeyboardIndex(currentIndex, key, buckets = [], options = {}) {
  const prepared = prepareSparkline(buckets, options);
  const count = prepared.buckets.length;
  if (!count) return null;
  if (key === "Escape") return null;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  if (key === "ArrowRight") {
    return currentIndex === null || currentIndex === undefined
      ? 0
      : clampIndex(Number(currentIndex) + 1, count);
  }
  if (key === "ArrowLeft") {
    return currentIndex === null || currentIndex === undefined
      ? count - 1
      : clampIndex(Number(currentIndex) - 1, count);
  }
  return currentIndex === null || currentIndex === undefined
    ? null
    : clampIndex(Number(currentIndex), count);
}

export function trendSparklineReadout(buckets = [], index = 0, options = {}) {
  const detail = trendSparklinePointAtIndex(buckets, index, options);
  if (!detail) return "";
  return formatTrendBucketReadout(detail.bucket, {
    bucket: options.bucket,
    partial: detail.partial,
  });
}

export function formatTrendBucketReadout(bucket, options = {}) {
  if (!bucket) return "";
  const count = formatCountLabelLocal(bucket.count, "record");
  const label = trendBucketReadoutLabel(bucket.bucketStart, options.bucket);
  const partial = options.partial ? " (partial)" : "";
  const spikeScore = Number(bucket.spikeScore || 0);
  const spike = spikeScore > 0 ? ` · spike ${spikeScore.toFixed(1)}` : "";
  return `${count} · ${label}${partial}${spike}`;
}

export function datePresetFilters(timeExtent, preset) {
  if (preset === "all") return { start: "", end: "" };
  const days = { "7d": 7, "30d": 30, "90d": 90 }[preset];
  if (!days || !timeExtent?.max) return { start: "", end: "" };
  const endDate = parseDateOnly(timeExtent.max);
  if (!endDate) return { start: "", end: "" };
  const startDate = new Date(endDate);
  startDate.setUTCDate(startDate.getUTCDate() - days + 1);
  const minDate = parseDateOnly(timeExtent.min);
  const clampedStart = minDate && startDate < minDate ? minDate : startDate;
  return {
    start: toDateOnly(clampedStart),
    end: toDateOnly(endDate),
  };
}

export function applyDatePreset(state, preset) {
  return updateFilters(state, datePresetFilters(state.timeExtent, preset));
}

export function selectedTopicExtentRecords(state, records = visibleRecords(state)) {
  if (state.selectedTopicId === null || state.selectedTopicId === undefined) return records;
  if (state.selectedTopicId === NOISE_TOPIC_ID) return noiseRecords(records);
  return records.filter((record) => record.clusterId === state.selectedTopicId);
}

function topicComparator(sort) {
  if (sort === "name") {
    return (a, b) => topicLabel(a).localeCompare(topicLabel(b)) || a.clusterId - b.clusterId;
  }
  if (sort === "spike") {
    return (a, b) =>
      (b.trend?.spikeScore || 0) - (a.trend?.spikeScore || 0) ||
      b.size - a.size ||
      a.clusterId - b.clusterId;
  }
  return (a, b) => b.size - a.size || a.clusterId - b.clusterId;
}

function normalizeTopicId(value) {
  if (value === null || value === undefined || value === "") return "";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : "";
}

function normalizeNullableTopicId(value) {
  const normalized = normalizeTopicId(value);
  return normalized === "" ? null : normalized;
}

function normalizeRecordId(value) {
  if (value === null || value === undefined || value === "") return null;
  return String(value);
}

function normalizeListLimit(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : LIST_PAGE_SIZE;
}

function positiveInteger(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

function normalizeTopicSort(value) {
  return TOPIC_SORTS.has(value) ? value : "size";
}

function normalizeSearch(value) {
  return String(value ?? "").trim();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("'", "&#039;");
}

function prepareSparkline(buckets = [], options = {}) {
  const width = positiveNumber(options.width, 96);
  const height = positiveNumber(options.height, 24);
  const padding = Math.max(0, Number(options.padding ?? 2));
  const finiteBuckets = buckets
    .map((bucket) => ({
      ...bucket,
      count: Number(bucket.count ?? 0),
    }))
    .filter((bucket) => Number.isFinite(bucket.count));
  if (!finiteBuckets.length) {
    return { buckets: [], points: [], firstBucketPartial: false, finalBucketPartial: false };
  }
  const firstNonzero = finiteBuckets.findIndex((bucket) => bucket.count > 0);
  const trimmed = firstNonzero === -1 ? finiteBuckets : finiteBuckets.slice(firstNonzero);
  const values = trimmed.map((bucket) => bucket.count);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const usableWidth = Math.max(1, width - padding * 2);
  const usableHeight = Math.max(1, height - padding * 2);
  const points =
    values.length === 1
      ? [
          { x: padding, y: height / 2 },
          { x: width - padding, y: height / 2 },
        ]
      : values.map((value, index) => {
          const x = padding + (usableWidth * index) / (values.length - 1);
          const ratio = span === 0 ? 0.5 : (value - min) / span;
          const y = padding + usableHeight * (1 - ratio);
          return { x, y };
        });
  const bucketPoints =
    values.length === 1
      ? [{ x: width / 2, y: height / 2 }]
      : points.map((point) => ({ ...point }));
  return {
    buckets: trimmed,
    points,
    bucketPoints,
    firstBucketPartial: isFirstBucketPartial(trimmed[0], options),
    finalBucketPartial: isFinalBucketPartial(trimmed[trimmed.length - 1], options),
  };
}

function pointsToPath(points) {
  if (!points.length) return "";
  return points
    .map(
      (point, index) =>
        `${index === 0 ? "M" : "L"} ${roundPath(point.x)} ${roundPath(point.y)}`,
    )
    .join(" ");
}

function isFinalBucketPartial(bucket, options) {
  if (!bucket?.bucketStart || !options.maxRecordTimestamp || !options.bucket) return false;
  const start = parseDateOnly(bucket.bucketStart);
  const maxTimestamp = new Date(options.maxRecordTimestamp);
  if (!start || Number.isNaN(maxTimestamp.getTime())) return false;
  const end = new Date(start);
  if (options.bucket === "day") {
    end.setUTCDate(end.getUTCDate() + 1);
  } else if (options.bucket === "week") {
    end.setUTCDate(end.getUTCDate() + 7);
  } else if (options.bucket === "month") {
    end.setUTCMonth(end.getUTCMonth() + 1);
  } else {
    return false;
  }
  return end.getTime() > maxTimestamp.getTime();
}

function isFirstBucketPartial(bucket, options) {
  if (!bucket?.bucketStart || !options.minRecordTimestamp || !options.bucket) return false;
  const start = parseDateOnly(bucket.bucketStart);
  const minTimestamp = new Date(options.minRecordTimestamp);
  if (!start || Number.isNaN(minTimestamp.getTime())) return false;
  return start.getTime() < minTimestamp.getTime();
}

function formatBucketLabel(value) {
  const date = parseDateOnly(value);
  if (!date) return String(value || "");
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(date);
}

export function trendBucketReadoutLabel(value, bucket, options = {}) {
  const date = parseDateOnly(value);
  if (!date) return String(value || "");
  if (bucket === "month") {
    return new Intl.DateTimeFormat("en-US", {
      month: "short",
      year: "numeric",
      timeZone: "UTC",
    }).format(date);
  }
  const includeYear =
    options.includeYearWhenDifferent && date.getUTCFullYear() !== currentUtcYear(options.now);
  const label = includeYear ? formatBucketLabelWithYear(value) : formatBucketLabel(value);
  return bucket === "week" ? `week of ${label}` : label;
}

function formatInteger(value) {
  return Math.trunc(Number(value) || 0).toLocaleString("en-US");
}

function formatCountLabelLocal(value, singular, plural = `${singular}s`) {
  const count = Math.trunc(Number(value) || 0);
  return `${formatInteger(count)} ${count === 1 ? singular : plural}`;
}

function positiveNumber(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function roundPath(value) {
  return Math.round(value * 100) / 100;
}

function clampIndex(index, count) {
  if (!Number.isFinite(index)) return 0;
  return Math.min(count - 1, Math.max(0, Math.trunc(index)));
}

function isPreparedBucketPartial(prepared, index) {
  return (
    (index === 0 && prepared.firstBucketPartial) ||
    (index === prepared.buckets.length - 1 && prepared.finalBucketPartial)
  );
}

function parseDateOnly(value) {
  if (!value) return null;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

function toDateOnly(date) {
  return date.toISOString().slice(0, 10);
}

function bucketEndDate(start, bucket) {
  const end = new Date(start);
  if (bucket === "day") {
    end.setUTCDate(end.getUTCDate() + 1);
  } else if (bucket === "month") {
    end.setUTCMonth(end.getUTCMonth() + 1);
  } else {
    end.setUTCDate(end.getUTCDate() + 7);
  }
  return end;
}

function addUtcDays(date, days) {
  const next = new Date(date);
  next.setUTCDate(next.getUTCDate() + days);
  return next;
}

function formatBucketLabelWithYear(value) {
  const date = parseDateOnly(value);
  if (!date) return String(value || "");
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}

function currentUtcYear(now = new Date()) {
  const date = now instanceof Date ? now : new Date(now);
  return Number.isNaN(date.getTime()) ? new Date().getUTCFullYear() : date.getUTCFullYear();
}
