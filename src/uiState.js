import { trustedImageUrl } from "./urlPolicy.js";

export const INSPECTOR_WIDTH_KEY = "dataAtlasInspectorWidth";
export const INSPECTOR_MIN_WIDTH = 320;
export const INSPECTOR_MAX_WIDTH = 620;
const fallbackRecordIdFields = [
  "id",
  "ticketId",
  "sourceTicketId",
  "sourceId",
  "issueId",
  "key",
];

export function savedInspectorWidth(rawValue) {
  if (rawValue === null || rawValue.trim() === "") return null;
  const width = Number(rawValue);
  return Number.isFinite(width) ? width : null;
}

export function boundedInspectorWidth(
  width,
  viewportWidth,
  minWidth = INSPECTOR_MIN_WIDTH,
  maxWidth = INSPECTOR_MAX_WIDTH,
) {
  const availableMaxWidth = Math.min(
    maxWidth,
    Math.max(minWidth, viewportWidth - 420),
  );
  return Math.max(minWidth, Math.min(availableMaxWidth, width));
}

export function recordIdentity(record, dataset = {}) {
  const fields = [
    dataset.recordIdField,
    ...fallbackRecordIdFields,
  ].filter(Boolean);
  for (const field of fields) {
    const value = record?.[field];
    if (value !== undefined && value !== null && String(value) !== "") {
      return String(value);
    }
  }
  return null;
}

export function recordMatchesQuery(record, dataset = {}, query = "") {
  const normalized = String(query || "").trim().toLocaleLowerCase();
  if (!normalized) return true;
  const fields = [
    dataset.recordIdField,
    ...fallbackRecordIdFields,
    dataset.titleField,
    dataset.detailField,
  ].filter(Boolean);
  const values = fields.map((field) => record?.[field]).filter((value) => value !== undefined && value !== null);
  return values.some((value) =>
    String(value).toLocaleLowerCase().includes(normalized),
  );
}

export function selectionFromSearch(search, records, clusters, dataset = {}) {
  const params = new URLSearchParams(search);
  const recordParam = params.get("record");
  const recordIndexParam = params.get("recordIndex");
  const clusterParam = params.get("cluster");
  if (recordParam) {
    const record = records.find((entry) => recordIdentity(entry, dataset) === recordParam);
    if (record) return { type: "record", value: record };
  }
  if (recordIndexParam) {
    const record = records.find(
      (entry) => String(entry.__index) === recordIndexParam,
    );
    if (record) return { type: "record", value: record };
  }
  if (clusterParam) {
    const cluster = clusters.find(
      (entry) => entry.id === clusterParam || entry.name === clusterParam,
    );
    if (cluster) return { type: "cluster", value: cluster };
  }
  return null;
}

export function selectionSearchParams(currentSearch, selection, dataset = {}) {
  const params = new URLSearchParams(currentSearch);
  params.delete("cluster");
  params.delete("record");
  params.delete("recordIndex");
  if (selection?.type === "cluster") {
    params.set("cluster", selection.value.name);
  } else if (selection?.type === "record") {
    const record = selection.value;
    const identity = recordIdentity(record, dataset);
    if (identity) {
      params.set("record", identity);
    } else {
      params.set("recordIndex", String(record.__index));
    }
  }
  return params;
}

export function tokenStorageKey(graphId) {
  return graphId ? `dataGraphApiToken:${graphId}` : "dataGraphApiToken";
}

export function recordImageUrlForField(record, imageField, baseUrl) {
  return trustedImageUrl(imageField ? record?.[imageField] : null, baseUrl);
}
