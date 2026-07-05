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
  const maxRecordTimestamp = timestamps.length ? Math.max(...timestamps) : null;
  const timeExtent = timestamps.length
    ? {
        min: new Date(Math.min(...timestamps)).toISOString().slice(0, 10),
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

export function spikeBadge(topic, threshold = 3) {
  if (!topic?.trend || topic.trend.spikeScore < threshold) return null;
  return {
    text: `Spike ${topic.trend.spikeScore.toFixed(1)} in ${topic.trend.topBucket}`,
    bucket: topic.trend.topBucket,
    score: topic.trend.spikeScore,
  };
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
  return `${formatBucketLabel(first.bucketStart)} – ${formatBucketLabel(last.bucketStart)} · peak ${formatInteger(peak.count)} (${formatBucketLabel(peak.bucketStart)})`;
}

export function trendSparklineParts(buckets = [], options = {}) {
  const prepared = prepareSparkline(buckets, options);
  const points = prepared.points;
  if (!points.length) return { linePath: "", partialPath: "", title: "" };
  const partial = prepared.finalBucketPartial && points.length > 1;
  const linePoints = partial ? points.slice(0, -1) : points;
  const linePath = pointsToPath(linePoints);
  const partialPath = partial ? pointsToPath(points.slice(-2)) : "";
  return {
    linePath,
    partialPath,
    title: trendSparklineTitle(buckets, options),
    partialPoint: prepared.finalBucketPartial ? points[points.length - 1] : null,
  };
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
    return { buckets: [], points: [], finalBucketPartial: false };
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
  return {
    buckets: trimmed,
    points,
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

function formatBucketLabel(value) {
  const date = parseDateOnly(value);
  if (!date) return String(value || "");
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(date);
}

function formatInteger(value) {
  return Math.trunc(Number(value) || 0).toLocaleString("en-US");
}

function positiveNumber(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function roundPath(value) {
  return Math.round(value * 100) / 100;
}

function parseDateOnly(value) {
  if (!value) return null;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

function toDateOnly(date) {
  return date.toISOString().slice(0, 10);
}
