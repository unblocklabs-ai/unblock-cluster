const state = {
  records: [],
  fields: [],
  groupField: "",
  titleField: "",
  detailField: "",
  imageField: "",
  clusters: [],
  filteredRecords: [],
  selected: null,
  mode: "map",
  search: "",
  view: { x: 0, y: 0, scale: 1 },
  dragging: false,
  lastPointer: null,
  pointerStart: null,
  pointIndex: [],
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
];

const els = {
  canvas: document.querySelector("#mapCanvas"),
  mapShell: document.querySelector("#mapShell"),
  listShell: document.querySelector("#listShell"),
  listContent: document.querySelector("#listContent"),
  datasetDescription: document.querySelector("#datasetDescription"),
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

const ctx = els.canvas.getContext("2d");

async function loadDataset() {
  try {
    const response = await fetch("./sample-data/restaurants.json");
    if (!response.ok)
      throw new Error(`Could not load dataset: ${response.status}`);
    const dataset = await response.json();
    loadRecords(dataset);
  } catch (error) {
    showLoadError(error);
  }
}

function loadRecords(dataset) {
  const records = Array.isArray(dataset) ? dataset : dataset.records;
  const preferred = Array.isArray(dataset)
    ? {}
    : {
        groupField: dataset.groupingField,
        titleField: dataset.titleField,
        detailField: dataset.detailField,
        imageField: dataset.imageField,
      };

  if (!Array.isArray(records)) {
    throw new Error("Dataset JSON must include a records array.");
  }

  state.datasetName = dataset.name || "Data Atlas";
  state.datasetDescription =
    dataset.description || "Cluster a JSON dataset by its configured field.";
  state.records = records.map((record, index) => ({
    ...record,
    __index: index,
  }));
  state.fields = collectFields(state.records);
  state.groupField = pickField(preferred.groupField, [
    "groupBy",
    "group",
    "category",
    "genre",
    "cuisine",
    "location",
    "neighborhood",
    "rating",
  ]);
  state.titleField = pickField(preferred.titleField, [
    "title",
    "name",
    "label",
    "restaurant",
    "book",
  ]);
  state.detailField = pickField(preferred.detailField, [
    "description",
    "reviewTone",
    "summary",
    "reviews",
    "author",
    "bestFor",
  ]);
  state.imageField = pickField(preferred.imageField, [
    "image",
    "imageUrl",
    "cover",
    "photo",
    "thumbnail",
  ]);
  state.search = "";
  if (els.datasetDescription)
    els.datasetDescription.textContent = state.datasetDescription;
  compute();
}

function showLoadError(error) {
  els.viewTitle.textContent = "Dataset could not load";
  els.viewSubtitle.textContent = error.message;
  if (els.datasetDescription) {
    els.datasetDescription.textContent =
      "Open this app through the local server so it can read the JSON file.";
  }
  els.stats.innerHTML = "";
  els.legend.innerHTML = "";
  els.emptyState.classList.remove("hidden");
  els.details.classList.add("hidden");
  els.emptyState.innerHTML = `
    <span>JSON file blocked by browser security</span>
    <p>Use http://localhost:4173 instead of opening index.html directly, so the app can load sample-data/movies.json.</p>
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

function pickField(preferred, candidates) {
  if (preferred && state.fields.includes(preferred)) return preferred;
  const lowerMap = new Map(
    state.fields.map((field) => [field.toLowerCase(), field]),
  );
  for (const candidate of candidates) {
    const exact = lowerMap.get(candidate.toLowerCase());
    if (exact) return exact;
  }
  return state.fields[0] || "";
}

function compute() {
  state.filteredRecords = filterRecords(state.records);
  state.clusters = buildClusters(state.filteredRecords, state.groupField);
  positionClusters(state.clusters);
  fitViewToClusters();
  state.selected = null;
  render();
}

function filterRecords(records) {
  const query = state.search.trim().toLowerCase();
  if (!query) return [...records];
  return records.filter((record) =>
    Object.values(record).some((value) =>
      String(value ?? "")
        .toLowerCase()
        .includes(query),
    ),
  );
}

function buildClusters(records, field) {
  const grouped = new Map();
  records.forEach((record) => {
    const value = normalizeGroupValue(record[field]);
    if (!grouped.has(value)) grouped.set(value, []);
    grouped.get(value).push(record);
  });

  return [...grouped.entries()]
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
    .map(([name, items], index) => ({
      id: slugify(name),
      name,
      items,
      color: palette[index % palette.length],
      x: 0,
      y: 0,
      radius: 90,
    }));
}

function normalizeGroupValue(value) {
  if (Array.isArray(value)) return value.join(", ");
  if (value === undefined || value === null || value === "") return "Unknown";
  return String(value);
}

function positionClusters(clusters) {
  const total = clusters.length || 1;
  clusters.forEach((cluster) => {
    cluster.radius = Math.max(82, Math.sqrt(cluster.items.length) * 38 + 34);
  });

  const maxRadius = Math.max(...clusters.map((cluster) => cluster.radius), 82);
  const ringRadius = Math.max(360, total * 54, maxRadius * 2.2);
  clusters.forEach((cluster, clusterIndex) => {
    const angle = -Math.PI / 2 + (Math.PI * 2 * clusterIndex) / total;
    cluster.x = Math.cos(angle) * ringRadius;
    cluster.y = Math.sin(angle) * ringRadius;

    cluster.items.forEach((item, itemIndex) => {
      const itemAngle = Math.PI * 2 * itemIndex * 0.61803398875;
      const itemRadius = Math.sqrt(itemIndex + 1) * 23;
      item.__x =
        cluster.x +
        Math.cos(itemAngle) * Math.min(itemRadius, cluster.radius - 26);
      item.__y =
        cluster.y +
        Math.sin(itemAngle) * Math.min(itemRadius, cluster.radius - 26);
      item.__cluster = cluster;
    });
  });
}

function render() {
  els.viewTitle.textContent =
    state.datasetName || `${humanize(state.groupField)} clusters`;
  els.viewSubtitle.textContent = `${state.filteredRecords.length} visible records grouped from the ${state.groupField} field.`;
  els.stats.innerHTML = `
    <div class="stat"><strong>${state.filteredRecords.length}</strong><span>Records</span></div>
    <div class="stat"><strong>${state.clusters.length}</strong><span>Groups</span></div>
  `;

  renderLegend();
  renderList();
  renderDetails();
  drawMap();
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
  const title = getValue(record, state.titleField);
  const detail = getValue(record, state.detailField);
  const image = getValue(record, state.imageField);
  return `
    <button class="record-card" type="button" data-record="${record.__index}">
      ${image ? `<img class="thumb" src="${escapeAttr(image)}" alt="">` : `<span class="thumb"></span>`}
      <span>
        <strong>${escapeHtml(title)}</strong>
        <span>${escapeHtml(detail)}</span>
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
        <p class="detail-meta">Cluster</p>
        <h2>${escapeHtml(cluster.name)}</h2>
        <p class="record-detail">${cluster.items.length} records grouped by ${escapeHtml(state.groupField)}.</p>
      </div>
      <div class="field-table">
        ${cluster.items
          .slice(0, 10)
          .map(
            (record) => `
          <button class="record-card" type="button" data-record="${record.__index}">
            ${getValue(record, state.imageField) ? `<img class="thumb" src="${escapeAttr(getValue(record, state.imageField))}" alt="">` : `<span class="thumb"></span>`}
            <span>
              <strong>${escapeHtml(getValue(record, state.titleField))}</strong>
              <span>${escapeHtml(getValue(record, state.detailField))}</span>
            </span>
          </button>
        `,
          )
          .join("")}
      </div>
    `;
    return;
  }

  const record = state.selected.value;
  const image = getValue(record, state.imageField);
  els.details.innerHTML = `
    ${image ? `<img src="${escapeAttr(image)}" alt="">` : ""}
    <div>
      <p class="detail-meta">${escapeHtml(normalizeGroupValue(record[state.groupField]))}</p>
      <h2>${escapeHtml(getValue(record, state.titleField))}</h2>
      <p class="record-detail">${escapeHtml(getValue(record, state.detailField))}</p>
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

function drawMap() {
  const rect = els.canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(rect.width * dpr));
  const height = Math.max(1, Math.floor(rect.height * dpr));
  if (els.canvas.width !== width || els.canvas.height !== height) {
    els.canvas.width = width;
    els.canvas.height = height;
  }

  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);
  ctx.save();
  ctx.translate(rect.width / 2 + state.view.x, rect.height / 2 + state.view.y);
  ctx.scale(state.view.scale, state.view.scale);

  state.pointIndex = [];
  drawGrid(rect.width, rect.height);

  state.clusters.forEach((cluster) => drawCluster(cluster));
  state.clusters.forEach((cluster) =>
    cluster.items.forEach((item) => drawPoint(item, cluster)),
  );

  ctx.restore();
}

function drawGrid(width, height) {
  const extent = Math.max(width, height) / state.view.scale;
  ctx.strokeStyle = "rgba(31, 37, 40, 0.055)";
  ctx.lineWidth = 1 / state.view.scale;
  for (let x = -extent; x <= extent; x += 80) {
    ctx.beginPath();
    ctx.moveTo(x, -extent);
    ctx.lineTo(x, extent);
    ctx.stroke();
  }
  for (let y = -extent; y <= extent; y += 80) {
    ctx.beginPath();
    ctx.moveTo(-extent, y);
    ctx.lineTo(extent, y);
    ctx.stroke();
  }
}

function drawCluster(cluster) {
  ctx.beginPath();
  ctx.arc(cluster.x, cluster.y, cluster.radius, 0, Math.PI * 2);
  ctx.fillStyle = hexToRgba(cluster.color, 0.1);
  ctx.fill();
  ctx.strokeStyle = hexToRgba(cluster.color, 0.34);
  ctx.lineWidth = 1.3 / state.view.scale;
  ctx.stroke();

  ctx.fillStyle = "#1f2528";
  ctx.font = `${Math.max(15, 18 / state.view.scale)}px Inter, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(cluster.name, cluster.x, cluster.y - cluster.radius - 20);
  ctx.fillStyle = "#6d7377";
  ctx.font = `${Math.max(11, 12 / state.view.scale)}px Inter, sans-serif`;
  ctx.fillText(
    `${cluster.items.length} records`,
    cluster.x,
    cluster.y - cluster.radius - 2,
  );
}

function drawPoint(item, cluster) {
  const radius =
    state.selected?.type === "record" &&
    state.selected.value.__index === item.__index
      ? 11
      : 7;
  ctx.beginPath();
  ctx.arc(item.__x, item.__y, radius, 0, Math.PI * 2);
  ctx.fillStyle = cluster.color;
  ctx.fill();
  ctx.strokeStyle = "#ffffff";
  ctx.lineWidth = 2 / state.view.scale;
  ctx.stroke();

  if (state.view.scale > 0.85) {
    ctx.fillStyle = "rgba(31, 37, 40, 0.78)";
    ctx.font = `${11 / state.view.scale}px Inter, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText(
      truncate(getValue(item, state.titleField), 18),
      item.__x,
      item.__y + 13,
    );
  }

  state.pointIndex.push({
    x: item.__x,
    y: item.__y,
    radius: 16,
    item,
    cluster,
  });
}

function canvasToWorld(event) {
  const rect = els.canvas.getBoundingClientRect();
  return {
    x:
      (event.clientX - rect.left - rect.width / 2 - state.view.x) /
      state.view.scale,
    y:
      (event.clientY - rect.top - rect.height / 2 - state.view.y) /
      state.view.scale,
  };
}

function selectAt(event) {
  const point = canvasToWorld(event);
  const hit = [...state.pointIndex].reverse().find((entry) => {
    const dx = entry.x - point.x;
    const dy = entry.y - point.y;
    return Math.sqrt(dx * dx + dy * dy) <= entry.radius / state.view.scale + 4;
  });

  if (hit) {
    state.selected = { type: "record", value: hit.item };
    renderDetails();
    drawMap();
    return;
  }

  const cluster = state.clusters.find((entry) => {
    const dx = entry.x - point.x;
    const dy = entry.y - point.y;
    return Math.sqrt(dx * dx + dy * dy) <= entry.radius;
  });

  if (cluster) {
    state.selected = { type: "cluster", value: cluster };
    renderDetails();
    drawMap();
  }
}

function zoomBy(factor) {
  state.view.scale = clamp(state.view.scale * factor, 0.35, 3.2);
  drawMap();
}

function resetView() {
  state.view = { x: 0, y: 0, scale: 1 };
}

function fitViewToClusters() {
  const rect = els.canvas.getBoundingClientRect();
  if (!state.clusters.length || !rect.width || !rect.height) {
    resetView();
    return;
  }

  const padding = 150;
  const bounds = state.clusters.reduce(
    (acc, cluster) => ({
      minX: Math.min(acc.minX, cluster.x - cluster.radius - padding),
      maxX: Math.max(acc.maxX, cluster.x + cluster.radius + padding),
      minY: Math.min(acc.minY, cluster.y - cluster.radius - padding),
      maxY: Math.max(acc.maxY, cluster.y + cluster.radius + padding),
    }),
    { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity },
  );

  const width = bounds.maxX - bounds.minX;
  const height = bounds.maxY - bounds.minY;
  const scale = clamp(
    Math.min(rect.width / width, rect.height / height),
    0.35,
    1.2,
  );
  const centerX = (bounds.minX + bounds.maxX) / 2;
  const centerY = (bounds.minY + bounds.maxY) / 2;
  state.view = { x: -centerX * scale, y: -centerY * scale, scale };
}

function setMode(mode) {
  state.mode = mode;
  els.mapShell.classList.toggle("hidden", mode !== "map");
  els.listShell.classList.toggle("hidden", mode !== "list");
  els.viewToggleButton.textContent = mode === "map" ? "List" : "Map";
  if (mode === "map") requestAnimationFrame(drawMap);
}

function getValue(record, field) {
  if (!field) return "";
  return formatValue(record[field]);
}

function formatValue(value) {
  if (Array.isArray(value)) return value.join(", ");
  if (value && typeof value === "object") return JSON.stringify(value);
  if (value === undefined || value === null) return "";
  return String(value);
}

function humanize(value) {
  return String(value || "Field")
    .replace(/[_-]/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function truncate(value, length) {
  const text = String(value || "");
  return text.length > length ? `${text.slice(0, length - 1)}...` : text;
}

function slugify(value) {
  return (
    String(value)
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/(^-|-$)/g, "") || "unknown"
  );
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function hexToRgba(hex, alpha) {
  const value = hex.replace("#", "");
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value);
}

els.viewToggleButton.addEventListener("click", () => setMode("list"));
els.mapModeButton.addEventListener("click", () => setMode("map"));
els.zoomInButton.addEventListener("click", () => zoomBy(1.18));
els.zoomOutButton.addEventListener("click", () => zoomBy(0.84));

els.legend.addEventListener("click", (event) => {
  const button = event.target.closest("[data-cluster]");
  if (!button) return;
  const cluster = state.clusters.find(
    (entry) => entry.id === button.dataset.cluster,
  );
  if (!cluster) return;
  state.selected = { type: "cluster", value: cluster };
  state.view.x = -cluster.x * state.view.scale;
  state.view.y = -cluster.y * state.view.scale;
  renderDetails();
  drawMap();
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
  drawMap();
});

els.canvas.addEventListener("pointerdown", (event) => {
  state.dragging = true;
  state.lastPointer = { x: event.clientX, y: event.clientY };
  state.pointerStart = { x: event.clientX, y: event.clientY };
  els.canvas.setPointerCapture(event.pointerId);
});

els.canvas.addEventListener("pointermove", (event) => {
  if (!state.dragging || !state.lastPointer) return;
  state.view.x += event.clientX - state.lastPointer.x;
  state.view.y += event.clientY - state.lastPointer.y;
  state.lastPointer = { x: event.clientX, y: event.clientY };
  drawMap();
});

els.canvas.addEventListener("pointerup", (event) => {
  const moved = state.pointerStart
    ? Math.abs(event.clientX - state.pointerStart.x) +
      Math.abs(event.clientY - state.pointerStart.y)
    : 0;
  state.dragging = false;
  state.lastPointer = null;
  state.pointerStart = null;
  if (moved < 3) selectAt(event);
});

els.canvas.addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    zoomBy(event.deltaY > 0 ? 0.92 : 1.08);
  },
  { passive: false },
);

window.addEventListener("resize", drawMap);

loadDataset();
