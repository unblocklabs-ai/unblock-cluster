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
  applyDatePreset,
  backToTopic,
  buildViewState,
  clearTopicSelection,
  clusterColor,
  listWindow,
  parseViewParams,
  representativeRecords,
  selectRecord,
  selectTopic,
  selectedTopicExtentRecords,
  showMoreListRecords,
  spikeBadge,
  topicPanelTopics,
  topicLabel,
  trendSparklineParts,
  updateFilters,
  updateTopicPanel,
  visibleRecords,
  viewSearchParams,
} from "./uiState.js";
import { formatCount, formatCountLabel } from "./formatters.js";

const els = {
  title: document.querySelector("#viewTitle"),
  subtitle: document.querySelector("#viewSubtitle"),
  toolbar: document.querySelector(".toolbar"),
  search: document.querySelector("#recordSearch"),
  sourceFilter: document.querySelector("#sourceTypeFilter"),
  topicFilter: document.querySelector("#topicFilter"),
  topicSearch: document.querySelector("#topicSearch"),
  topicSort: document.querySelector("#topicSort"),
  startFilter: document.querySelector("#startDate"),
  endFilter: document.querySelector("#endDate"),
  workspace: document.querySelector("#workspace"),
  warningsBanner: document.querySelector("#warningsBanner"),
  mapShell: document.querySelector("#mapShell"),
  mapCanvas: document.querySelector("#mapCanvas"),
  listShell: document.querySelector("#listShell"),
  listContent: document.querySelector("#listContent"),
  topicPanel: document.querySelector("#topicPanel"),
  topicList: document.querySelector("#topicList"),
  provenance: document.querySelector("#provenance"),
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
  trends: new Map(),
  trendBucket: null,
  trendsStatus: "idle",
  facetField: "",
  facetStatus: "idle",
  facetTopics: null,
  facetError: "",
  loadToken: null,
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
    selectedRecordId: params.recordId,
  });
}

async function loadArtifact(graphId, viewId, options = {}) {
  const loadToken = Symbol("artifact-load");
  runtime.loadToken = loadToken;
  runtime.state = null;
  renderLoading("Loading records...");
  const recordCount = await fetchViewRecordCount(graphId, viewId);
  if (runtime.loadToken === loadToken && recordCount !== null) {
    renderLoading(`Loading ${formatCountLabel(recordCount, "record")}...`);
  }
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
    runtime.loadToken = null;
    renderError("Artifact unavailable", detail);
    return;
  }
  const artifact = await response.json();
  renderLoading(`Loading ${formatCountLabel(artifact.data?.length ?? 0, "record")}...`);
  runtime.state = buildViewState(artifact, options);
  if (options.selectedRecordId) {
    runtime.state = selectRecord(runtime.state, options.selectedRecordId);
  }
  runtime.initialFitDone = false;
  runtime.recordDetails.clear();
  runtime.recordDetailRequests.clear();
  runtime.styleCache.clear();
  runtime.recordsSignature = "";
  runtime.listSignature = "";
  runtime.trends = new Map();
  runtime.trendBucket = null;
  runtime.trendsStatus = "idle";
  runtime.facetField = "";
  runtime.facetStatus = "idle";
  runtime.facetTopics = null;
  runtime.facetError = "";
  delete els.warningsBanner.dataset.dismissed;
  render({ dataChanged: true });
  runtime.loadToken = null;
  if (runtime.state.selectedTopicId !== null) {
    scrollSelectedTopicIntoView();
    scheduleMapUpdate();
    requestAnimationFrame(() => fitSelectedTopic());
  }
  fetchTrendsOnce();
}

async function fetchViewRecordCount(graphId, viewId) {
  try {
    const response = await fetch(`/api/graphs/${encodeURIComponent(graphId)}`);
    if (!response.ok) return null;
    const graph = await response.json();
    const view = (graph.views || []).find((item) => item.id === viewId);
    return Number.isFinite(view?.recordCount) ? view.recordCount : graph.recordCount;
  } catch {
    return null;
  }
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
  els.title.textContent = "Data Graph";
  els.subtitle.textContent = "Choose a graph view to inspect.";
  hideWorkspace();
  els.workspace.hidden = true;
  els.warningsBanner.hidden = true;
  els.toolbar.hidden = true;
  els.picker.hidden = false;
  els.picker.innerHTML = graphDetails
    .map(
      (graph) => `
        <article class="picker-card">
          <h2>${escapeHtml(graph.name)}</h2>
          <p>${formatCountLabel(graph.recordCount ?? 0, "record")}</p>
          <div class="picker-views">
            ${(graph.views || [])
              .map(
                (view) => `
                  <a class="picker-row" href="/?graphId=${encodeURIComponent(graph.id)}&viewId=${encodeURIComponent(view.id)}">
                    <strong>${escapeHtml(view.name)}</strong>
                    <span>${escapeHtml(view.description || "No description")} · ${formatCountLabel(view.recordCount, "record")}</span>
                  </a>
                `,
              )
              .join("")}
          </div>
        </article>
      `,
    )
    .join("") || `<div class="empty-landing"><h2>No graphs yet</h2><p>Start with the README quickstart, then return here to choose a graph view.</p></div>`;
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
    render({ dataChanged: true });
  });
  els.topicSearch?.addEventListener("input", () => {
    runtime.state = updateTopicPanel(runtime.state, { topicSearch: els.topicSearch.value });
    renderTopics(runtime.state, runtime.visibleRecords);
  });
  els.topicSort?.addEventListener("change", () => {
    runtime.state = updateTopicPanel(runtime.state, { topicSort: els.topicSort.value });
    renderTopics(runtime.state, runtime.visibleRecords);
  });
  els.startFilter?.addEventListener("change", () => {
    runtime.state = updateFilters(runtime.state, { start: els.startFilter.value });
    render({ dataChanged: true });
  });
  els.endFilter?.addEventListener("change", () => {
    runtime.state = updateFilters(runtime.state, { end: els.endFilter.value });
    render({ dataChanged: true });
  });
  document.querySelectorAll("[data-date-preset]").forEach((button) => {
    button.addEventListener("click", () => {
      runtime.state = applyDatePreset(runtime.state, button.dataset.datePreset);
      render({ dataChanged: true });
    });
  });
  els.mapModeButton?.addEventListener("click", () => {
    runtime.mode = "map";
    updateUrl();
    render({ modeChanged: true });
  });
  els.listModeButton?.addEventListener("click", () => {
    runtime.mode = "list";
    updateUrl();
    render({ modeChanged: true });
  });
  els.clearTopicButton?.addEventListener("click", () => {
    runtime.state = clearTopicSelection(runtime.state);
    updateUrl();
    render({ selectionOnly: true });
  });
  els.fitButton?.addEventListener("click", fitVisible);
  if (!runtime.resizeBound && typeof window !== "undefined") {
    window.addEventListener("resize", () => scheduleMapUpdate());
    runtime.resizeBound = true;
  }
}

function render(options = {}) {
  const {
    dataChanged = false,
    selectionOnly = false,
    listPageChanged = false,
    scrollSelectedTopic = false,
  } = options;
  const state = runtime.state;
  if (!state) return;
  els.picker.hidden = true;
  els.toolbar.hidden = false;
  els.workspace.hidden = false;
  els.mapShell.hidden = runtime.mode !== "map";
  els.listShell.hidden = runtime.mode !== "list";
  const records = dataChanged ? visibleRecords(state) : runtime.visibleRecords;
  if (dataChanged) {
    runtime.visibleRecords = records;
  }
  els.title.textContent = "Data Graph";
  els.subtitle.textContent = `${state.artifact.graphId} · ${state.artifact.viewId}`;
  els.stats.innerHTML = `
    <span>${formatCountLabel(records.length, "visible record")}</span>
    <span>${formatCountLabel(state.topics.length, "topic")}</span>
    <span>${formatCountLabel(state.artifact.noise.noiseCount, "noise record")}</span>
  `;
  renderWarnings(state);
  renderProvenance(state);
  syncControls(state);
  if (selectionOnly) {
    updateTopicSelectionClasses(state);
  } else {
    renderTopics(state, records);
  }
  if (scrollSelectedTopic) {
    scrollSelectedTopicIntoView();
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
  updateUrl();
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
        `<option value="${topic.clusterId}">${escapeHtml(topicLabel(topic))} (${formatCount(topic.size)})</option>`,
    )
    .join("")}`;
  els.topicFilter.value =
    state.filters.topicId === "" ? "" : String(state.filters.topicId);
  els.topicSearch.value = state.topicSearch;
  els.topicSort.value = state.topicSort;
  els.mapModeButton.setAttribute("aria-pressed", String(runtime.mode === "map"));
  els.listModeButton.setAttribute("aria-pressed", String(runtime.mode === "list"));
  els.fitButton.hidden = runtime.mode !== "map";
  els.fitButton.disabled = runtime.mode !== "map";
  const hasSelection = state.selectedTopicId !== null || state.selectedRecordId !== null;
  els.clearTopicButton.hidden = !hasSelection;
  els.clearTopicButton.disabled = !hasSelection;
}

function renderTopics(state, records) {
  els.topicList.innerHTML = topicPanelTopics(state, records)
    .map(({ topic, visibleCount, selected }) => {
      const badge = spikeBadge(topic);
      const series = runtime.trends.get(topic.clusterId);
      const sparkline = renderSparkline(series, "small");
      return `
        <button class="topic ${selected ? "selected" : ""}" type="button" data-topic-id="${topic.clusterId}">
          <span class="topic-swatch" style="background:${clusterColor(topic.clusterId)}"></span>
          <span class="topic-main">
            <strong>${escapeHtml(topicLabel(topic))}</strong>
            <small>${formatCount(visibleCount)}/${formatCount(topic.size)} visible · mean p ${formatNumber(topic.meanProbability)}</small>
            ${sparkline}
            ${topic.summary ? `<span>${escapeHtml(topic.summary)}</span>` : ""}
            ${topic.coherent === false ? `<em>Low coherence</em>` : ""}
            ${badge ? `<mark>${escapeHtml(badge.text)}</mark>` : ""}
            <span class="source-mix">${formatSourceMix(topic.sourceMix)}</span>
          </span>
        </button>
      `;
    })
    .join("");
  els.topicList.querySelectorAll("[data-topic-id]").forEach((button) => {
    button.addEventListener("click", () => {
      runtime.state = selectTopic(runtime.state, button.dataset.topicId);
      updateUrl();
      render({ selectionOnly: true });
      fitSelectedTopic();
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
  const selectedRecords = records.filter((record) => record.clusterId === selectedTopic.clusterId);
  const series = runtime.trends.get(selectedTopic.clusterId);
  els.emptyState.hidden = true;
  els.details.hidden = false;
  els.details.innerHTML = `
    <h2>${escapeHtml(topicLabel(selectedTopic))}</h2>
    ${renderSparkline(series, "large")}
    ${selectedTopic.summary ? `<p class="wrap-text">${escapeHtml(selectedTopic.summary)}</p>` : ""}
    <dl>
      <dt>Coherence</dt><dd>${selectedTopic.coherent === false ? "Low" : "Normal"}</dd>
      <dt>Visible records</dt><dd>${formatCount(selectedRecords.length)}</dd>
      <dt>Total records</dt><dd>${formatCount(selectedTopic.size)}</dd>
      <dt>Source mix</dt><dd>${formatSourceMix(selectedTopic.sourceMix)}</dd>
    </dl>
    ${renderFacetControls(state, selectedTopic)}
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
  els.details.querySelector("[data-facet-selector]")?.addEventListener("change", (event) => {
    runtime.facetField = event.target.value;
    runtime.facetStatus = runtime.facetField ? "loading" : "idle";
    runtime.facetTopics = null;
    runtime.facetError = "";
    renderInspector(runtime.state, runtime.visibleRecords);
    if (runtime.facetField) fetchFacets(runtime.facetField);
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
      <span>Showing ${formatCount(windowed.showing)} of ${formatCount(windowed.total)}</span>
      ${
        windowed.remaining > 0
          ? `<button type="button" data-show-more>Show ${formatCount(Math.min(windowed.remaining, 500))} more</button>`
          : ""
      }
    </div>
    <table>
      <thead><tr><th>Topic</th><th>Record</th><th>Source</th><th>Sentiment</th><th>Text</th></tr></thead>
      <tbody>
        ${windowed.records
          .map((record) => {
            const topic = state.topicById.get(record.clusterId);
            const topicSelected =
              !state.selectedRecordId &&
              state.selectedTopicId !== null &&
              record.clusterId === state.selectedTopicId;
            return `
              <tr class="${record.id === state.selectedRecordId ? "selected" : ""} ${topicSelected ? "topic-selected" : ""}" data-record-id="${escapeAttr(record.id)}" tabindex="0">
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
    els.mapCanvas.innerHTML = "";
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
  const baseRadius = recordSelected
    ? 13
    : record.isNoise
      ? 4
      : Math.round(Math.max(5, Math.min(12, 5 + record.outlierScore * 5)) * 2) / 2;
  const radiusBucket =
    !recordSelected && topicSelected && selectedTopicId !== null && selectedTopicId !== ""
      ? Math.min(14, Math.round(baseRadius * 1.18 * 2) / 2)
      : baseRadius;
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
        color: !topicSelected
          ? "rgba(148, 163, 184, 0.24)"
          : record.isNoise
            ? "rgba(120, 120, 120, 0.35)"
            : `${clusterColor(record.clusterId)}${lowProbability ? "88" : "dd"}`,
      }),
      stroke: new Stroke({
        color: recordSelected ? "#f8fafc" : record.isNoise ? "#4b5563" : topicSelected ? "#111827" : "rgba(255,255,255,0.45)",
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

function fitSelectedTopic() {
  if (!runtime.map || runtime.mode !== "map" || !runtime.state) return false;
  const selectedRecords = selectedTopicExtentRecords(runtime.state, runtime.visibleRecords);
  if (!selectedRecords.length) return false;
  runtime.map.updateSize();
  runtime.map.getView().fit(boundingExtent(selectedRecords.map((record) => [record.x, record.y])), {
    padding: [72, 72, 72, 72],
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
  els.workspace.hidden = true;
  els.mapShell.hidden = true;
  els.listShell.hidden = true;
  els.toolbar.hidden = true;
  els.topicList.innerHTML = "";
  els.provenance.innerHTML = "";
  els.stats.innerHTML = "";
}

function selectRecordAndRender(recordId) {
  runtime.state = selectRecord(runtime.state, recordId);
  updateUrl();
  render({ selectionOnly: true, scrollSelectedTopic: true });
}

function renderLoading(message) {
  els.picker.hidden = true;
  els.workspace.hidden = false;
  els.warningsBanner.hidden = true;
  els.mapShell.hidden = runtime.mode !== "map";
  els.listShell.hidden = runtime.mode !== "list";
  els.topicList.innerHTML = "";
  els.provenance.innerHTML = "";
  els.emptyState.hidden = true;
  els.details.hidden = false;
  els.details.innerHTML = `<div class="loading-state"><span class="spinner"></span><strong>${escapeHtml(message)}</strong></div>`;
  if (runtime.mode === "list") {
    els.listContent.innerHTML = `<div class="loading-state"><span class="spinner"></span><strong>${escapeHtml(message)}</strong></div>`;
  } else {
    els.mapCanvas.innerHTML = `<div class="loading-state"><span class="spinner"></span><strong>${escapeHtml(message)}</strong></div>`;
  }
}

function renderWarnings(state) {
  const warnings = state.artifact.warnings || [];
  if (!warnings.length || els.warningsBanner.dataset.dismissed === "true") {
    els.warningsBanner.hidden = true;
    return;
  }
  els.warningsBanner.hidden = false;
  els.warningsBanner.innerHTML = `
    <div>
      <strong>Artifact warnings</strong>
      <ul>${warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>
    </div>
    <button type="button" aria-label="Dismiss warnings">Dismiss</button>
  `;
  els.warningsBanner.querySelector("button")?.addEventListener("click", () => {
    els.warningsBanner.dataset.dismissed = "true";
    els.warningsBanner.hidden = true;
  });
}

function renderProvenance(state) {
  const refs = state.artifact.runRefs || {};
  const items = [
    ["embed", refs.embeddingRunId],
    ["cluster", refs.clusterRunId],
    ["label", refs.labelRunId],
    ["trend", refs.trendRunId],
  ].filter(([, value]) => value);
  els.provenance.innerHTML = `
    <span>Runs</span>
    ${items.map(([label, value]) => renderRunChip(label, value)).join("")}
    ${
      state.artifact.representation === "summary"
        ? `<span class="representation-pill">summary representation</span>${refs.summarizeRunId ? renderRunChip("summary", refs.summarizeRunId) : ""}`
        : `<span class="representation-pill">raw representation</span>`
    }
  `;
  els.provenance.querySelectorAll("[data-copy-run-id]").forEach((button) => {
    button.addEventListener("click", () => copyRunId(button));
  });
}

async function fetchTrendsOnce() {
  if (!runtime.state || runtime.trendsStatus !== "idle") return;
  runtime.trendsStatus = "loading";
  const { graphId, viewId } = runtime.state.artifact;
  try {
    const response = await fetch(
      `/api/graphs/${encodeURIComponent(graphId)}/views/${encodeURIComponent(viewId)}/trends`,
    );
    if (response.status === 409) {
      runtime.trendsStatus = "none";
      return;
    }
    if (!response.ok) throw new Error(`Trend request failed with ${response.status}`);
    const body = await response.json();
    runtime.trendBucket = body.bucket || null;
    runtime.trends = new Map(
      (body.series || []).map((series) => [series.clusterId, series.buckets || []]),
    );
    runtime.trendsStatus = "loaded";
    renderTopics(runtime.state, runtime.visibleRecords);
    scrollSelectedTopicIntoView();
    renderInspector(runtime.state, runtime.visibleRecords);
  } catch {
    runtime.trendsStatus = "error";
  }
}

function renderSparkline(buckets, size) {
  if (!buckets?.length) return "";
  const width = size === "large" ? 220 : 96;
  const height = size === "large" ? 52 : 24;
  const parts = trendSparklineParts(buckets, {
    width,
    height,
    padding: 3,
    bucket: runtime.trendBucket,
    maxRecordTimestamp: runtime.state?.maxRecordTimestamp,
  });
  if (!parts.linePath) return "";
  const fillPath = `${parts.linePath}${parts.partialPath ? ` ${parts.partialPath.replace(/^M /, "L ")}` : ""}`;
  return `
    <svg class="sparkline sparkline-${size}" viewBox="0 0 ${width} ${height}" role="img" aria-label="Topic trend sparkline">
      <title>${escapeHtml(parts.title)}</title>
      <path class="sparkline-fill" d="${escapeAttr(fillPath)} L ${width - 3} ${height - 3} L 3 ${height - 3} Z"></path>
      <path class="sparkline-line" d="${escapeAttr(parts.linePath)}"></path>
      ${parts.partialPath ? `<path class="sparkline-partial" d="${escapeAttr(parts.partialPath)}"></path>` : ""}
      ${parts.partialPoint ? `<circle class="sparkline-partial-point" cx="${roundSvg(parts.partialPoint.x)}" cy="${roundSvg(parts.partialPoint.y)}" r="2.75"></circle>` : ""}
    </svg>
  `;
}

function renderFacetControls(state, selectedTopic) {
  const fields = [
    ["sourceType", "Source type"],
    ["sourceName", "Source name"],
    ["product", "Product"],
    ["sentiment", "Sentiment"],
  ];
  if (state.artifact.representation === "summary") {
    fields.push(
      ["summary.product", "Summary product"],
      ["summary.issue", "Summary issue"],
      ["summary.junkType", "Summary junk type"],
    );
  }
  const selected = runtime.facetTopics?.find(
    (topic) => topic.clusterId === selectedTopic.clusterId,
  );
  return `
    <section class="facet-panel">
      <label>
        <span>Facet</span>
        <select data-facet-selector aria-label="Facet topic records">
          <option value="">Choose facet</option>
          ${fields
            .map(
              ([value, label]) =>
                `<option value="${escapeAttr(value)}" ${runtime.facetField === value ? "selected" : ""}>${escapeHtml(label)}</option>`,
            )
            .join("")}
        </select>
      </label>
      ${facetBody(selected)}
    </section>
  `;
}

function facetBody(topic) {
  if (!runtime.facetField) return `<p class="muted">Choose a facet to inspect this topic.</p>`;
  if (runtime.facetStatus === "loading") return `<p class="muted">Loading facets...</p>`;
  if (runtime.facetStatus === "error") {
    return `<p class="muted">${escapeHtml(runtime.facetError || "Facet unavailable.")}</p>`;
  }
  const facets = Object.entries(topic?.facets || {}).map(([value, count]) => ({
    value,
    count,
  }));
  if (!facets.length) return `<p class="muted">No facet counts for this topic.</p>`;
  const max = Math.max(...facets.map((facet) => facet.count));
  return `
    <div class="facet-bars">
      ${facets
        .map(
          (facet) => `
            <div class="facet-row">
              <span>${escapeHtml(facet.value)}</span>
              <div><i style="width:${Math.max(4, (facet.count / max) * 100)}%"></i></div>
              <strong>${formatCount(facet.count)}</strong>
            </div>
          `,
        )
        .join("")}
    </div>
  `;
}

async function fetchFacets(field) {
  if (!runtime.state) return;
  const { graphId, viewId } = runtime.state.artifact;
  try {
    const response = await fetch(
      `/api/graphs/${encodeURIComponent(graphId)}/views/${encodeURIComponent(viewId)}/topics?facetBy=${encodeURIComponent(field)}`,
    );
    if (response.status === 422) {
      runtime.facetStatus = "error";
      runtime.facetError = "Summary facets need summary representation lineage.";
      renderInspector(runtime.state, runtime.visibleRecords);
      return;
    }
    if (!response.ok) throw new Error(`Facet request failed with ${response.status}`);
    const body = await response.json();
    runtime.facetTopics = body.topics || [];
    runtime.facetStatus = "loaded";
  } catch (error) {
    runtime.facetStatus = "error";
    runtime.facetError = error instanceof Error ? error.message : String(error);
  }
  renderInspector(runtime.state, runtime.visibleRecords);
}

function updateUrl() {
  if (!runtime.state || typeof window === "undefined") return;
  const next = viewSearchParams(runtime.state, runtime.mode);
  if (window.location.search !== next) {
    window.history.replaceState(null, "", next);
  }
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

function scrollSelectedTopicIntoView() {
  const topicId = runtime.state?.selectedTopicId;
  if (topicId === null || topicId === undefined) {
    return;
  }
  requestAnimationFrame(() => {
    const selected = els.topicList.querySelector(
      `[data-topic-id="${escapeCssIdentifier(String(topicId))}"]`,
    );
    selected?.scrollIntoView({ block: "nearest" });
  });
}

function updateListSelectionClasses(state) {
  els.listContent.querySelectorAll("[data-record-id]").forEach((row) => {
    const record = state.recordById.get(row.dataset.recordId);
    row.classList.toggle("selected", row.dataset.recordId === state.selectedRecordId);
    row.classList.toggle(
      "topic-selected",
      !state.selectedRecordId &&
        state.selectedTopicId !== null &&
        record?.clusterId === state.selectedTopicId,
    );
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
    .map(([source, count]) => `${escapeHtml(source)} ${formatCount(count)}`)
    .join(" · ");
}

function formatNumber(value) {
  return Number.isFinite(value) ? value.toFixed(2) : "n/a";
}

function shortRunId(value) {
  return String(value ?? "").slice(-6);
}

function renderRunChip(label, value) {
  const runId = String(value);
  return `
    <button class="run-chip" type="button" data-copy-run-id="${escapeAttr(runId)}" title="${escapeAttr(runId)}">
      <code>${escapeHtml(label)}:${escapeHtml(shortRunId(runId))}</code>
      <span class="copy-status" aria-hidden="true">copied</span>
    </button>
  `;
}

async function copyRunId(button) {
  const runId = button.dataset.copyRunId;
  if (!runId) return;
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(runId);
    button.classList.add("copied");
    button.setAttribute("aria-label", "Copied full run id");
    window.setTimeout(() => {
      button.classList.remove("copied");
      button.removeAttribute("aria-label");
    }, 1200);
  } catch {
    button.classList.add("copy-unavailable");
  }
}

function roundSvg(value) {
  return String(Math.round(value * 100) / 100);
}

function escapeCssIdentifier(value) {
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
    return CSS.escape(value);
  }
  return value.replace(/["\\]/g, "\\$&");
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
