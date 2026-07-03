import assert from "node:assert/strict";
import test from "node:test";

import {
  buildViewState,
  clearTopicSelection,
  parseViewParams,
  representativeRecords,
  selectTopic,
  spikeBadge,
  updateFilters,
  visibleRecords,
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
  });
  assert.deepEqual(
    parseViewParams("?graphId=grf_1&viewId=view_1&mode=list&topicId=2"),
    {
      graphId: "grf_1",
      viewId: "view_1",
      mode: "list",
      topicId: "2",
    },
  );
  assert.deepEqual(parseViewParams(""), {
    graphId: null,
    viewId: null,
    mode: null,
    topicId: null,
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

test("selects and clears topic state", () => {
  let state = selectTopic(buildViewState(artifact), 1);
  assert.equal(state.selectedTopicId, 1);
  assert.equal(state.filters.topicId, 1);

  state = clearTopicSelection(state);
  assert.equal(state.selectedTopicId, null);
  assert.equal(state.filters.topicId, "");
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
