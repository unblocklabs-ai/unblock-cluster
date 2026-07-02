import "ol/ol.css";
import Feature from "ol/Feature";
import OlMap from "ol/Map";
import View from "ol/View";
import Point from "ol/geom/Point";
import VectorLayer from "ol/layer/Vector";
import VectorSource from "ol/source/Vector";
import { Circle as CircleStyle, Fill, Stroke, Style, Text } from "ol/style";
import { boundingExtent } from "ol/extent";
import {
  INSPECTOR_WIDTH_KEY,
  boundedInspectorWidth,
  recordImageUrlForField,
  recordMatchesQuery,
  savedInspectorWidth,
  selectionFromSearch,
  selectionSearchParams,
  tokenStorageKey,
} from "./uiState.js";
import sampleManifest from "../sample-manifest.json";

const state = {
  dataset: null,
  records: [],
  fields: [],
  clusters: [],
  selected: null,
  mode: "map",
  map: null,
  source: null,
  layer: null,
  labelSource: null,
  labelLayer: null,
  searchQuery: "",
  graphId: null,
};

const palette = [
  "#b85f36",
  "#40798c",
  "#6a8f3f",
  "#9f5f80",
  "#d39b2a",
  "#4f6fb5",
  "#8a6f3d",
  "#2f8b72",
  "#a34848",
  "#5d6b73",
  "#5b5f68",
  "#c46f7e",
];

const els = {
  map: document.querySelector("#mapCanvas"),
  mapShell: document.querySelector("#mapShell"),
  listShell: document.querySelector("#listShell"),
  listContent: document.querySelector("#listContent"),
  inspector: document.querySelector(".inspector"),
  inspectorResizer: document.querySelector("#inspectorResizer"),
  stats: document.querySelector("#stats"),
  legend: document.querySelector("#legend"),
  details: document.querySelector("#details"),
  emptyState: document.querySelector("#emptyState"),
  viewTitle: document.querySelector("#viewTitle"),
  viewSubtitle: document.querySelector("#viewSubtitle"),
  topbarActions: document.querySelector(".topbar-actions"),
  viewToggleButton: document.querySelector("#viewToggleButton"),
  mapModeButton: document.querySelector("#mapModeButton"),
  zoomInButton: document.querySelector("#zoomInButton"),
  zoomOutButton: document.querySelector("#zoomOutButton"),
  recordSearch: document.querySelector("#recordSearch"),
  clearSearchButton: document.querySelector("#clearSearchButton"),
  forgetTokenButton: document.querySelector("#forgetTokenButton"),
};

async function loadDataset() {
  try {
    const graphId = dataGraphIdFromPath();
    state.graphId = graphId;
    const storageKey = tokenStorageKey(graphId);
    let token =
      tokenFromUrl(graphId) ||
      sessionStorage.getItem(storageKey) ||
      sessionStorage.getItem("dataGraphApiToken");
    const datasetUrl = graphId
      ? `/api/data-graph/${encodeURIComponent(graphId)}/artifact/latest`
      : sampleManifest.defaultSamplePath;
    if (graphId && !token) {
      showAuthPrompt("API token required", "Open this private data graph.");
      return;
    }
    let response = await fetchDataset(datasetUrl, token);
    if (graphId && response.status === 401) {
      sessionStorage.removeItem(storageKey);
      sessionStorage.removeItem("dataGraphApiToken");
      showAuthPrompt("Token rejected", "Paste the API token again.");
      return;
    }
    if (!response.ok)
      throw new Error(`Could not load dataset: ${response.status}`);
    const dataset = await response.json();
    loadRecords(dataset);
  } catch (error) {
    showLoadError(error);
  }
}

function fetchDataset(datasetUrl, token) {
  return fetch(datasetUrl, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
}

function tokenFromUrl(graphId) {
  const url = new URL(window.location.href);
  const token = url.searchParams.get("token");
  if (!token) return null;
  sessionStorage.setItem(tokenStorageKey(graphId), token);
  url.searchParams.delete("token");
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
  return token;
}

function showAuthPrompt(title, message) {
  els.viewTitle.textContent = title;
  if (els.viewSubtitle) els.viewSubtitle.textContent = message;
  if (els.topbarActions) els.topbarActions.hidden = true;
  els.mapShell.classList.add("hidden");
  els.listShell.classList.add("hidden");
  els.stats.innerHTML = "";
  els.legend.innerHTML = "";
  els.emptyState.classList.remove("hidden");
  els.details.classList.add("hidden");
  els.emptyState.innerHTML = `
    <form class="auth-panel" id="authPanel">
      <label>
        <span>API token</span>
        <input id="authTokenInput" type="password" autocomplete="off" required>
      </label>
      <button class="primary" type="submit">Open</button>
    </form>
  `;
  const form = document.querySelector("#authPanel");
  const input = document.querySelector("#authTokenInput");
  input?.focus();
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    const token = input?.value.trim();
    if (!token) return;
    sessionStorage.setItem(tokenStorageKey(state.graphId), token);
    loadDataset();
  });
}

function dataGraphIdFromPath() {
  const match = window.location.pathname.match(/^\/clusters\/([^/]+)$/);
  return match ? decodeURIComponent(match[1]) : null;
}

function loadRecords(payload) {
  const dataset = normalizeDataset(payload);
  const records = dataset.records;
  if (!Array.isArray(records)) {
    throw new Error(
      "Dataset JSON must include a top-level data array or a records array.",
    );
  }

  state.dataset = dataset;
  state.records = records.map((record, index) => ({
    ...record,
    __index: index,
  }));
  state.fields = collectFields(state.records);
  state.clusters = buildClusters(state.records);
  state.searchQuery = searchQueryFromUrl();
  state.selected = selectionFromUrl();

  if (els.topbarActions) els.topbarActions.hidden = false;
  els.mapShell.classList.toggle("hidden", state.mode !== "map");
  els.listShell.classList.toggle("hidden", state.mode !== "list");
  renderChrome();
  renderSearchControls();
  renderLegend();
  renderList();
  renderDetails();
  renderMap();
}

function showLoadError(error) {
  els.viewTitle.textContent = "Dataset could not load";
  if (els.viewSubtitle) els.viewSubtitle.textContent = error.message;
  if (els.topbarActions) els.topbarActions.hidden = true;
  els.mapShell.classList.add("hidden");
  els.listShell.classList.add("hidden");
  els.stats.innerHTML = "";
  els.legend.innerHTML = "";
  els.emptyState.classList.remove("hidden");
  els.details.classList.add("hidden");
  els.emptyState.innerHTML = `
    <span>Dataset JSON is missing or invalid</span>
    <p>Expected { config: { dataSchema, groupingFields }, data: [] }.</p>
  `;
}

function collectFields(records) {
  const fields = new Set();
  records.forEach((record) => {
    Object.keys(record).forEach((field) => {
      if (!field.startsWith("__")) fields.add(field);
    });
  });
  return [...fields].sort((a, b) => a.localeCompare(b));
}

function buildClusters(records) {
  const grouped = new Map();
  records.forEach((record) => {
    const id = String(record.__clusterId ?? record.clusterId ?? "unknown");
    if (!grouped.has(id)) grouped.set(id, []);
    grouped.get(id).push(record);
  });

  const clusters = [...grouped.entries()]
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
    .map(([id, items], index) => ({
      id,
      name:
        items[0]?.__clusterLabel || items[0]?.clusterLabel || `Cluster ${id}`,
      items,
      color: id === "-1" ? "#767b81" : palette[index % palette.length],
    }));
  const nameCounts = clusters.reduce((counts, cluster) => {
    counts.set(cluster.name, (counts.get(cluster.name) || 0) + 1);
    return counts;
  }, new Map());
  return clusters.map((cluster) => ({
    ...cluster,
    name:
      nameCounts.get(cluster.name) > 1
        ? `${cluster.name} ${cluster.id}`
        : cluster.name,
  }));
}

function renderChrome() {
  const visibleCount = visibleRecords().length;
  els.viewTitle.textContent = state.dataset.name || "Data Atlas";
  els.stats.innerHTML = `
    <div class="stat"><strong>${state.records.length}</strong><span>Records</span></div>
    ${
      state.searchQuery
        ? `<div class="stat"><strong>${visibleCount}</strong><span>Found</span></div>`
        : ""
    }
    <div class="stat"><strong>${state.clusters.length}</strong><span>Clusters</span></div>
  `;
  if (els.forgetTokenButton) {
    els.forgetTokenButton.hidden = !state.graphId;
  }
}

function renderSearchControls() {
  if (els.recordSearch) {
    els.recordSearch.value = state.searchQuery;
  }
  if (els.clearSearchButton) {
    els.clearSearchButton.hidden = !state.searchQuery;
  }
}

function renderLegend() {
  els.legend.innerHTML = visibleClusters()
    .map(
      (cluster) => `
        <button class="legend-item" type="button" data-cluster="${escapeHtml(cluster.id)}" aria-pressed="${state.selected?.type === "cluster" && state.selected.value.id === cluster.id ? "true" : "false"}">
          <span class="swatch" style="background:${cluster.color}"></span>
          <span>${escapeHtml(cluster.name)}</span>
          <span>${cluster.items.length}</span>
        </button>
      `,
    )
    .join("");
}

function renderList() {
  const clusters = visibleClusters();
  if (!clusters.length) {
    els.listContent.innerHTML = `
      <section class="cluster-block">
        <div class="cluster-title">
          <h2>No records found</h2>
          <span class="cluster-subtitle">${escapeHtml(state.searchQuery)}</span>
        </div>
      </section>
    `;
    return;
  }
  els.listContent.innerHTML = clusters
    .map(
      (cluster) => `
        <section class="cluster-block">
          <div class="cluster-title">
            <h2>${escapeHtml(cluster.name)}</h2>
            <span class="cluster-subtitle">${cluster.items.length} records</span>
          </div>
          <div class="record-grid">
            ${cluster.items.map((record) => renderRecordCard(record)).join("")}
          </div>
        </section>
      `,
    )
    .join("");
}

function renderRecordCard(record) {
  return `
    <button class="record-card" type="button" data-record="${record.__index}">
      ${renderRecordThumb(record)}
      <span>
        <strong>${escapeHtml(record[state.dataset.titleField])}</strong>
        <span>${escapeHtml(record[state.dataset.detailField])}</span>
      </span>
    </button>
  `;
}

function renderRecordThumb(record) {
  const imageUrl = recordImageUrl(record);
  if (imageUrl) {
    return `<img class="thumb" src="${escapeHtml(imageUrl)}" alt="" loading="lazy">`;
  }
  return `<span class="thumb color-thumb" style="background:${clusterForRecord(record)?.color || "#d9ddd8"}"></span>`;
}

function renderDetails() {
  if (!state.selected) {
    els.emptyState.classList.remove("hidden");
    els.details.classList.add("hidden");
    els.details.innerHTML = "";
    return;
  }

  els.emptyState.classList.add("hidden");
  els.details.classList.remove("hidden");

  if (state.selected.type === "cluster") {
    const cluster = state.selected.value;
    const items = cluster.items.filter((record) =>
      recordMatchesQuery(record, state.dataset, state.searchQuery),
    );
    els.details.innerHTML = `
      <div>
        <p class="detail-meta">${escapeHtml(groupingLabel())}</p>
        <h2>${escapeHtml(cluster.name)}</h2>
        <p class="record-detail">${items.length} ${state.searchQuery ? "matching" : "grouped"} records.</p>
      </div>
      <div class="field-table">
        ${items
          .slice(0, 12)
          .map((record) => renderRecordCard(record))
          .join("")}
      </div>
    `;
    return;
  }

  const record = state.selected.value;
  const imageField = state.dataset.imageField;
  const imageUrl = recordImageUrl(record);
  els.details.innerHTML = `
    ${imageUrl ? `<img src="${escapeHtml(imageUrl)}" alt="${escapeHtml(record[state.dataset.titleField])}" loading="lazy">` : ""}
    <div>
      <p class="detail-meta">${escapeHtml(record.__clusterLabel || record.clusterLabel || "")} · ${escapeHtml(record.__groupValue || record.groupValue || "")}</p>
      <h2>${escapeHtml(record[state.dataset.titleField])}</h2>
      <p class="record-detail">${escapeHtml(record[state.dataset.detailField])}</p>
    </div>
    <div class="field-table">
      ${visibleDetailFields(imageField, imageUrl)
        .map(
          (field) => `
        <div class="field-row">
          <span>${escapeHtml(field)}</span>
          <strong>${escapeHtml(formatValue(record[field]))}</strong>
        </div>
      `,
        )
        .join("")}
    </div>
  `;
}

function renderMap() {
  state.map?.setTarget(null);

  const features = state.records.map((record) => {
    const feature = new Feature({
      geometry: new Point([record.x, record.y]),
      record,
      cluster: clusterForRecord(record),
    });
    feature.setId(record.__index);
    return feature;
  });
  const labelFeatures = state.clusters.map((cluster) => {
    const feature = new Feature({
      geometry: new Point(clusterCenter(cluster)),
      cluster,
    });
    feature.setId(`label-${cluster.id}`);
    return feature;
  });

  state.source = new VectorSource({ features });
  state.layer = new VectorLayer({
    source: state.source,
    style: styleFeature,
  });
  state.labelSource = new VectorSource({ features: labelFeatures });
  state.labelLayer = new VectorLayer({
    source: state.labelSource,
    style: styleGroupLabel,
  });

  state.map = new OlMap({
    target: els.map,
    layers: [state.layer, state.labelLayer],
    view: new View({
      center: [0, 0],
      zoom: 12,
      minZoom: 8,
      maxZoom: 18,
    }),
    controls: [],
  });

  state.map.on("click", (event) => {
    const feature = state.map.forEachFeatureAtPixel(
      event.pixel,
      (candidate) => candidate,
    );
    if (!feature) return;
    const cluster = feature.get("cluster");
    if (cluster && !feature.get("record")) {
      setSelection({ type: "cluster", value: cluster });
      return;
    }
    setSelection({ type: "record", value: feature.get("record") });
  });

  state.map.getView().on("change:resolution", () => {
    state.layer.changed();
    state.labelLayer.changed();
  });

  if (state.selected) {
    focusSelection(false);
  } else {
    fitMap();
  }
}

function visibleRecords() {
  return state.records.filter((record) =>
    recordMatchesQuery(record, state.dataset, state.searchQuery),
  );
}

function visibleClusters() {
  return state.clusters
    .map((cluster) => ({
      ...cluster,
      items: cluster.items.filter((record) =>
        recordMatchesQuery(record, state.dataset, state.searchQuery),
      ),
    }))
    .filter((cluster) => cluster.items.length);
}

function firstVisibleRecord() {
  return visibleRecords()[0] || null;
}

function styleFeature(feature) {
  const record = feature.get("record");
  if (!recordMatchesQuery(record, state.dataset, state.searchQuery)) return undefined;
  const cluster = feature.get("cluster");
  const selected =
    state.selected?.type === "record" &&
    state.selected.value.__index === record.__index;
  const zoom = state.map?.getView().getZoom() || 12;
  const showLabel = zoom >= 16 || selected;

  return new Style({
    image: new CircleStyle({
      radius: selected ? 8 : 5,
      fill: new Fill({ color: cluster?.color || "#767b81" }),
      stroke: new Stroke({ color: "#ffffff", width: selected ? 3 : 1.5 }),
    }),
    text: showLabel
      ? new Text({
          text: truncate(String(record[state.dataset.titleField] || ""), 18),
          offsetY: 15,
          font: "12px Inter, sans-serif",
          fill: new Fill({ color: "#1f2528" }),
          stroke: new Stroke({ color: "rgba(255,255,255,0.85)", width: 3 }),
        })
      : undefined,
  });
}

function styleGroupLabel(feature) {
  const cluster = feature.get("cluster");
  if (
    !cluster.items.some((record) =>
      recordMatchesQuery(record, state.dataset, state.searchQuery),
    )
  ) {
    return undefined;
  }
  const selected =
    state.selected?.type === "cluster" &&
    state.selected.value.id === cluster.id;
  const zoom = state.map?.getView().getZoom() || 12;
  if (zoom >= 16 && !selected) return undefined;

  const size = selected ? 18 : 15;
  const color = selected ? "#1f2528" : cluster.color;
  return new Style({
    text: new Text({
      text: truncate(cluster.name, 28),
      font: `700 ${size}px Inter, sans-serif`,
      fill: new Fill({ color }),
      stroke: new Stroke({ color: "rgba(255,255,255,0.94)", width: 5 }),
      backgroundFill: new Fill({ color: "rgba(255,255,255,0.68)" }),
      backgroundStroke: new Stroke({ color: "rgba(31,37,40,0.12)", width: 1 }),
      padding: [5, 8, 5, 8],
      offsetY: -18,
    }),
  });
}

function fitMap(cluster) {
  const records = (cluster?.items || state.records).filter((record) =>
    recordMatchesQuery(record, state.dataset, state.searchQuery),
  );
  if (!records.length) return;
  const extent = boundingExtent(records.map((record) => [record.x, record.y]));
  state.map.getView().fit(extent, {
    padding: [80, 80, 80, 80],
    duration: 280,
    maxZoom: cluster ? 16 : 13,
  });
}

function clusterCenter(cluster) {
  if (!cluster.items.length) return [0, 0];
  const totals = cluster.items.reduce(
    (sum, record) => [sum[0] + record.x, sum[1] + record.y],
    [0, 0],
  );
  return [totals[0] / cluster.items.length, totals[1] / cluster.items.length];
}

function clusterForRecord(record) {
  return state.clusters.find(
    (cluster) => cluster.id === String(record.__clusterId ?? record.clusterId),
  );
}

function recordImageUrl(record) {
  return recordImageUrlForField(
    record,
    state.dataset?.imageField,
    window.location.href,
  );
}

function visibleDetailFields(imageField, imageUrl) {
  const hiddenFields = new Set([
    "clusterId",
    "clusterLabel",
    "groupValue",
    "x",
    "y",
    state.dataset.titleField,
    state.dataset.detailField,
  ]);
  return state.fields.filter((field) => {
    if (hiddenFields.has(field)) return false;
    return field !== imageField || !imageUrl;
  });
}

function normalizeDataset(payload) {
  if (payload?.config?.dataSchema) return normalizeConfiguredDataset(payload);
  return normalizeLegacyDataset(payload);
}

function normalizeConfiguredDataset(payload) {
  const config = payload.config;
  const records = Array.isArray(payload.data) ? payload.data : [];
  const schemaFields = Object.keys(config.dataSchema || {});
  const groupingFields = normalizeGroupingFields(
    config.groupingFields || config.groupingField,
    schemaFields,
  );
  const titleField =
    config.titleField ||
    firstExistingField(schemaFields, [
      "bookName",
      "title",
      "name",
      "category",
      "id",
    ]) ||
    schemaFields[0];
  const detailField =
    config.detailField ||
    firstExistingField(schemaFields, ["summary", "description", "detail"]) ||
    titleField;

  return {
    name: config.name || payload.name || "Data Atlas",
    description: config.description || payload.description || "",
    source: config.source || payload.source || "",
    dataSchema: config.dataSchema,
    groupingFields,
    titleField,
    detailField,
    recordIdField: config.recordIdField || payload.recordIdField,
    imageField: config.imageField || payload.imageField,
    records: ensureLayout(records, groupingFields),
  };
}

function normalizeLegacyDataset(payload) {
  const records = Array.isArray(payload.records) ? payload.records : [];
  const groupingFields = normalizeGroupingFields(
    payload.groupingFields || payload.groupingField,
    Object.keys(records[0] || {}),
  );
  return {
    ...payload,
    groupingFields,
    titleField:
      payload.titleField ||
      firstExistingField(Object.keys(records[0] || {}), [
        "title",
        "name",
        "category",
        "id",
      ]),
    detailField:
      payload.detailField ||
      firstExistingField(Object.keys(records[0] || {}), [
        "summary",
        "description",
      ]),
    imageField: payload.imageField,
    records: ensureLayout(records, groupingFields),
  };
}

function normalizeGroupingFields(value, fields) {
  const requested = Array.isArray(value) ? value : value ? [value] : [];
  const valid = requested.filter((field) => fields.includes(field));
  return valid.length ? valid : fields.slice(0, 1);
}

function firstExistingField(fields, candidates) {
  return candidates.find((field) => fields.includes(field));
}

function ensureLayout(records, groupingFields) {
  if (
    records.every(
      (record) => isFiniteNumber(record.x) && isFiniteNumber(record.y),
    )
  ) {
    return records.map((record) => decorateRecord(record, groupingFields));
  }

  const grouped = new Map();
  records.forEach((record) => {
    const groupValue = groupValueForRecord(record, groupingFields);
    if (!grouped.has(groupValue)) grouped.set(groupValue, []);
    grouped.get(groupValue).push(record);
  });

  const groups = [...grouped.entries()];
  const groupCount = Math.max(groups.length, 1);
  return groups.flatMap(([groupValue, items], groupIndex) => {
    const centerAngle = (Math.PI * 2 * groupIndex) / groupCount;
    const centerRadius = groupCount === 1 ? 0 : 1200;
    const centerX = Math.cos(centerAngle) * centerRadius;
    const centerY = Math.sin(centerAngle) * centerRadius;

    return items.map((record, itemIndex) => {
      const itemAngle = itemIndex * 2.399963229728653;
      const itemRadius = 70 + Math.sqrt(itemIndex) * 52;
      return decorateRecord(
        {
          ...record,
          x: centerX + Math.cos(itemAngle) * itemRadius,
          y: centerY + Math.sin(itemAngle) * itemRadius,
        },
        groupingFields,
        groupIndex,
        groupValue,
      );
    });
  });
}

function decorateRecord(record, groupingFields, clusterId, groupValue) {
  const resolvedGroupValue =
    groupValue ?? groupValueForRecord(record, groupingFields);
  return {
    ...record,
    __clusterId: clusterId ?? record.clusterId ?? resolvedGroupValue,
    __clusterLabel: resolvedGroupValue,
    __groupValue: resolvedGroupValue,
  };
}

function groupValueForRecord(record, groupingFields) {
  if (groupingFields.length === 1) {
    return formatValue(record[groupingFields[0]]) || "Unknown";
  }
  return groupingFields
    .map((field) => `${field}: ${formatValue(record[field]) || "Unknown"}`)
    .join(" / ");
}

function groupingLabel() {
  const fields = state.dataset.groupingFields || [];
  return fields.length ? `Grouped by ${fields.join(", ")}` : "Grouped records";
}

function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function setMode(mode) {
  state.mode = mode;
  els.mapShell.classList.toggle("hidden", mode !== "map");
  els.listShell.classList.toggle("hidden", mode !== "list");
  els.viewToggleButton.textContent = mode === "map" ? "List" : "Map";
  if (mode === "map") {
    requestAnimationFrame(() => {
      state.map.updateSize();
      fitMap();
    });
  }
}

function selectionFromUrl() {
  return selectionFromSearch(
    window.location.search,
    state.records,
    state.clusters,
    state.dataset,
  );
}

function setSelection(selection, { updateUrl = true, focus = true } = {}) {
  state.selected = selection;
  if (updateUrl) syncSelectionToUrl();
  renderSelection(focus);
}

function renderSelection(focus = true) {
  renderLegend();
  renderDetails();
  state.layer?.changed();
  state.labelLayer?.changed();
  if (focus) focusSelection();
}

function focusSelection(animate = true) {
  if (!state.map || !state.selected) return;
  if (state.selected.type === "cluster") {
    fitMap(state.selected.value);
    return;
  }
  const record = state.selected.value;
  const view = state.map.getView();
  if (animate) {
    view.animate({ center: [record.x, record.y], zoom: 16, duration: 260 });
  } else {
    view.setCenter([record.x, record.y]);
    view.setZoom(16);
  }
}

function syncSelectionToUrl() {
  const url = new URL(window.location.href);
  url.search = selectionSearchParams(
    url.search,
    state.selected,
    state.dataset,
  ).toString();
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
}

function restoreSelectionFromUrl() {
  state.searchQuery = searchQueryFromUrl();
  renderSearchControls();
  state.selected = selectionFromUrl();
  renderChrome();
  renderList();
  renderSelection();
}

function searchQueryFromUrl() {
  return new URLSearchParams(window.location.search).get("q") || "";
}

function setSearchQuery(query, { updateUrl = true } = {}) {
  state.searchQuery = String(query || "").trim();
  if (updateUrl) syncSearchToUrl();
  if (
    state.selected?.type === "record" &&
    !recordMatchesQuery(state.selected.value, state.dataset, state.searchQuery)
  ) {
    state.selected = null;
    syncSelectionToUrl();
  }
  renderChrome();
  renderSearchControls();
  renderLegend();
  renderList();
  renderDetails();
  state.layer?.changed();
  state.labelLayer?.changed();
}

function syncSearchToUrl() {
  const url = new URL(window.location.href);
  if (state.searchQuery) {
    url.searchParams.set("q", state.searchQuery);
  } else {
    url.searchParams.delete("q");
  }
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
}

function selectFirstSearchMatch() {
  const record = firstVisibleRecord();
  if (record) {
    setSelection({ type: "record", value: record }, { focus: state.mode === "map" });
  }
}

function initInspectorResize() {
  const savedWidth = savedInspectorWidth(
    localStorage.getItem(INSPECTOR_WIDTH_KEY),
  );
  if (savedWidth !== null) {
    setInspectorWidth(savedWidth);
  }
  if (!els.inspector || !els.inspectorResizer) return;

  let startX = 0;
  let startWidth = 0;

  els.inspectorResizer.addEventListener("pointerdown", (event) => {
    if (window.matchMedia("(max-width: 1120px)").matches) return;
    startX = event.clientX;
    startWidth = els.inspector.getBoundingClientRect().width;
    document.body.classList.add("is-resizing-inspector");
    els.inspectorResizer.setPointerCapture(event.pointerId);
  });

  els.inspectorResizer.addEventListener("pointermove", (event) => {
    if (!document.body.classList.contains("is-resizing-inspector")) return;
    const nextWidth = startWidth + startX - event.clientX;
    setInspectorWidth(nextWidth);
  });

  els.inspectorResizer.addEventListener("pointerup", (event) => {
    if (!document.body.classList.contains("is-resizing-inspector")) return;
    document.body.classList.remove("is-resizing-inspector");
    els.inspectorResizer.releasePointerCapture(event.pointerId);
    const width = Math.round(els.inspector.getBoundingClientRect().width);
    localStorage.setItem(INSPECTOR_WIDTH_KEY, String(width));
    state.map?.updateSize();
  });
}

function setInspectorWidth(width) {
  const nextWidth = boundedInspectorWidth(width, window.innerWidth);
  document.documentElement.style.setProperty(
    "--inspector-width",
    `${Math.round(nextWidth)}px`,
  );
  state.map?.updateSize();
}

function formatValue(value) {
  if (Array.isArray(value)) return value.join(", ");
  if (value && typeof value === "object") return JSON.stringify(value);
  if (value === undefined || value === null) return "";
  return String(value);
}

function truncate(value, length) {
  const text = String(value || "");
  return text.length > length ? `${text.slice(0, length - 1)}...` : text;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

els.viewToggleButton.addEventListener("click", () => setMode("list"));
els.mapModeButton.addEventListener("click", () => setMode("map"));
els.zoomInButton.addEventListener("click", () => {
  const view = state.map.getView();
  view.animate({ zoom: view.getZoom() + 1, duration: 180 });
});
els.zoomOutButton.addEventListener("click", () => {
  const view = state.map.getView();
  view.animate({ zoom: view.getZoom() - 1, duration: 180 });
});

els.recordSearch?.addEventListener("input", (event) => {
  setSearchQuery(event.target.value);
});

els.recordSearch?.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  selectFirstSearchMatch();
});

els.clearSearchButton?.addEventListener("click", () => {
  setSearchQuery("");
  els.recordSearch?.focus();
});

els.forgetTokenButton?.addEventListener("click", () => {
  sessionStorage.removeItem(tokenStorageKey(state.graphId));
  sessionStorage.removeItem("dataGraphApiToken");
  loadDataset();
});

els.legend.addEventListener("click", (event) => {
  const button = event.target.closest("[data-cluster]");
  if (!button) return;
  const cluster = state.clusters.find(
    (entry) => entry.id === button.dataset.cluster,
  );
  if (!cluster) return;
  setSelection({ type: "cluster", value: cluster });
});

document.addEventListener("click", (event) => {
  const recordButton = event.target.closest("[data-record]");
  if (!recordButton) return;
  const record = state.records.find(
    (entry) => String(entry.__index) === recordButton.dataset.record,
  );
  if (!record) return;
  setSelection({ type: "record", value: record }, { focus: state.mode === "map" });
});

window.addEventListener("popstate", restoreSelectionFromUrl);

initInspectorResize();
loadDataset();
