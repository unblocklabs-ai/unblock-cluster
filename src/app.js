import "ol/ol.css";
import Feature from "ol/Feature";
import OlMap from "ol/Map";
import View from "ol/View";
import Point from "ol/geom/Point";
import VectorLayer from "ol/layer/Vector";
import VectorSource from "ol/source/Vector";
import { Circle as CircleStyle, Fill, Stroke, Style, Text } from "ol/style";
import { boundingExtent } from "ol/extent";

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
  stats: document.querySelector("#stats"),
  legend: document.querySelector("#legend"),
  details: document.querySelector("#details"),
  emptyState: document.querySelector("#emptyState"),
  viewTitle: document.querySelector("#viewTitle"),
  viewSubtitle: document.querySelector("#viewSubtitle"),
  viewToggleButton: document.querySelector("#viewToggleButton"),
  mapModeButton: document.querySelector("#mapModeButton"),
  zoomInButton: document.querySelector("#zoomInButton"),
  zoomOutButton: document.querySelector("#zoomOutButton"),
};

async function loadDataset() {
  try {
    const response = await fetch("./sample-data/cars.processed.json");
    if (!response.ok)
      throw new Error(`Could not load dataset: ${response.status}`);
    const dataset = await response.json();
    loadRecords(dataset);
  } catch (error) {
    showLoadError(error);
  }
}

function loadRecords(dataset) {
  const records = dataset.records;
  if (!Array.isArray(records)) {
    throw new Error("Dataset JSON must include a records array.");
  }

  state.dataset = dataset;
  state.records = records.map((record, index) => ({
    ...record,
    __index: index,
  }));
  state.fields = collectFields(state.records);
  state.clusters = buildClusters(state.records);

  renderChrome();
  renderLegend();
  renderList();
  renderDetails();
  renderMap();
}

function showLoadError(error) {
  els.viewTitle.textContent = "Dataset could not load";
  els.viewSubtitle.textContent = error.message;
  els.stats.innerHTML = "";
  els.legend.innerHTML = "";
  els.emptyState.classList.remove("hidden");
  els.details.classList.add("hidden");
  els.emptyState.innerHTML = `
    <span>Processed JSON is missing</span>
    <p>Run npm run process:data, then reload http://localhost:4173.</p>
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
    const id = String(record.clusterId ?? "unknown");
    if (!grouped.has(id)) grouped.set(id, []);
    grouped.get(id).push(record);
  });

  const clusters = [...grouped.entries()]
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
    .map(([id, items], index) => ({
      id,
      name: items[0]?.clusterLabel || `Cluster ${id}`,
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
  const layout = state.dataset.layout;
  els.viewTitle.textContent = state.dataset.name || "Data Atlas";
  els.stats.innerHTML = `
    <div class="stat"><strong>${state.records.length}</strong><span>Records</span></div>
    <div class="stat"><strong>${state.clusters.length}</strong><span>Clusters</span></div>
  `;
}

function renderLegend() {
  els.legend.innerHTML = state.clusters
    .map(
      (cluster) => `
        <button class="legend-item" type="button" data-cluster="${escapeHtml(cluster.id)}">
          <span class="swatch" style="background:${cluster.color}"></span>
          ${escapeHtml(cluster.name)} (${cluster.items.length})
        </button>
      `,
    )
    .join("");
}

function renderList() {
  els.listContent.innerHTML = state.clusters
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
      <span class="thumb color-thumb" style="background:${clusterForRecord(record)?.color || "#d9ddd8"}"></span>
      <span>
        <strong>${escapeHtml(record[state.dataset.titleField])}</strong>
        <span>${escapeHtml(record[state.dataset.detailField])}</span>
      </span>
    </button>
  `;
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
    els.details.innerHTML = `
      <div>
        <p class="detail-meta">HDBSCAN cluster</p>
        <h2>${escapeHtml(cluster.name)}</h2>
        <p class="record-detail">${cluster.items.length} nearby records in PaCMAP space.</p>
      </div>
      <div class="field-table">
        ${cluster.items
          .slice(0, 12)
          .map((record) => renderRecordCard(record))
          .join("")}
      </div>
    `;
    return;
  }

  const record = state.selected.value;
  els.details.innerHTML = `
    <div>
      <p class="detail-meta">${escapeHtml(record.clusterLabel)} · ${escapeHtml(record.groupValue)}</p>
      <h2>${escapeHtml(record[state.dataset.titleField])}</h2>
      <p class="record-detail">${escapeHtml(record[state.dataset.detailField])}</p>
    </div>
    <div class="field-table">
      ${state.fields
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
  const features = state.records.map((record) => {
    const feature = new Feature({
      geometry: new Point([record.x, record.y]),
      record,
      cluster: clusterForRecord(record),
    });
    feature.setId(record.__index);
    return feature;
  });

  state.source = new VectorSource({ features });
  state.layer = new VectorLayer({
    source: state.source,
    style: styleFeature,
  });

  state.map = new OlMap({
    target: els.map,
    layers: [state.layer],
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
    state.selected = { type: "record", value: feature.get("record") };
    renderDetails();
    state.layer.changed();
  });

  state.map.getView().on("change:resolution", () => state.layer.changed());

  fitMap();
}

function styleFeature(feature) {
  const record = feature.get("record");
  const cluster = feature.get("cluster");
  const selected =
    state.selected?.type === "record" &&
    state.selected.value.__index === record.__index;
  const zoom = state.map?.getView().getZoom() || 12;
  const showLabel = zoom >= 14 || selected;

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

function fitMap(cluster) {
  const records = cluster?.items || state.records;
  const extent = boundingExtent(records.map((record) => [record.x, record.y]));
  state.map.getView().fit(extent, {
    padding: [80, 80, 80, 80],
    duration: 280,
    maxZoom: cluster ? 16 : 13,
  });
}

function clusterForRecord(record) {
  return state.clusters.find(
    (cluster) => cluster.id === String(record.clusterId),
  );
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

els.legend.addEventListener("click", (event) => {
  const button = event.target.closest("[data-cluster]");
  if (!button) return;
  const cluster = state.clusters.find(
    (entry) => entry.id === button.dataset.cluster,
  );
  if (!cluster) return;
  state.selected = { type: "cluster", value: cluster };
  renderDetails();
  state.layer.changed();
  fitMap(cluster);
});

document.addEventListener("click", (event) => {
  const recordButton = event.target.closest("[data-record]");
  if (!recordButton) return;
  const record = state.records.find(
    (entry) => String(entry.__index) === recordButton.dataset.record,
  );
  if (!record) return;
  state.selected = { type: "record", value: record };
  renderDetails();
  state.layer.changed();
  if (state.mode === "map") {
    state.map
      .getView()
      .animate({ center: [record.x, record.y], zoom: 16, duration: 260 });
  }
});

loadDataset();
