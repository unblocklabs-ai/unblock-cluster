export function parseViewParams(search = "") {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  return {
    graphId: params.get("graphId"),
    viewId: params.get("viewId"),
    mode: params.get("mode"),
    topicId: params.get("topicId"),
  };
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
  const timeExtent = timestamps.length
    ? {
        min: new Date(Math.min(...timestamps)).toISOString().slice(0, 10),
        max: new Date(Math.max(...timestamps)).toISOString().slice(0, 10),
      }
    : { min: "", max: "" };
  return {
    artifact,
    topics,
    topicById,
    recordById,
    sourceTypes,
    timeExtent,
    filters: {
      query: options.query || "",
      topicId: normalizeTopicId(options.topicId),
      sourceType: options.sourceType || "",
      start: options.start || "",
      end: options.end || "",
    },
    selectedTopicId: normalizeNullableTopicId(options.selectedTopicId),
    selectedRecordId: normalizeRecordId(options.selectedRecordId),
  };
}

export function updateFilters(state, patch) {
  return {
    ...state,
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
    filters: {
      ...state.filters,
      topicId: normalizedTopicId,
    },
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
    filters: {
      ...state.filters,
      topicId: "",
    },
  };
}

export function visibleRecords(state) {
  const records = state.artifact?.data || [];
  return records.filter((record) => recordMatchesFilters(record, state.filters));
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
