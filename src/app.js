import "ol/ol.css";
import Feature from "ol/Feature";
import OlMap from "ol/Map";
import View from "ol/View";
import Point from "ol/geom/Point";
import VectorLayer from "ol/layer/Vector";
import Projection from "ol/proj/Projection";
import { addProjection } from "ol/proj";
import VectorSource from "ol/source/Vector";
import { Circle as CircleStyle, Fill, Stroke, Style } from "ol/style";
import { boundingExtent } from "ol/extent";
import {
  backToTopic,
  buildViewState,
  clearTopicSelection,
  clusterColor,
  listWindow,
  parseViewParams,
  representativeRecords,
  selectRecord,
  selectTopic,
  showMoreListRecords,
  spikeBadge,
  topicLabel,
  updateFilters,
  visibleRecords,
} from "./uiState.js";

const els = {
  title: document.querySelector("#viewTitle"),
  subtitle: document.querySelector("#viewSubtitle"),
  search: document.querySelector("#recordSearch"),
  sourceFilter: document.querySelector("#sourceTypeFilter"),
  topicFilter: document.querySelector("#topicFilter"),
  startFilter: document.querySelector("#startDate"),
  endFilter: document.querySelector("#endDate"),
  mapShell: document.querySelector("#mapShell"),
  mapCanvas: document.querySelector("#mapCanvas"),
  listShell: document.querySelector("#listShell"),
  listContent: document.querySelector("#listContent"),
  topicPanel: document.querySelector("#topicPanel"),
  details: document.querySelector("#details"),
  stats: document.querySelector("#stats"),
  emptyState: document.querySelector("#emptyState"),
  picker: document.querySelector("#picker"),
  mapModeButton: document.querySelector("#mapModeButton"),
  listModeButton: document.querySelector("#listModeButton"),
  clearTopicButton: document.querySelector("#clearTopicButton"),
  fitButton: document.querySelector("#fitAllButton"),
};

const runtime = {
  state: null,
  map: null,
  source: null,
  layer: null,
  mode: "map",
  mapClickBound: false,
  resizeBound: false,
  resizeFrame: null,
  initialFitDone: false,
  recordDetails: new Map(),
  recordDetailRequests: new Set(),
  styleCache: new Map(),
  searchDraft: null,
  searchTimer: null,
  visibleRecords: [],
  recordsSignature: "",
  listSignature: "",
};

const LAYOUT_PROJECTION = new Projection({
  code: "DATAGRAPH:LAYOUT",
  extent: [-1_000_000, -1_000_000, 1_000_000, 1_000_000],
  units: "pixels",
});
addProjection(LAYOUT_PROJECTION);

if (typeof window !== "undefined") {
  window.addEventListener("DOMContentLoaded", () => {
    start().catch((error) => renderError("Could not load Data Graph", error.message));
  });
}

async function start() {
  bindEvents();
  const params = parseViewParams(window.location.search);
  if (!params.graphId || !params.viewId) {
    await renderPicker();
    return;
  }
  runtime.mode = params.mode === "list" ? "list" : "map";
  await loadArtifact(params.graphId, params.viewId, {
    selectedTopicId: params.topicId,
    topicId: params.topicId,
  });
}

async function loadArtifact(graphId, viewId, options = {}) {
  const response = await fetch(
    `/api/graphs/${encodeURIComponent(graphId)}/views/${encodeURIComponent(viewId)}/artifact`,
  );
  if (!response.ok) {
    let detail = `Artifact request failed with ${response.status}`;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : detail;
    } catch {
      // Keep the status-based fallback.
    }
    renderError("Artifact unavailable", detail);
    return;
  }
  runtime.state = buildViewState(await response.json(), options);
  runtime.initialFitDone = false;
  runtime.recordDetails.clear();
  runtime.recordDetailRequests.clear();
  runtime.styleCache.clear();
  runtime.recordsSignature = "";
  runtime.listSignature = "";
  render({ dataChanged: true });
}

async function renderPicker() {
  const response = await fetch("/api/graphs");
  if (!response.ok) {
    renderError("No graph selected", "Add graphId and viewId query parameters.");
    return;
  }
  const graphs = (await response.json()).graphs || [];
  const graphDetails = await Promise.all(
    graphs.map(async (graph) => {
      const detail = await fetch(`/api/graphs/${encodeURIComponent(graph.id)}`);
      return detail.ok ? detail.json() : graph;
    }),
  );
  els.title.textContent = "Open Data Graph";
  els.subtitle.textContent = "Choose a graph view to inspect.";
  hideWorkspace();
  els.picker.hidden = false;
  els.picker.innerHTML = graphDetails
    .map((graph) =>
      (graph.views || [])
        .map(
          (view) => `
            <a class="picker-row" href="/?graphId=${encodeURIComponent(graph.id)}&viewId=${encodeURIComponent(view.id)}">
              <strong>${escapeHtml(graph.name)}</strong>
              <span>${escapeHtml(view.name)} · ${view.recordCount} records</span>
            </a>
          `,
        )
        .join(""),
    )
    .join("") || `<p class="muted">No graphs found.</p>`;
}

function bindEvents() {
  els.search?.addEventListener("input", () => {
    runtime.searchDraft = els.search.value;
    if (runtime.searchTimer !== null) {
      clearTimeout(runtime.searchTimer);
    }
    runtime.searchTimer = setTimeout(() => {
      runtime.searchTimer = null;
      runtime.state = updateFilters(runtime.state, { query: runtime.searchDraft || "" });
      runtime.searchDraft = null;
      render({ dataChanged: true });
    }, 200);
  });
  els.sourceFilter?.addEventListener("change", () => {
    runtime.state = updateFilters(runtime.state, { sourceType: els.sourceFilter.value });
    render({ dataChanged: true });
  });
  els.topicFilter?.addEventListener("change", () => {
    runtime.state = updateFilters(runtime.state, { topicId: els.topicFilter.value });
    runtime.state =
      els.topicFilter.value === ""
        ? clearTopicSelection(runtime.state)
        : selectTopic(runtime.state, els.topicFilter.value);
    render({ dataChanged: true });
  });
  els.startFilter?.addEventListener("change", () => {
    runtime.state = updateFilters(runtime.state, { start: els.startFilter.value });
    render({ dataChanged: true });
  });
  els.endFilter?.addEventListener("change", () => {
    runtime.state = updateFilters(runtime.state, { end: els.endFilter.value });
    render({ dataChanged: true });
  });
  els.mapModeButton?.addEventListener("click", () => {
    runtime.mode = "map";
    render({ modeChanged: true });
  });
  els.listModeButton?.addEventListener("click", () => {
    runtime.mode = "list";
    render({ modeChanged: true });
  });
  els.clearTopicButton?.addEventListener("click", () => {
    runtime.state = clearTopicSelection(runtime.state);
    render({ selectionOnly: true });
  });
  els.fitButton?.addEventListener("click", fitVisible);
  if (!runtime.resizeBound && typeof window !== "undefined") {
    window.addEventListener("resize", () => scheduleMapUpdate());
    runtime.resizeBound = true;
  }
}

function render(options = {}) {
  const { dataChanged = false, selectionOnly = false, listPageChanged = false } = options;
  const state = runtime.state;
  if (!state) return;
  els.picker.hidden = true;
  els.mapShell.hidden = runtime.mode !== "map";
  els.listShell.hidden = runtime.mode !== "list";
  const records = dataChanged ? visibleRecords(state) : runtime.visibleRecords;
  if (dataChanged) {
    runtime.visibleRecords = records;
  }
  els.title.textContent = "Data Graph";
  els.subtitle.textContent = `${state.artifact.graphId} · ${state.artifact.viewId}`;
  els.stats.innerHTML = `
    <span>${records.length} visible records</span>
    <span>${state.topics.length} topics</span>
    <span>${state.artifact.noise.noiseCount} noise</span>
  `;
  syncControls(state);
  if (selectionOnly) {
    updateTopicSelectionClasses(state);
  } else {
    renderTopics(state, records);
  }
  renderInspector(state, records);
  if (selectionOnly) {
    updateListSelectionClasses(state);
  } else if (dataChanged || listPageChanged || runtime.mode === "list") {
    renderList(state, records);
  }
  if (selectionOnly) {
    runtime.layer?.changed();
  } else {
    renderMap(state, records, { rebuildSource: dataChanged });
  }
}

function syncControls(state) {
  els.search.value = runtime.searchDraft ?? state.filters.query;
  els.startFilter.value = state.filters.start || state.timeExtent.min;
  els.endFilter.value = state.filters.end || state.timeExtent.max;
  els.startFilter.min = state.timeExtent.min;
  els.startFilter.max = state.timeExtent.max;
  els.endFilter.min = state.timeExtent.min;
  els.endFilter.max = state.timeExtent.max;
  els.sourceFilter.innerHTML = `<option value="">All sources</option>${state.sourceTypes
    .map((source) => `<option value="${escapeHtml(source)}">${escapeHtml(source)}</option>`)
    .join("")}`;
  els.sourceFilter.value = state.filters.sourceType;
  els.topicFilter.innerHTML = `<option value="">All topics</option>${state.topics
    .map(
      (topic) =>
        `<option value="${topic.clusterId}">${escapeHtml(topicLabel(topic))} (${topic.size})</option>`,
    )
    .join("")}`;
  els.topicFilter.value =
    state.filters.topicId === "" ? "" : String(state.filters.topicId);
  els.mapModeButton.setAttribute("aria-pressed", String(runtime.mode === "map"));
  els.listModeButton.setAttribute("aria-pressed", String(runtime.mode === "list"));
  els.fitButton.hidden = runtime.mode !== "map";
  els.fitButton.disabled = runtime.mode !== "map";
  const hasSelection = state.selectedTopicId !== null || state.selectedRecordId !== null;
  els.clearTopicButton.hidden = !hasSelection;
  els.clearTopicButton.disabled = !hasSelection;
}

function renderTopics(state, records) {
  els.topicPanel.innerHTML = state.topics
    .map((topic) => {
      const selected = topic.clusterId === state.selectedTopicId;
      const visibleCount = records.filter((record) => record.clusterId === topic.clusterId).length;
      const badge = spikeBadge(topic);
      return `
        <button class="topic ${selected ? "selected" : ""}" type="button" data-topic-id="${topic.clusterId}">
          <span class="topic-swatch" style="background:${clusterColor(topic.clusterId)}"></span>
          <span class="topic-main">
            <strong>${escapeHtml(topicLabel(topic))}</strong>
            <small>${visibleCount}/${topic.size} visible · mean p ${formatNumber(topic.meanProbability)}</small>
            ${topic.summary ? `<span>${escapeHtml(topic.summary)}</span>` : ""}
            ${topic.coherent === false ? `<em>Low coherence</em>` : ""}
            ${badge ? `<mark>${escapeHtml(badge.text)}</mark>` : ""}
            <span class="source-mix">${formatSourceMix(topic.sourceMix)}</span>
          </span>
        </button>
      `;
    })
    .join("");
  els.topicPanel.querySelectorAll("[data-topic-id]").forEach((button) => {
    button.addEventListener("click", () => {
      runtime.state = selectTopic(runtime.state, button.dataset.topicId);
      render({ selectionOnly: true });
    });
  });
}

function renderInspector(state, records) {
  if (state.selectedRecordId) {
    renderRecordInspector(state);
    return;
  }
  const selectedTopic = state.topicById.get(state.selectedTopicId);
  if (!selectedTopic) {
    els.emptyState.hidden = false;
    els.details.hidden = true;
    return;
  }
  const reps = representativeRecords(selectedTopic, state.artifact.data);
  els.emptyState.hidden = true;
  els.details.hidden = false;
  els.details.innerHTML = `
    <h2>${escapeHtml(topicLabel(selectedTopic))}</h2>
    ${selectedTopic.summary ? `<p class="wrap-text">${escapeHtml(selectedTopic.summary)}</p>` : ""}
    <dl>
      <dt>Coherence</dt><dd>${selectedTopic.coherent === false ? "Low" : "Normal"}</dd>
      <dt>Visible records</dt><dd>${records.filter((record) => record.clusterId === selectedTopic.clusterId).length}</dd>
      <dt>Total records</dt><dd>${selectedTopic.size}</dd>
      <dt>Source mix</dt><dd>${formatSourceMix(selectedTopic.sourceMix)}</dd>
    </dl>
    <h3>Representatives</h3>
    <div class="representatives">
      ${reps
        .map((record) => {
          const recordUrl = safeRecordUrl(record.recordUrl);
          return `
            <article>
              <strong>${escapeHtml(record.title || record.recordId)}</strong>
              <p>${escapeHtml(record.customerText || "")}</p>
              <button type="button" data-record-id="${escapeAttr(record.id)}">Inspect record</button>
              ${recordUrl ? `<a href="${escapeAttr(recordUrl)}" rel="noreferrer" target="_blank">Open source</a>` : ""}
            </article>
          `;
        })
        .join("")}
    </div>
  `;
  els.details.querySelectorAll("[data-record-id]").forEach((button) => {
    button.addEventListener("click", () => selectRecordAndRender(button.dataset.recordId));
  });
}

function renderRecordInspector(state) {
  const artifactRecord = state.recordById.get(state.selectedRecordId);
  if (!artifactRecord) {
    els.emptyState.hidden = false;
    els.details.hidden = true;
    return;
  }
  ensureRecordDetail(artifactRecord.id);
  const detailState = runtime.recordDetails.get(artifactRecord.id);
  const fullRecord = detailState?.record || artifactRecord;
  const topic = state.topicById.get(artifactRecord.clusterId);
  const loading = !detailState || detailState.status === "loading";
  const failed = detailState?.status === "error";
  const recordUrl = safeRecordUrl(fullRecord.recordUrl);
  els.emptyState.hidden = true;
  els.details.hidden = false;
  els.details.innerHTML = `
    <div class="inspector-heading">
      <button type="button" data-back-to-topic>Back to topic</button>
      <span class="topic-pill" style="--topic-color:${clusterColor(artifactRecord.clusterId)}">${escapeHtml(topicLabel(topic))}</span>
    </div>
    <h2>${escapeHtml(fullRecord.title || fullRecord.recordId || fullRecord.id)}</h2>
    ${loading ? `<p class="muted">Loading full record. Showing artifact preview for now.</p>` : ""}
    ${failed ? `<p class="error-inline">${escapeHtml(detailState.error)}</p>` : ""}
    <dl class="record-facts">
      <dt>Record ID</dt><dd>${escapeHtml(fullRecord.recordId || artifactRecord.recordId || artifactRecord.id)}</dd>
      <dt>Source</dt><dd>${escapeHtml(joinParts([fullRecord.sourceType, fullRecord.sourceName]))}</dd>
      <dt>Product</dt><dd>${escapeHtml(fullRecord.product || "")}</dd>
      <dt>SKU</dt><dd>${escapeHtml(fullRecord.sku || "")}</dd>
      <dt>Rating</dt><dd>${escapeHtml(formatOptionalNumber(fullRecord.rating))}</dd>
      <dt>Sentiment</dt><dd>${escapeHtml(fullRecord.sentiment || "")}</dd>
      <dt>Tags</dt><dd>${escapeHtml(formatTags(fullRecord.tags))}</dd>
      <dt>Timestamp</dt><dd>${escapeHtml(fullRecord.timestamp || "")}</dd>
      <dt>Probability</dt><dd>${formatNumber(artifactRecord.clusterProbability)}</dd>
      <dt>Outlier score</dt><dd>${formatNumber(artifactRecord.outlierScore)}</dd>
      ${
        recordUrl
          ? `<dt>URL</dt><dd><a href="${escapeAttr(recordUrl)}" rel="noreferrer" target="_blank">${escapeHtml(recordUrl)}</a></dd>`
          : ""
      }
    </dl>
    <section class="record-section">
      <h3>Customer Text</h3>
      <p>${escapeHtml(fullRecord.customerText || "")}</p>
    </section>
    <section class="record-section">
      <h3>Metadata</h3>
      ${formatMetadata(fullRecord.metadata)}
    </section>
  `;
  els.details
    .querySelector("[data-back-to-topic]")
    ?.addEventListener("click", () => {
      runtime.state = backToTopic(runtime.state);
      render({ selectionOnly: true });
    });
}

function renderList(state, records) {
  const windowed = listWindow(records, state);
  const signature = `${recordsSignature(records)}::${windowed.showing}`;
  if (signature === runtime.listSignature) {
    updateListSelectionClasses(state);
    return;
  }
  runtime.listSignature = signature;
  els.listContent.innerHTML = `
    <div class="list-summary">
      <span>Showing ${windowed.showing} of ${windowed.total}</span>
      ${
        windowed.remaining > 0
          ? `<button type="button" data-show-more>Show ${Math.min(windowed.remaining, 500)} more</button>`
          : ""
      }
    </div>
    <table>
      <thead><tr><th>Topic</th><th>Record</th><th>Source</th><th>Sentiment</th><th>Text</th></tr></thead>
      <tbody>
        ${windowed.records
          .map((record) => {
            const topic = state.topicById.get(record.clusterId);
            return `
              <tr class="${record.id === state.selectedRecordId ? "selected" : ""}" data-record-id="${escapeAttr(record.id)}" tabindex="0">
                <td>${escapeHtml(topicLabel(topic))}</td>
                <td>${escapeHtml(record.title || record.recordId)}</td>
                <td>${escapeHtml(record.sourceType || "")}</td>
                <td>${escapeHtml(record.sentiment || "")}</td>
                <td class="text-cell"><span>${escapeHtml(record.customerText || "")}</span></td>
              </tr>
            `;
          })
          .join("")}
      </tbody>
    </table>
  `;
  els.listContent.querySelectorAll("[data-record-id]").forEach((row) => {
    row.addEventListener("click", () => selectRecordAndRender(row.dataset.recordId));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectRecordAndRender(row.dataset.recordId);
      }
    });
  });
  els.listContent.querySelector("[data-show-more]")?.addEventListener("click", () => {
    runtime.state = showMoreListRecords(runtime.state);
    render({ listPageChanged: true });
  });
}

function renderMap(state, records, options = {}) {
  const { rebuildSource = false } = options;
  if (!runtime.map) {
    runtime.source = new VectorSource();
    runtime.layer = new VectorLayer({
      source: runtime.source,
      style: (feature) =>
        pointStyle(
          feature.get("record"),
          runtime.state?.selectedTopicId,
          runtime.state?.selectedRecordId,
        ),
    });
    runtime.map = new OlMap({
      target: els.mapCanvas,
      layers: [runtime.layer],
      view: new View({ center: [0, 0], resolution: 1, projection: LAYOUT_PROJECTION }),
    });
  }
  const signature = recordsSignature(records);
  if (rebuildSource || signature !== runtime.recordsSignature) {
    runtime.recordsSignature = signature;
    runtime.source.clear();
    runtime.source.addFeatures(
      records.map(
        (record) =>
          new Feature({
            geometry: new Point([record.x, record.y]),
            record,
          }),
      ),
    );
  }
  if (!runtime.mapClickBound) {
    runtime.map.on("singleclick", (event) => {
      const record = nearestRecordAtCoordinate(event.coordinate);
      if (record) {
        selectRecordAndRender(record.id);
      }
    });
    runtime.mapClickBound = true;
  }
  if (runtime.initialFitDone) {
    scheduleMapUpdate();
  } else {
    fitInitialView();
  }
}

function recordsSignature(records) {
  if (!records.length) return "0";
  const step = Math.max(1, Math.floor(records.length / 16));
  const sampled = [];
  for (let index = 0; index < records.length; index += step) {
    sampled.push(records[index].id);
  }
  const last = records[records.length - 1];
  if (sampled[sampled.length - 1] !== last.id) sampled.push(last.id);
  return `${records.length}:${sampled.join("|")}`;
}

function scheduleMapUpdate(options = {}) {
  const { fitOnce = false } = options;
  if (!runtime.map || els.mapShell.hidden) return;
  if (runtime.resizeFrame !== null) {
    cancelAnimationFrame(runtime.resizeFrame);
  }
  runtime.resizeFrame = requestAnimationFrame(() => {
    runtime.resizeFrame = requestAnimationFrame(() => {
      runtime.resizeFrame = null;
      runtime.map.updateSize();
      if (fitOnce) fitInitialView();
    });
  });
}

function fitInitialView() {
  if (runtime.initialFitDone || !runtime.map || els.mapShell.hidden) return;
  runtime.map.updateSize();
  if (hasMeasuredMapSize()) {
    runtime.initialFitDone = fitVisible();
    return;
  }
  scheduleMapUpdate({ fitOnce: true });
  if (!hasMeasuredMapSize()) {
    runtime.map.once("postrender", () => {
      runtime.map.updateSize();
      fitInitialView();
    });
  }
}

function hasMeasuredMapSize() {
  const size = runtime.map?.getSize();
  return Array.isArray(size) && size[0] > 0 && size[1] > 0;
}

function pointStyle(record, selectedTopicId, selectedRecordId) {
  const lowProbability = record.clusterProbability < 0.55;
  const topicSelected =
    selectedTopicId === "" || selectedTopicId === null || record.clusterId === selectedTopicId;
  const recordSelected = record.id === selectedRecordId;
  const radiusBucket = recordSelected
    ? 13
    : record.isNoise
      ? 4
      : Math.round(Math.max(5, Math.min(12, 5 + record.outlierScore * 5)) * 2) / 2;
  const key = [
    record.clusterId,
    record.isNoise ? "noise" : "member",
    lowProbability ? "low" : "high",
    topicSelected ? "active" : "muted",
    recordSelected ? "selected" : "normal",
    radiusBucket,
  ].join(":");
  const cached = runtime.styleCache.get(key);
  if (cached) return cached;
  const style = new Style({
    image: new CircleStyle({
      radius: radiusBucket,
      fill: new Fill({
        color: record.isNoise
          ? "rgba(120, 120, 120, 0.35)"
          : `${clusterColor(record.clusterId)}${lowProbability || !topicSelected ? "88" : "dd"}`,
      }),
      stroke: new Stroke({
        color: recordSelected ? "#f8fafc" : record.isNoise ? "#4b5563" : topicSelected ? "#111827" : "#ffffff",
        width: recordSelected ? 4 : record.isNoise ? 2 : 1,
      }),
    }),
  });
  runtime.styleCache.set(key, style);
  return style;
}

function fitVisible() {
  if (!runtime.map || !runtime.source || runtime.source.getFeatures().length === 0) {
    return false;
  }
  runtime.map.updateSize();
  const coordinates = runtime.source
    .getFeatures()
    .map((feature) => feature.getGeometry().getCoordinates());
  runtime.map.getView().fit(boundingExtent(coordinates), {
    padding: [48, 48, 48, 48],
    duration: 0,
  });
  return true;
}

function nearestRecordAtCoordinate(coordinate) {
  if (!runtime.map || !runtime.source) return null;
  const resolution = runtime.map.getView().getResolution() || 1;
  const tolerance = resolution * 10;
  const toleranceSquared = tolerance * tolerance;
  let nearest = null;
  let nearestDistance = Infinity;
  for (const feature of runtime.source.getFeatures()) {
    const record = feature.get("record");
    if (!record) continue;
    const dx = record.x - coordinate[0];
    const dy = record.y - coordinate[1];
    const distance = dx * dx + dy * dy;
    if (distance <= toleranceSquared && distance < nearestDistance) {
      nearest = record;
      nearestDistance = distance;
    }
  }
  return nearest;
}

function hideWorkspace() {
  els.mapShell.hidden = true;
  els.listShell.hidden = true;
  els.topicPanel.innerHTML = "";
  els.stats.innerHTML = "";
}

function selectRecordAndRender(recordId) {
  runtime.state = selectRecord(runtime.state, recordId);
  render({ selectionOnly: true });
}

async function ensureRecordDetail(recordId) {
  if (
    runtime.recordDetails.has(recordId) ||
    runtime.recordDetailRequests.has(recordId) ||
    !runtime.state
  ) {
    return;
  }
  runtime.recordDetailRequests.add(recordId);
  runtime.recordDetails.set(recordId, { status: "loading", record: null, error: null });
  try {
    const graphId = runtime.state.artifact.graphId;
    const response = await fetch(
      `/api/graphs/${encodeURIComponent(graphId)}/records/${encodeURIComponent(recordId)}`,
    );
    if (!response.ok) {
      throw new Error(`Record request failed with ${response.status}`);
    }
    runtime.recordDetails.set(recordId, {
      status: "loaded",
      record: await response.json(),
      error: null,
    });
  } catch (error) {
    runtime.recordDetails.set(recordId, {
      status: "error",
      record: null,
      error: error instanceof Error ? error.message : String(error),
    });
  } finally {
    runtime.recordDetailRequests.delete(recordId);
    if (runtime.state?.selectedRecordId === recordId) {
      render({ selectionOnly: true });
    }
  }
}

function updateTopicSelectionClasses(state) {
  els.topicPanel.querySelectorAll("[data-topic-id]").forEach((button) => {
    button.classList.toggle(
      "selected",
      Number(button.dataset.topicId) === state.selectedTopicId,
    );
  });
}

function updateListSelectionClasses(state) {
  els.listContent.querySelectorAll("[data-record-id]").forEach((row) => {
    row.classList.toggle("selected", row.dataset.recordId === state.selectedRecordId);
  });
}

function renderError(title, message) {
  els.title.textContent = title;
  els.subtitle.textContent = message;
  hideWorkspace();
  els.picker.hidden = false;
  els.picker.innerHTML = `<div class="error">${escapeHtml(message)}</div>`;
}

function formatSourceMix(sourceMix = {}) {
  return Object.entries(sourceMix)
    .map(([source, count]) => `${escapeHtml(source)} ${count}`)
    .join(" · ");
}

function formatNumber(value) {
  return Number.isFinite(value) ? value.toFixed(2) : "n/a";
}

function formatOptionalNumber(value) {
  return Number.isFinite(value) ? String(value) : "";
}

function formatTags(value) {
  return Array.isArray(value) ? value.join(", ") : "";
}

function formatMetadata(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return `<p class="muted">No metadata.</p>`;
  }
  const entries = Object.entries(value);
  if (!entries.length) return `<p class="muted">No metadata.</p>`;
  return `
    <dl class="metadata-list">
      ${entries
        .map(
          ([key, item]) =>
            `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(formatMetadataValue(item))}</dd>`,
        )
        .join("")}
    </dl>
  `;
}

function formatMetadataValue(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function joinParts(parts) {
  return parts.filter(Boolean).join(" · ");
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

function safeRecordUrl(value) {
  if (!value) return null;
  try {
    const url = new URL(value, window.location.href);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}
