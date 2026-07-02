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
  capLegendClusters,
  classifyLongValue,
  humanizeFieldLabel,
  inferFieldGroups,
  parseTokenList,
  recordImageUrlForField,
  recordIdentity,
  recordMatchesQuery,
  savedInspectorWidth,
  selectionFromSearch,
  selectionSearchParams,
  tokenStorageKey,
} from "./uiState.js";
import sampleManifest from "../sample-manifest.json";

const LEGEND_VISIBLE_LIMIT = 12;
const CLUSTER_BROWSER_LIMIT = 120;
const FIELD_TOKEN_LIMIT = 8;
const LONG_TEXT_LINES = 4;
const LIST_RECORD_LIMIT_PER_CLUSTER = 8;

const state = {
  dataset: null,
  records: [],
  fields: [],
  clusters: [],
  selected: null,
  hovered: null,
  clusterBrowserOpen: false,
  clusterSearchQuery: "",
  expandedFields: new Set(),
  filters: {
    issueType: "",
    refund: "",
    cancellation: "",
    selectedClusterOnly: false,
  },
  colorMode: "cluster",
  visibleRecordCount: 0,
  visibleClusterCount: 0,
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
  fitAllButton: document.querySelector("#fitAllButton"),
  clearSelectionButton: document.querySelector("#clearSelectionButton"),
  zoomInButton: document.querySelector("#zoomInButton"),
  zoomOutButton: document.querySelector("#zoomOutButton"),
  recordSearch: document.querySelector("#recordSearch"),
  clearSearchButton: document.querySelector("#clearSearchButton"),
  forgetTokenButton: document.querySelector("#forgetTokenButton"),
  filterbar: document.querySelector("#filterbar"),
  issueTypeFilter: document.querySelector("#issueTypeFilter"),
  refundFilter: document.querySelector("#refundFilter"),
  cancellationFilter: document.querySelector("#cancellationFilter"),
  selectedClusterOnly: document.querySelector("#selectedClusterOnly"),
  colorModeSelect: document.querySelector("#colorModeSelect"),
  clusterBrowser: document.querySelector("#clusterBrowser"),
  hoverPreview: document.querySelector("#hoverPreview"),
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
  state.hovered = null;
  state.clusterBrowserOpen = false;
  state.clusterSearchQuery = "";
  state.expandedFields = new Set();

  if (els.topbarActions) els.topbarActions.hidden = false;
  els.mapShell.classList.toggle("hidden", state.mode !== "map");
  els.listShell.classList.toggle("hidden", state.mode !== "list");
  renderChrome();
  renderSearchControls();
  renderFilters();
  renderLegend();
  renderClusterBrowser();
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
  const visibleClusterCount = visibleClusters().length;
  state.visibleRecordCount = visibleCount;
  state.visibleClusterCount = visibleClusterCount;
  const selectedClusterCount =
    state.selected?.type === "cluster"
      ? state.selected.value.items.filter(recordPassesSearchAndFilters).length
      : null;
  els.viewTitle.textContent = state.dataset.name || "Data Atlas";
  els.stats.innerHTML = `
    <div class="stat"><strong>${state.records.length}</strong><span>Records</span></div>
    ${
      state.searchQuery || hasActiveFilters()
        ? `<div class="stat"><strong>${visibleCount}</strong><span>Visible</span></div>`
        : ""
    }
    <div class="stat"><strong>${visibleClusterCount}</strong><span>${visibleClusterCount === state.clusters.length ? "Clusters" : "Visible clusters"}</span></div>
    ${
      selectedClusterCount !== null
        ? `<div class="stat"><strong>${selectedClusterCount}</strong><span>Selected</span></div>`
        : ""
    }
  `;
  if (els.forgetTokenButton) {
    els.forgetTokenButton.hidden = !state.graphId;
  }
  if (els.clearSelectionButton) {
    els.clearSelectionButton.hidden = !state.selected;
  }
}

function renderSearchControls() {
  if (els.recordSearch) {
    els.recordSearch.value = state.searchQuery;
  }
  if (els.clearSearchButton) {
    els.clearSearchButton.hidden = !state.searchQuery && !hasActiveFilters();
  }
}

function renderFilters() {
  if (!els.filterbar) return;
  const issueValues = fieldValueCounts(issueTypeField());
  const selectedIssue = state.filters.issueType;
  if (els.issueTypeFilter) {
    els.issueTypeFilter.innerHTML = [
      `<option value="">All issues</option>`,
      ...issueValues.map(
        ([value, count]) =>
          `<option value="${escapeHtml(value)}">${escapeHtml(value)} (${count})</option>`,
      ),
    ].join("");
    els.issueTypeFilter.value = selectedIssue;
    els.issueTypeFilter.disabled = !issueValues.length;
  }
  if (els.refundFilter) els.refundFilter.value = state.filters.refund;
  if (els.cancellationFilter)
    els.cancellationFilter.value = state.filters.cancellation;
  if (els.selectedClusterOnly) {
    els.selectedClusterOnly.checked = state.filters.selectedClusterOnly;
    els.selectedClusterOnly.disabled = state.selected?.type !== "cluster";
  }
  if (els.colorModeSelect) els.colorModeSelect.value = state.colorMode;
}

function renderLegend() {
  const capped = capLegendClusters(visibleClusters(), {
    maxVisible: LEGEND_VISIBLE_LIMIT,
  });
  els.legend.classList.toggle("is-overflowing", capped.hasOverflow);
  els.legend.innerHTML = [
    ...capped.visibleClusters.map(
      (cluster) => `
        <button class="legend-item ${state.selected?.type === "cluster" && state.selected.value.id === cluster.id ? "is-selected-cluster" : ""}" type="button" data-cluster="${escapeHtml(cluster.id)}" aria-pressed="${state.selected?.type === "cluster" && state.selected.value.id === cluster.id ? "true" : "false"}">
          <span class="swatch" style="background:${cluster.color}"></span>
          <span>${escapeHtml(cluster.name)}</span>
          <span>${cluster.items.length}</span>
        </button>
      `,
    ),
    capped.hasOverflow
      ? `<button class="legend-item legend-overflow" type="button" data-cluster-browser="toggle" aria-expanded="${state.clusterBrowserOpen ? "true" : "false"}">${escapeHtml(capped.overflowLabel)}</button>`
      : "",
  ].join("");
}

function renderClusterBrowser() {
  if (!els.clusterBrowser) return;
  if (!state.clusterBrowserOpen) {
    els.clusterBrowser.classList.add("hidden");
    els.clusterBrowser.innerHTML = "";
    return;
  }
  const query = state.clusterSearchQuery.trim().toLocaleLowerCase();
  const clusters = visibleClusters().filter((cluster) => {
    if (!query) return true;
    return (
      cluster.name.toLocaleLowerCase().includes(query) ||
      cluster.id.toLocaleLowerCase().includes(query)
    );
  });
  const renderedClusters = clusters.slice(0, CLUSTER_BROWSER_LIMIT);
  els.clusterBrowser.classList.remove("hidden");
  els.clusterBrowser.innerHTML = `
    <button class="cluster-browser-backdrop" type="button" data-cluster-browser="close" aria-label="Close cluster browser"></button>
    <div class="cluster-browser-panel">
      <header class="cluster-browser-head">
        <div>
          <strong>All clusters</strong>
          <span>${clusters.length} visible</span>
        </div>
        <button type="button" class="icon-button" data-cluster-browser="close" aria-label="Close cluster browser">×</button>
      </header>
      <div class="cluster-browser-content">
        <div class="cluster-browser-toolbar">
          <label class="cluster-search">
            <span>Find cluster</span>
            <input id="clusterSearchInput" type="search" value="${escapeHtml(state.clusterSearchQuery)}" placeholder="Cluster name or id" autocomplete="off">
          </label>
        </div>
        <div class="cluster-browser-list">
          ${
            renderedClusters.length
              ? renderedClusters
                  .map(
                    (cluster) => `
                      <button class="cluster-browser-row ${state.selected?.type === "cluster" && state.selected.value.id === cluster.id ? "is-selected-cluster" : ""}" type="button" data-cluster="${escapeHtml(cluster.id)}" aria-pressed="${state.selected?.type === "cluster" && state.selected.value.id === cluster.id ? "true" : "false"}">
                        <span class="swatch" style="background:${cluster.color}"></span>
                        <span>${escapeHtml(cluster.name)}</span>
                        <strong>${cluster.items.length}</strong>
                      </button>
                    `,
                  )
                  .join("")
              : `<div class="cluster-browser-empty">No matching clusters</div>`
          }
        </div>
        ${
          clusters.length > renderedClusters.length
            ? `<p class="cluster-browser-note">Showing first ${renderedClusters.length} matches. Refine search to narrow the list.</p>`
            : ""
        }
      </div>
    </div>
  `;
}

function renderList() {
  if (state.mode !== "list") {
    els.listContent.innerHTML = "";
    return;
  }
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
            ${cluster.items
              .slice(0, LIST_RECORD_LIMIT_PER_CLUSTER)
              .map((record) => renderRecordCard(record))
              .join("")}
          </div>
          ${
            cluster.items.length > LIST_RECORD_LIMIT_PER_CLUSTER
              ? `<button class="cluster-more-row" type="button" data-cluster="${escapeHtml(cluster.id)}">Showing ${LIST_RECORD_LIMIT_PER_CLUSTER} of ${cluster.items.length}. Select cluster to inspect all.</button>`
              : ""
          }
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
    const items = cluster.items.filter(recordPassesSearchAndFilters);
    els.details.innerHTML = `
      <div>
        <p class="detail-meta">${escapeHtml(groupingLabel())}</p>
        <h2>${escapeHtml(cluster.name)}</h2>
        <p class="record-detail">${items.length} ${state.searchQuery ? "matching" : "grouped"} records.</p>
        <div class="detail-actions">
          <button type="button" data-filter-selected-cluster="true">Only this cluster</button>
          <button type="button" data-fit-selected="true">Fit cluster</button>
          <button type="button" data-clear-selection="true">Clear</button>
        </div>
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
  const recordId = recordIdentity(record, state.dataset);
  const clusterName = record.__clusterLabel || record.clusterLabel || "";
  const groupValue = record.__groupValue || record.groupValue || "";
  els.details.innerHTML = `
    ${imageUrl ? `<img src="${escapeHtml(imageUrl)}" alt="${escapeHtml(record[state.dataset.titleField])}" loading="lazy">` : ""}
    <div>
      <p class="detail-meta">All clusters · ${escapeHtml(clusterName)}${groupValue ? ` · ${escapeHtml(groupValue)}` : ""}</p>
      <h2>${escapeHtml(record[state.dataset.titleField])}</h2>
      <p class="record-detail">${escapeHtml(record[state.dataset.detailField])}</p>
      <div class="detail-actions">
        ${
          recordId
            ? `<button type="button" data-copy-value="${escapeHtml(recordId)}">Copy ID</button>`
            : ""
        }
        <button type="button" data-fit-selected="true">Fit record</button>
        <button type="button" data-clear-selection="true">Clear</button>
      </div>
    </div>
    ${renderGroupedFields(record, imageField, imageUrl)}
  `;
}

function renderGroupedFields(record, imageField, imageUrl) {
  const fields = visibleDetailFields(imageField, imageUrl);
  const groups = inferFieldGroups(fields);
  const groupOrder = [
    ["customer", "Customer"],
    ["ticket", "Ticket"],
    ["order", "Order"],
    ["other", "Details"],
    ["system", "System"],
  ];
  return `
    <div class="field-table">
      ${groupOrder
        .filter(([key]) => groups[key]?.length)
        .map(
          ([key, label]) => `
            <section class="field-group" data-field-group="${key}">
              <h3 class="field-group-title">${label}</h3>
              ${groups[key].map((field) => renderFieldRow(record, field)).join("")}
            </section>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderFieldRow(record, field) {
  const value = record[field];
  const fieldKey = `${record.__index}:${field}`;
  const expanded = state.expandedFields.has(fieldKey);
  const info = classifyLongValue(value, {
    longTextLines: LONG_TEXT_LINES,
    tokenListMaxVisible: FIELD_TOKEN_LIMIT,
  });
  const canCopy = isCopyableField(field, value);
  const rowClasses = [
    "field-row",
    expanded ? "is-expanded field-row--expanded" : "",
    info.kind === "token-list" || info.reason === "long-text" ? "field-row--compact" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return `
    <div class="${rowClasses}" style="--field-clamp-lines:${info.clampLines || LONG_TEXT_LINES}; --field-clamp-lines-expanded:99;">
      <div class="field-row-head">
        <span title="${escapeHtml(field)}">${escapeHtml(humanizeFieldLabel(field))}</span>
        ${
          canCopy
            ? `<button type="button" class="field-copy" data-copy-value="${escapeHtml(formatValue(value))}">Copy</button>`
            : ""
        }
      </div>
      ${renderFieldValue(value, info, expanded)}
      ${
        info.showMore
          ? `<button type="button" class="field-toggle" data-field-toggle="${escapeHtml(fieldKey)}">${expanded ? "View less" : "View more"}</button>`
          : ""
      }
    </div>
  `;
}

function renderFieldValue(value, info, expanded) {
  if (info.kind === "boolean") {
    return `<strong class="boolean-badge ${value ? "is-true" : "is-false"}">${value ? "Yes" : "No"}</strong>`;
  }
  if (info.kind === "token-list") {
    const parsed = parseTokenList(value, {
      maxTokens: expanded ? Number.POSITIVE_INFINITY : FIELD_TOKEN_LIMIT,
    });
    return `
      <strong class="token-chips" aria-label="${escapeHtml(formatValue(value))}">
        ${parsed.tokens
          .map(
            (token) =>
              `<span class="token-chip" title="${escapeHtml(token)}">${escapeHtml(token)}</span>`,
          )
          .join("")}
        ${
          !expanded && parsed.overflowCount
            ? `<span class="token-chip more">+${parsed.overflowCount} more</span>`
            : ""
        }
      </strong>
    `;
  }
  return `<strong>${escapeHtml(info.text ?? formatValue(value))}</strong>`;
}

function isCopyableField(field, value) {
  if (value === undefined || value === null || value === "") return false;
  const normalized = normalizeFieldName(field);
  return (
    normalized.includes("email") ||
    normalized.includes("id") ||
    normalized.includes("sku") ||
    normalized.includes("url") ||
    normalized.includes("source")
  );
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

  state.map.on("pointermove", (event) => {
    const feature = state.map.forEachFeatureAtPixel(
      event.pixel,
      (candidate) => candidate,
    );
    if (!feature) {
      setHovered(null);
      return;
    }
    const record = feature.get("record");
    const cluster = feature.get("cluster");
    setHovered(
      record
        ? { type: "record", value: record, pixel: event.pixel }
        : { type: "cluster", value: cluster, pixel: event.pixel },
    );
  });

  els.map.onmouseleave = () => setHovered(null);

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
  return state.records.filter(recordPassesSearchAndFilters);
}

function visibleClusters() {
  return state.clusters
    .map((cluster) => ({
      ...cluster,
      items: cluster.items.filter(recordPassesSearchAndFilters),
    }))
    .filter((cluster) => cluster.items.length);
}

function firstVisibleRecord() {
  return visibleRecords()[0] || null;
}

function recordPassesSearchAndFilters(record) {
  if (!recordMatchesQuery(record, state.dataset, state.searchQuery)) return false;
  if (
    state.filters.selectedClusterOnly &&
    state.selected?.type === "cluster" &&
    String(record.__clusterId ?? record.clusterId) !== state.selected.value.id
  ) {
    return false;
  }
  if (state.filters.issueType) {
    const field = issueTypeField();
    if (!field || String(record[field] ?? "") !== state.filters.issueType) return false;
  }
  if (state.filters.refund) {
    const field = refundField();
    if (!field || String(booleanFieldValue(record[field])) !== state.filters.refund)
      return false;
  }
  if (state.filters.cancellation) {
    const field = cancellationField();
    if (
      !field ||
      String(booleanFieldValue(record[field])) !== state.filters.cancellation
    ) {
      return false;
    }
  }
  return true;
}

function hasActiveFilters() {
  return Boolean(
    state.filters.issueType ||
      state.filters.refund ||
      state.filters.cancellation ||
      state.filters.selectedClusterOnly,
  );
}

function fieldValueCounts(field) {
  if (!field) return [];
  const counts = new Map();
  for (const record of state.records) {
    const value = record[field];
    if (value === undefined || value === null || value === "") continue;
    const text = String(value);
    counts.set(text, (counts.get(text) || 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function issueTypeField() {
  return firstMatchingField([
    "issueType",
    "issuetype",
    "issue_type",
    "category",
    "type",
  ]);
}

function refundField() {
  return firstMatchingField(["hasRefund", "hasrefund", "refund"]);
}

function cancellationField() {
  return firstMatchingField([
    "hasCancellation",
    "hascancellation",
    "cancellation",
    "cancelled",
    "canceled",
  ]);
}

function firstMatchingField(candidates) {
  const normalizedCandidates = candidates.map(normalizeFieldName);
  return state.fields.find((field) =>
    normalizedCandidates.includes(normalizeFieldName(field)),
  );
}

function normalizeFieldName(field) {
  return String(field || "").replace(/[^a-z0-9]/gi, "").toLocaleLowerCase();
}

function booleanFieldValue(value) {
  if (typeof value === "boolean") return value;
  const normalized = String(value ?? "").trim().toLocaleLowerCase();
  if (["true", "1", "yes", "y"].includes(normalized)) return true;
  if (["false", "0", "no", "n", ""].includes(normalized)) return false;
  return Boolean(value);
}

function styleFeature(feature) {
  const record = feature.get("record");
  if (!recordPassesSearchAndFilters(record)) return undefined;
  const cluster = feature.get("cluster");
  const selected =
    state.selected?.type === "record" &&
    state.selected.value.__index === record.__index;
  const selectedCluster =
    state.selected?.type === "cluster" &&
    state.selected.value.id === String(record.__clusterId ?? record.clusterId);
  const dimmedByCluster =
    state.selected?.type === "cluster" && !selectedCluster;
  const hovered =
    state.hovered?.type === "record" &&
    state.hovered.value.__index === record.__index;
  const zoom = state.map?.getView().getZoom() || 12;
  const showLabel =
    selected ||
    hovered ||
    (zoom >= 17 && state.visibleRecordCount <= 250);
  const color = colorForRecord(record, cluster);
  const opacity = dimmedByCluster ? 0.28 : 1;

  return new Style({
    image: new CircleStyle({
      radius: selected || hovered ? 8 : selectedCluster ? 6 : 5,
      fill: new Fill({ color: withAlpha(color, opacity) }),
      stroke: new Stroke({
        color: selected || hovered ? "#1f2528" : "#ffffff",
        width: selected || hovered || selectedCluster ? 3 : 1.5,
      }),
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
    !cluster.items.some(recordPassesSearchAndFilters)
  ) {
    return undefined;
  }
  const selected =
    state.selected?.type === "cluster" &&
    state.selected.value.id === cluster.id;
  const zoom = state.map?.getView().getZoom() || 12;
  if (!selected && state.visibleClusterCount > 20) return undefined;
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

function colorForRecord(record, cluster = clusterForRecord(record)) {
  if (state.colorMode === "issueType") {
    const field = issueTypeField();
    if (field) return paletteColor(String(record[field] ?? "unknown"));
  }
  if (state.colorMode === "outcome") {
    if (refundField() && booleanFieldValue(record[refundField()])) return "#2f8b72";
    if (cancellationField() && booleanFieldValue(record[cancellationField()]))
      return "#a34848";
    return "#5d6b73";
  }
  if (state.colorMode === "messageCount") {
    const field = firstMatchingField([
      "messageCount",
      "messagecount",
      "inboundMessageCount",
      "inboundmessagecount",
    ]);
    const count = Number(record[field] || 0);
    if (count >= 8) return "#a34848";
    if (count >= 4) return "#d39b2a";
    if (count >= 2) return "#40798c";
    return "#6a8f3f";
  }
  return cluster?.color || "#767b81";
}

function paletteColor(value) {
  let hash = 0;
  for (const char of value) {
    hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  }
  return palette[hash % palette.length];
}

function withAlpha(hex, alpha) {
  const normalized = String(hex || "").replace("#", "");
  if (normalized.length !== 6) return hex;
  const value = Number.parseInt(normalized, 16);
  const red = (value >> 16) & 255;
  const green = (value >> 8) & 255;
  const blue = value & 255;
  return `rgba(${red},${green},${blue},${alpha})`;
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
  renderList();
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
  if (state.selected?.type !== "cluster") {
    state.filters.selectedClusterOnly = false;
  }
  if (updateUrl) syncSelectionToUrl();
  renderSelection(focus);
}

function renderSelection(focus = true) {
  renderChrome();
  renderFilters();
  renderLegend();
  renderClusterBrowser();
  renderList();
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

function setHovered(hovered) {
  const sameHover =
    state.hovered?.type === hovered?.type &&
    state.hovered?.value === hovered?.value &&
    String(state.hovered?.pixel) === String(hovered?.pixel);
  if (sameHover) return;
  state.hovered = hovered;
  renderHoverPreview();
  state.layer?.changed();
}

function renderHoverPreview() {
  if (!els.hoverPreview) return;
  if (!state.hovered || !state.hovered.value) {
    els.hoverPreview.classList.add("hidden");
    els.hoverPreview.innerHTML = "";
    return;
  }
  const [x, y] = state.hovered.pixel || [0, 0];
  els.hoverPreview.style.transform = `translate(${Math.round(x + 14)}px, ${Math.round(y + 14)}px)`;
  els.hoverPreview.classList.remove("hidden");
  if (state.hovered.type === "cluster") {
    const cluster = state.hovered.value;
    const count = cluster.items.filter(recordPassesSearchAndFilters).length;
    els.hoverPreview.innerHTML = `
      <h4>${escapeHtml(cluster.name)}</h4>
      <p>${count} visible records</p>
    `;
    return;
  }
  const record = state.hovered.value;
  els.hoverPreview.innerHTML = `
    <h4>${escapeHtml(record[state.dataset.titleField] || "Untitled record")}</h4>
    <p>${escapeHtml(record.__clusterLabel || record.clusterLabel || "")}${record[state.dataset.detailField] ? ` · ${escapeHtml(truncate(record[state.dataset.detailField], 90))}` : ""}</p>
  `;
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
  state.selected = selectionFromUrl();
  if (state.selected?.type !== "cluster") {
    state.filters.selectedClusterOnly = false;
  }
  renderSearchControls();
  renderChrome();
  renderFilters();
  renderLegend();
  renderClusterBrowser();
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
  renderFilters();
  renderLegend();
  renderClusterBrowser();
  renderList();
  renderDetails();
  state.layer?.changed();
  state.labelLayer?.changed();
}

function renderFilteredState() {
  if (
    state.filters.selectedClusterOnly &&
    state.selected?.type !== "cluster"
  ) {
    state.filters.selectedClusterOnly = false;
  }
  renderChrome();
  renderSearchControls();
  renderFilters();
  renderLegend();
  renderClusterBrowser();
  renderList();
  renderDetails();
  state.layer?.changed();
  state.labelLayer?.changed();
}

function clearFilters() {
  state.filters = {
    issueType: "",
    refund: "",
    cancellation: "",
    selectedClusterOnly: false,
  };
  renderFilteredState();
}

function clearSelection() {
  state.selected = null;
  state.filters.selectedClusterOnly = false;
  syncSelectionToUrl();
  renderSelection(false);
  fitMap();
}

function copyText(value) {
  if (!value) return;
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(value).catch(() => {});
  }
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
els.fitAllButton?.addEventListener("click", () => fitMap());
els.clearSelectionButton?.addEventListener("click", () => clearSelection());
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
  clearFilters();
  els.recordSearch?.focus();
});

els.forgetTokenButton?.addEventListener("click", () => {
  sessionStorage.removeItem(tokenStorageKey(state.graphId));
  sessionStorage.removeItem("dataGraphApiToken");
  loadDataset();
});

els.issueTypeFilter?.addEventListener("change", (event) => {
  state.filters.issueType = event.target.value;
  renderFilteredState();
});

els.refundFilter?.addEventListener("change", (event) => {
  state.filters.refund = event.target.value;
  renderFilteredState();
});

els.cancellationFilter?.addEventListener("change", (event) => {
  state.filters.cancellation = event.target.value;
  renderFilteredState();
});

els.selectedClusterOnly?.addEventListener("change", (event) => {
  state.filters.selectedClusterOnly = event.target.checked;
  renderFilteredState();
});

els.colorModeSelect?.addEventListener("change", (event) => {
  state.colorMode = event.target.value;
  state.layer?.changed();
});

els.legend.addEventListener("click", (event) => {
  const browserAction = event.target.closest("[data-cluster-browser]");
  if (browserAction) {
    state.clusterBrowserOpen = browserAction.dataset.clusterBrowser !== "close"
      ? !state.clusterBrowserOpen
      : false;
    renderLegend();
    renderClusterBrowser();
    document.querySelector("#clusterSearchInput")?.focus();
    return;
  }
  const button = event.target.closest("[data-cluster]");
  if (!button) return;
  const cluster = state.clusters.find(
    (entry) => entry.id === button.dataset.cluster,
  );
  if (!cluster) return;
  setSelection({ type: "cluster", value: cluster });
});

els.clusterBrowser?.addEventListener("input", (event) => {
  if (event.target.id !== "clusterSearchInput") return;
  state.clusterSearchQuery = event.target.value;
  renderClusterBrowser();
  document.querySelector("#clusterSearchInput")?.focus();
});

els.clusterBrowser?.addEventListener("click", (event) => {
  const browserAction = event.target.closest("[data-cluster-browser]");
  if (browserAction) {
    state.clusterBrowserOpen = false;
    renderLegend();
    renderClusterBrowser();
    return;
  }
  const button = event.target.closest("[data-cluster]");
  if (!button) return;
  const cluster = state.clusters.find(
    (entry) => entry.id === button.dataset.cluster,
  );
  if (!cluster) return;
  state.clusterBrowserOpen = false;
  setSelection({ type: "cluster", value: cluster });
});

document.addEventListener("click", (event) => {
  const recordButton = event.target.closest("[data-record]");
  if (recordButton) {
    const record = state.records.find(
      (entry) => String(entry.__index) === recordButton.dataset.record,
    );
    if (!record) return;
    setSelection({ type: "record", value: record }, { focus: state.mode === "map" });
    return;
  }
  const fieldToggle = event.target.closest("[data-field-toggle]");
  if (fieldToggle) {
    const key = fieldToggle.dataset.fieldToggle;
    if (state.expandedFields.has(key)) {
      state.expandedFields.delete(key);
    } else {
      state.expandedFields.add(key);
    }
    renderDetails();
    return;
  }
  const copyButton = event.target.closest("[data-copy-value]");
  if (copyButton) {
    copyText(copyButton.dataset.copyValue || "");
    return;
  }
  if (event.target.closest("[data-fit-selected]")) {
    focusSelection();
    return;
  }
  if (event.target.closest("[data-clear-selection]")) {
    clearSelection();
    return;
  }
  if (event.target.closest("[data-filter-selected-cluster]")) {
    state.filters.selectedClusterOnly = state.selected?.type === "cluster";
    renderFilteredState();
  }
});

window.addEventListener("popstate", restoreSelectionFromUrl);

initInspectorResize();
loadDataset();
