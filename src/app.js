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
  labelSource: null,
  labelLayer: null,
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
    const sinkId = dataSinkIdFromPath();
    const datasetUrl = sinkId
      ? `/api/data-sink/${encodeURIComponent(sinkId)}/artifact/latest`
      : "./sample-data/commerce.json";
    const response = await fetch(datasetUrl);
    if (!response.ok)
      throw new Error(`Could not load dataset: ${response.status}`);
    const dataset = await response.json();
    loadRecords(dataset);
  } catch (error) {
    showLoadError(error);
  }
}

function dataSinkIdFromPath() {
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
  els.viewTitle.textContent = state.dataset.name || "Data Atlas";
  els.stats.innerHTML = `
    <div class="stat"><strong>${state.records.length}</strong><span>Records</span></div>
    <div class="stat"><strong>${state.clusters.length}</strong><span>Clusters</span></div>
  `;
}

function renderLegend() {
  els.legend.innerHTML = "";
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
        <p class="detail-meta">${escapeHtml(groupingLabel())}</p>
        <h2>${escapeHtml(cluster.name)}</h2>
        <p class="record-detail">${cluster.items.length} grouped records.</p>
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
      <p class="detail-meta">${escapeHtml(record.__clusterLabel || record.clusterLabel || "")} · ${escapeHtml(record.__groupValue || record.groupValue || "")}</p>
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
      state.selected = { type: "cluster", value: cluster };
      renderDetails();
      state.layer.changed();
      state.labelLayer.changed();
      fitMap(cluster);
      return;
    }
    state.selected = { type: "record", value: feature.get("record") };
    renderDetails();
    state.layer.changed();
    state.labelLayer.changed();
  });

  state.map.getView().on("change:resolution", () => {
    state.layer.changed();
    state.labelLayer.changed();
  });

  fitMap();
}

function styleFeature(feature) {
  const record = feature.get("record");
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
  const records = cluster?.items || state.records;
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
  state.labelLayer.changed();
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
  state.labelLayer.changed();
  if (state.mode === "map") {
    state.map
      .getView()
      .animate({ center: [record.x, record.y], zoom: 16, duration: 260 });
  }
});

loadDataset();
