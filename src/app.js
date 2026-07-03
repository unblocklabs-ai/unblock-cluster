import "ol/ol.css";
import Feature from "ol/Feature";
import OlMap from "ol/Map";
import View from "ol/View";
import Point from "ol/geom/Point";
import VectorLayer from "ol/layer/Vector";
import VectorSource from "ol/source/Vector";
import { Circle as CircleStyle, Fill, Stroke, Style } from "ol/style";
import { boundingExtent } from "ol/extent";
import {
  buildViewState,
  clearTopicSelection,
  clusterColor,
  parseViewParams,
  representativeRecords,
  selectTopic,
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
};

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
  render();
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
    runtime.state = updateFilters(runtime.state, { query: els.search.value });
    render();
  });
  els.sourceFilter?.addEventListener("change", () => {
    runtime.state = updateFilters(runtime.state, { sourceType: els.sourceFilter.value });
    render();
  });
  els.topicFilter?.addEventListener("change", () => {
    runtime.state = updateFilters(runtime.state, { topicId: els.topicFilter.value });
    runtime.state.selectedTopicId = runtime.state.filters.topicId || null;
    render();
  });
  els.startFilter?.addEventListener("change", () => {
    runtime.state = updateFilters(runtime.state, { start: els.startFilter.value });
    render();
  });
  els.endFilter?.addEventListener("change", () => {
    runtime.state = updateFilters(runtime.state, { end: els.endFilter.value });
    render();
  });
  els.mapModeButton?.addEventListener("click", () => {
    runtime.mode = "map";
    render();
  });
  els.listModeButton?.addEventListener("click", () => {
    runtime.mode = "list";
    render();
  });
  els.clearTopicButton?.addEventListener("click", () => {
    runtime.state = clearTopicSelection(runtime.state);
    render();
  });
  els.fitButton?.addEventListener("click", fitVisible);
  if (!runtime.resizeBound && typeof window !== "undefined") {
    window.addEventListener("resize", () => scheduleMapUpdate());
    runtime.resizeBound = true;
  }
}

function render() {
  const state = runtime.state;
  if (!state) return;
  els.picker.hidden = true;
  els.mapShell.hidden = runtime.mode !== "map";
  els.listShell.hidden = runtime.mode !== "list";
  const records = visibleRecords(state);
  els.title.textContent = "Data Graph";
  els.subtitle.textContent = `${state.artifact.graphId} · ${state.artifact.viewId}`;
  els.stats.innerHTML = `
    <span>${records.length} visible records</span>
    <span>${state.topics.length} topics</span>
    <span>${state.artifact.noise.noiseCount} noise</span>
  `;
  syncControls(state);
  renderTopics(state, records);
  renderDetails(state, records);
  renderList(state, records);
  renderMap(state, records);
}

function syncControls(state) {
  els.search.value = state.filters.query;
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
      render();
    });
  });
}

function renderDetails(state, records) {
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
    ${selectedTopic.summary ? `<p>${escapeHtml(selectedTopic.summary)}</p>` : ""}
    <dl>
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
              ${recordUrl ? `<a href="${escapeAttr(recordUrl)}" rel="noreferrer" target="_blank">Open source</a>` : ""}
            </article>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderList(state, records) {
  els.listContent.innerHTML = `
    <table>
      <thead><tr><th>Topic</th><th>Record</th><th>Source</th><th>Sentiment</th><th>Text</th></tr></thead>
      <tbody>
        ${records
          .map((record) => {
            const topic = state.topicById.get(record.clusterId);
            return `
              <tr data-topic-id="${record.clusterId}">
                <td>${escapeHtml(topicLabel(topic))}</td>
                <td>${escapeHtml(record.title || record.recordId)}</td>
                <td>${escapeHtml(record.sourceType || "")}</td>
                <td>${escapeHtml(record.sentiment || "")}</td>
                <td>${escapeHtml(record.customerText || "")}</td>
              </tr>
            `;
          })
          .join("")}
      </tbody>
    </table>
  `;
}

function renderMap(state, records) {
  if (!runtime.map) {
    runtime.source = new VectorSource();
    runtime.layer = new VectorLayer({ source: runtime.source });
    runtime.map = new OlMap({
      target: els.mapCanvas,
      layers: [runtime.layer],
      view: new View({ center: [0, 0], zoom: 2 }),
    });
  }
  runtime.source.clear();
  for (const record of records) {
    const feature = new Feature({
      geometry: new Point([record.x, record.y]),
      record,
    });
    feature.setStyle(pointStyle(record, state.selectedTopicId));
    feature.on("change", () => {});
    runtime.source.addFeature(feature);
  }
  if (!runtime.mapClickBound) {
    runtime.map.on("singleclick", (event) => {
      const feature = runtime.map.forEachFeatureAtPixel(event.pixel, (item) => item);
      const record = feature?.get("record");
      if (record) {
        runtime.state = selectTopic(runtime.state, record.clusterId);
        render();
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

function pointStyle(record, selectedTopicId) {
  const lowProbability = record.clusterProbability < 0.55;
  const selected = selectedTopicId === "" || selectedTopicId === null || record.clusterId === selectedTopicId;
  const radius = record.isNoise ? 4 : Math.max(5, Math.min(12, 5 + record.outlierScore * 5));
  return new Style({
    image: new CircleStyle({
      radius,
      fill: new Fill({
        color: record.isNoise
          ? "rgba(120, 120, 120, 0.35)"
          : `${clusterColor(record.clusterId)}${lowProbability || !selected ? "88" : "dd"}`,
      }),
      stroke: new Stroke({
        color: record.isNoise ? "#4b5563" : selected ? "#111827" : "#ffffff",
        width: record.isNoise ? 2 : 1,
      }),
    }),
  });
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

function hideWorkspace() {
  els.mapShell.hidden = true;
  els.listShell.hidden = true;
  els.topicPanel.innerHTML = "";
  els.stats.innerHTML = "";
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
