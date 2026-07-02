import { trustedImageUrl } from "./urlPolicy.js";

export const INSPECTOR_WIDTH_KEY = "dataAtlasInspectorWidth";
export const INSPECTOR_MIN_WIDTH = 320;
export const INSPECTOR_MAX_WIDTH = 620;

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

export function selectionFromSearch(search, records, clusters) {
  const params = new URLSearchParams(search);
  const recordParam = params.get("record");
  const recordIndexParam = params.get("recordIndex");
  const clusterParam = params.get("cluster");
  if (recordParam) {
    const record = records.find((entry) => String(entry.id ?? "") === recordParam);
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

export function selectionSearchParams(currentSearch, selection) {
  const params = new URLSearchParams(currentSearch);
  params.delete("cluster");
  params.delete("record");
  params.delete("recordIndex");
  if (selection?.type === "cluster") {
    params.set("cluster", selection.value.name);
  } else if (selection?.type === "record") {
    const record = selection.value;
    if (record.id !== undefined && record.id !== null) {
      params.set("record", String(record.id));
    } else {
      params.set("recordIndex", String(record.__index));
    }
  }
  return params;
}

export function recordImageUrlForField(record, imageField, baseUrl) {
  return trustedImageUrl(imageField ? record?.[imageField] : null, baseUrl);
}
