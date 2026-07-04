import assert from "node:assert/strict";
import test from "node:test";

import {
  backToTopic,
  applyDatePreset,
  buildViewState,
  clearTopicSelection,
  datePresetFilters,
  listWindow,
  parseViewParams,
  representativeRecords,
  selectRecord,
  selectTopic,
  selectedTopicExtentRecords,
  showMoreListRecords,
  spikeBadge,
  topicPanelTopics,
  trendSparklinePath,
  updateFilters,
  updateTopicPanel,
  visibleRecords,
  viewSearchParams,
} from "../src/uiState.js";

const artifact = {
  graphId: "grf_1",
  viewId: "view_1",
  topics: [
    {
      clusterId: 2,
      label: "Small",
      size: 1,
      meanProbability: 0.8,
      sourceMix: { review: 1 },
      representativeRecordIds: ["r3"],
      trend: null,
    },
    {
      clusterId: 1,
      label: "December spike",
      summary: "Energy crashes in December.",
      coherent: true,
      size: 2,
      meanProbability: 0.9,
      sourceMix: { support: 2 },
      representativeRecordIds: ["r1", "r2"],
      trend: { bucket: "week", spikeScore: 6.5, topBucket: "2025-12-08" },
    },
  ],
  data: [
    {
      id: "r1",
      recordId: "rec-1",
      title: "Energy crash",
      customerText: "Energy drops after lunch",
      sourceType: "support",
      timestamp: "2025-12-09T00:00:00Z",
      clusterId: 1,
    },
    {
      id: "r2",
      recordId: "rec-2",
      title: "Timing",
      customerText: "Crash again",
      sourceType: "support",
      timestamp: "2025-12-20T00:00:00Z",
      clusterId: 1,
    },
    {
      id: "r3",
      recordId: "rec-3",
      title: "Texture",
      customerText: "Clumps",
      sourceType: "review",
      timestamp: "2025-06-01T00:00:00Z",
      clusterId: 2,
    },
  ],
};

test("parses graph and view query parameters", () => {
  assert.deepEqual(parseViewParams("?graphId=grf_1&viewId=view_1"), {
    graphId: "grf_1",
    viewId: "view_1",
    mode: null,
    topicId: null,
    recordId: null,
  });
  assert.deepEqual(
    parseViewParams("?graphId=grf_1&viewId=view_1&mode=list&topicId=2&recordId=r2"),
    {
      graphId: "grf_1",
      viewId: "view_1",
      mode: "list",
      topicId: "2",
      recordId: "r2",
    },
  );
  assert.deepEqual(parseViewParams(""), {
    graphId: null,
    viewId: null,
    mode: null,
    topicId: null,
    recordId: null,
  });
});

test("round-trips URL parameters including record selection", () => {
  let state = buildViewState(artifact, { selectedTopicId: 1 });
  assert.equal(
    viewSearchParams(state, "map"),
    "?graphId=grf_1&viewId=view_1&topicId=1",
  );

  state = selectRecord(state, "r2");
  const query = viewSearchParams(state, "list");
  assert.deepEqual(parseViewParams(query), {
    graphId: "grf_1",
    viewId: "view_1",
    mode: "list",
    topicId: "1",
    recordId: "r2",
  });
});

test("builds sorted artifact view state with time extent and source filters", () => {
  const state = buildViewState(artifact);

  assert.deepEqual(
    state.topics.map((topic) => topic.clusterId),
    [1, 2],
  );
  assert.deepEqual(state.sourceTypes, ["review", "support"]);
  assert.deepEqual(state.timeExtent, { min: "2025-06-01", max: "2025-12-20" });
  assert.equal(state.topicById.get(1).label, "December spike");
  assert.equal(state.recordById.get("r1").recordId, "rec-1");
});

test("filters records by time, topic, source, and search query", () => {
  let state = buildViewState(artifact);
  state = updateFilters(state, { start: "2025-12-01", end: "2025-12-31" });
  assert.deepEqual(
    visibleRecords(state).map((record) => record.id),
    ["r1", "r2"],
  );

  state = updateFilters(state, { topicId: 1, sourceType: "support", query: "lunch" });
  assert.deepEqual(
    visibleRecords(state).map((record) => record.id),
    ["r1"],
  );
});

test("keeps selection spatial while topic filter hard-scopes records", () => {
  let state = selectTopic(buildViewState(artifact), 1);
  assert.deepEqual(
    visibleRecords(state).map((record) => record.id),
    ["r1", "r2", "r3"],
  );
  assert.deepEqual(
    selectedTopicExtentRecords(state).map((record) => record.id),
    ["r1", "r2"],
  );

  state = updateFilters(state, { topicId: 1 });
  assert.deepEqual(
    visibleRecords(state).map((record) => record.id),
    ["r1", "r2"],
  );
});

test("sorts and searches topic panel topics", () => {
  let state = buildViewState(artifact);
  assert.deepEqual(
    topicPanelTopics(state).map((entry) => entry.topic.clusterId),
    [1, 2],
  );

  state = updateTopicPanel(state, { topicSort: "name" });
  assert.deepEqual(
    topicPanelTopics(state).map((entry) => entry.topic.label),
    ["December spike", "Small"],
  );

  state = updateTopicPanel(state, { topicSort: "spike" });
  assert.deepEqual(
    topicPanelTopics(state).map((entry) => entry.topic.clusterId),
    [1, 2],
  );

  state = updateTopicPanel(state, { topicSearch: "small" });
  assert.deepEqual(
    topicPanelTopics(state).map((entry) => entry.topic.clusterId),
    [2],
  );
});

test("selects and clears topic state", () => {
  let state = selectTopic(buildViewState(artifact), 1);
  assert.equal(state.selectedTopicId, 1);
  assert.equal(state.selectedRecordId, null);
  assert.equal(state.filters.topicId, "");

  state = clearTopicSelection(state);
  assert.equal(state.selectedTopicId, null);
  assert.equal(state.selectedRecordId, null);
  assert.equal(state.filters.topicId, "");
});

test("transitions from topic to record and back to topic", () => {
  let state = selectTopic(buildViewState(artifact), 1);
  state = selectRecord(state, "r2");

  assert.equal(state.selectedTopicId, 1);
  assert.equal(state.selectedRecordId, "r2");
  assert.equal(state.filters.topicId, "");
  assert.deepEqual(
    visibleRecords(state).map((record) => record.id),
    ["r1", "r2", "r3"],
  );

  state = backToTopic(state);
  assert.equal(state.selectedTopicId, 1);
  assert.equal(state.selectedRecordId, null);
  assert.equal(state.filters.topicId, "");
});

test("record selection from list, map, and representative card converges selection state", () => {
  const base = buildViewState(artifact);
  const fromList = selectRecord(base, "r3");
  const fromMap = selectRecord(base, "r3");
  const fromCard = selectRecord(selectTopic(base, 2), "r3");

  const publicSelection = (state) => ({
    selectedTopicId: state.selectedTopicId,
    selectedRecordId: state.selectedRecordId,
  });
  assert.deepEqual(publicSelection(fromList), {
    selectedTopicId: 2,
    selectedRecordId: "r3",
  });
  assert.deepEqual(publicSelection(fromMap), publicSelection(fromList));
  assert.deepEqual(publicSelection(fromCard), publicSelection(fromList));
  assert.equal(fromList.filters.topicId, "");
  assert.equal(fromCard.filters.topicId, "");
});

test("list window caps rows and advances by page without touching filters", () => {
  const largeArtifact = {
    ...artifact,
    data: Array.from({ length: 1205 }, (_, index) => ({
      ...artifact.data[index % artifact.data.length],
      id: `r${index + 1}`,
      recordId: `rec-${index + 1}`,
    })),
  };
  let state = buildViewState(largeArtifact);
  let page = listWindow(visibleRecords(state), state);

  assert.equal(page.showing, 500);
  assert.equal(page.total, 1205);
  assert.equal(page.records.length, 500);
  assert.equal(page.remaining, 705);

  state = showMoreListRecords(state);
  page = listWindow(visibleRecords(state), state);
  assert.equal(page.showing, 1000);
  assert.equal(page.remaining, 205);
  assert.deepEqual(state.filters, buildViewState(largeArtifact).filters);

  state = showMoreListRecords(state);
  page = listWindow(visibleRecords(state), state);
  assert.equal(page.showing, 1205);
  assert.equal(page.remaining, 0);

  state = updateFilters(state, { query: "energy" });
  page = listWindow(visibleRecords(state), state);
  assert.equal(state.listLimit, 500);
  assert.equal(page.total, 402);
  assert.equal(page.showing, 402);
});

test("resolves representatives and trend spike badge", () => {
  const state = buildViewState(artifact);
  const topic = state.topicById.get(1);

  assert.deepEqual(
    representativeRecords(topic, artifact.data).map((record) => record.id),
    ["r1", "r2"],
  );
  assert.deepEqual(spikeBadge(topic), {
    text: "Spike 6.5 in 2025-12-08",
    bucket: "2025-12-08",
    score: 6.5,
  });
  assert.equal(spikeBadge(state.topicById.get(2)), null);
});

test("builds sparkline paths for empty, single, flat, and spike series", () => {
  assert.equal(trendSparklinePath([]), "");
  assert.equal(trendSparklinePath([{ count: 4 }], { width: 10, height: 6, padding: 1 }), "M 1 3 L 9 3");
  assert.equal(
    trendSparklinePath(
      [{ count: 3 }, { count: 3 }, { count: 3 }],
      { width: 10, height: 6, padding: 1 },
    ),
    "M 1 3 L 5 3 L 9 3",
  );
  assert.equal(
    trendSparklinePath(
      [{ count: 1 }, { count: 9 }, { count: 2 }],
      { width: 10, height: 6, padding: 1 },
    ),
    "M 1 5 L 5 1 L 9 4.5",
  );
});

test("applies date presets against the artifact time extent", () => {
  const state = buildViewState(artifact);
  assert.deepEqual(datePresetFilters(state.timeExtent, "7d"), {
    start: "2025-12-14",
    end: "2025-12-20",
  });
  assert.deepEqual(datePresetFilters(state.timeExtent, "90d"), {
    start: "2025-09-22",
    end: "2025-12-20",
  });
  assert.deepEqual(datePresetFilters(state.timeExtent, "all"), { start: "", end: "" });

  const filtered = applyDatePreset(state, "30d");
  assert.deepEqual(filtered.filters, {
    ...state.filters,
    start: "2025-11-21",
    end: "2025-12-20",
  });
});
