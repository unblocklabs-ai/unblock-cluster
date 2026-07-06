import assert from "node:assert/strict";
import test from "node:test";

import {
  backToTopic,
  applyDatePreset,
  buildViewState,
  clearTopicSelection,
  datePresetFilters,
  listRecordTitleCell,
  listTopicCell,
  listWindow,
  noiseRecords,
  parseViewParams,
  representativeRecords,
  sampleNoiseRecordIds,
  selectNoise,
  selectRecord,
  selectTopic,
  selectedTopicExtentRecords,
  showMoreListRecords,
  sentimentCell,
  spikeBadge,
  topicPanelTopics,
  trendSparklinePartialPath,
  trendSparklinePath,
  trendSparklineParts,
  trendSparklineTitle,
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

const noiseArtifact = {
  ...artifact,
  noise: { noiseCount: 4, noiseRatio: 0.4 },
  data: [
    ...artifact.data,
    {
      id: "n1",
      recordId: "noise-1",
      title: "One-off note",
      customerText: "Individual context",
      sourceType: "support",
      timestamp: "2025-12-21T00:00:00Z",
      clusterId: -1,
      isNoise: true,
    },
    {
      id: "n2",
      recordId: "noise-2",
      title: "Another one-off",
      customerText: "No shared theme",
      sourceType: "review",
      timestamp: "2025-12-22T00:00:00Z",
      clusterId: -1,
      isNoise: true,
    },
    {
      id: "n3",
      recordId: "noise-3",
      title: "Edge case",
      customerText: "Different individual issue",
      sourceType: "support",
      timestamp: "2025-12-23T00:00:00Z",
      clusterId: -1,
      isNoise: true,
    },
  ],
};

function sequenceRng(values) {
  let index = 0;
  return () => values[index++ % values.length];
}

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
  assert.equal(state.minRecordTimestamp, Date.parse("2025-06-01T00:00:00Z"));
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

test("selects noise with seeded sampling and noise-only spatial extent", () => {
  const state = buildViewState(noiseArtifact);
  const selected = selectNoise(state, visibleRecords(state), {
    limit: 2,
    rng: sequenceRng([0.7, 0.2, 0.5]),
  });

  assert.equal(selected.selectedTopicId, -1);
  assert.equal(selected.selectedRecordId, null);
  assert.deepEqual(selected.noiseSampleIds, ["n2", "n3"]);
  assert.deepEqual(
    noiseRecords(visibleRecords(selected)).map((record) => record.id),
    ["n1", "n2", "n3"],
  );
  assert.deepEqual(
    selectedTopicExtentRecords(selected).map((record) => record.id),
    ["n1", "n2", "n3"],
  );
});

test("noise selection resamples on repeated clicks", () => {
  const state = buildViewState(noiseArtifact);
  const first = selectNoise(state, visibleRecords(state), {
    limit: 2,
    rng: sequenceRng([0.1, 0.2, 0.3]),
  });
  const second = selectNoise(first, visibleRecords(first), {
    limit: 2,
    rng: sequenceRng([0.3, 0.1, 0.2]),
  });

  assert.deepEqual(first.noiseSampleIds, ["n1", "n2"]);
  assert.deepEqual(second.noiseSampleIds, ["n2", "n3"]);
});

test("samples noise records directly with an injectable rng", () => {
  assert.deepEqual(
    sampleNoiseRecordIds(noiseArtifact.data, {
      limit: 20,
      rng: sequenceRng([0.9, 0.1, 0.3]),
    }),
    ["n2", "n3", "n1"],
  );
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
  assert.equal(spikeBadge({ trend: { spikeScore: 0, topBucket: null } }), null);
  assert.equal(spikeBadge({ trend: { spikeScore: -1, topBucket: "2025-01-01" } }), null);
});

test("renders list topic cells with dots, noise labels, and truncation classes", () => {
  const state = buildViewState(noiseArtifact);
  const topicCell = listTopicCell(noiseArtifact.data[0], state.topicById.get(1));
  assert.match(topicCell, /class="topic-cell-content"/);
  assert.match(topicCell, /class="topic-swatch list-topic-swatch"/);
  assert.match(topicCell, /class="topic-cell-label"/);
  assert.match(topicCell, />December spike</);

  const noiseCell = listTopicCell(noiseArtifact.data.find((record) => record.id === "n1"), null);
  assert.match(noiseCell, />Noise</);
  assert.match(noiseCell, /noise-swatch/);
  assert.doesNotMatch(noiseCell, /Topic unknown/);

  const titleCell = listRecordTitleCell({ title: "A long title", recordId: "rec-1" });
  assert.match(titleCell, /class="single-line"/);
  assert.match(titleCell, /title="A long title"/);
});

test("renders compact sentiment icons and passthrough fallbacks", () => {
  assert.equal(
    sentimentCell("positive"),
    '<span class="sentiment-icon sentiment-positive" title="positive" aria-label="positive">●</span>',
  );
  assert.equal(
    sentimentCell("neutral"),
    '<span class="sentiment-icon sentiment-neutral" title="neutral" aria-label="neutral">●</span>',
  );
  assert.equal(
    sentimentCell("negative"),
    '<span class="sentiment-icon sentiment-negative" title="negative" aria-label="negative">●</span>',
  );
  assert.equal(
    sentimentCell("mixed"),
    '<span class="sentiment-text" title="mixed">mixed</span>',
  );
  assert.equal(sentimentCell(""), "");
  assert.equal(sentimentCell(null), "");
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

test("trims leading zero buckets from sparkline presentation", () => {
  assert.equal(
    trendSparklinePath(
      [
        { bucketStart: "2025-05-04", count: 0 },
        { bucketStart: "2025-05-11", count: 0 },
        { bucketStart: "2025-05-18", count: 5 },
        { bucketStart: "2025-05-25", count: 10 },
      ],
      { width: 10, height: 6, padding: 1 },
    ),
    "M 1 5 L 9 1",
  );
  assert.equal(
    trendSparklinePath(
      [
        { bucketStart: "2025-05-04", count: 4 },
        { bucketStart: "2025-05-11", count: 8 },
      ],
      { width: 10, height: 6, padding: 1 },
    ),
    "M 1 5 L 9 1",
  );
});

test("keeps all-zero and single-nonzero sparklines flat", () => {
  assert.equal(
    trendSparklinePath(
      [
        { bucketStart: "2025-05-04", count: 0 },
        { bucketStart: "2025-05-11", count: 0 },
        { bucketStart: "2025-05-18", count: 0 },
      ],
      { width: 10, height: 6, padding: 1 },
    ),
    "M 1 3 L 5 3 L 9 3",
  );
  assert.equal(
    trendSparklinePath(
      [
        { bucketStart: "2025-05-04", count: 0 },
        { bucketStart: "2025-05-11", count: 7 },
      ],
      { width: 10, height: 6, padding: 1 },
    ),
    "M 1 3 L 9 3",
  );
});

test("derives sparkline y-domain from trimmed values", () => {
  assert.equal(
    trendSparklinePath(
      [
        { bucketStart: "2025-05-04", count: 0 },
        { bucketStart: "2025-05-11", count: 9 },
        { bucketStart: "2025-05-18", count: 10 },
      ],
      { width: 10, height: 6, padding: 1 },
    ),
    "M 1 5 L 9 1",
  );
});

test("marks incomplete final buckets as a separate partial segment", () => {
  const buckets = [
    { bucketStart: "2025-05-04", count: 2 },
    { bucketStart: "2025-05-11", count: 10 },
    { bucketStart: "2025-05-18", count: 4 },
  ];
  const options = {
    width: 10,
    height: 6,
    padding: 1,
    bucket: "week",
    maxRecordTimestamp: "2025-05-20T12:00:00Z",
  };
  assert.equal(trendSparklinePath(buckets, options), "M 1 5 L 5 1");
  assert.equal(trendSparklinePartialPath(buckets, options), "M 5 1 L 9 4");
  assert.equal(
    trendSparklinePartialPath(buckets, {
      ...options,
      maxRecordTimestamp: "2025-05-25T00:00:00Z",
    }),
    "",
  );
});

test("marks incomplete first buckets as a separate partial segment", () => {
  const buckets = [
    { bucketStart: "2025-05-04", count: 2 },
    { bucketStart: "2025-05-11", count: 10 },
    { bucketStart: "2025-05-18", count: 4 },
  ];
  const options = {
    width: 10,
    height: 6,
    padding: 1,
    bucket: "week",
    minRecordTimestamp: "2025-05-06T12:00:00Z",
  };

  assert.equal(trendSparklinePath(buckets, options), "M 5 1 L 9 4");
  assert.equal(trendSparklinePartialPath(buckets, options), "M 1 5 L 5 1");
  assert.deepEqual(trendSparklineParts(buckets, options).partialPoints, [{ x: 1, y: 5 }]);
  assert.equal(
    trendSparklinePartialPath(buckets, {
      ...options,
      minRecordTimestamp: "2025-05-04T00:00:00Z",
    }),
    "",
  );
});

test("handles both incomplete first and final sparkline buckets", () => {
  const buckets = [
    { bucketStart: "2025-05-04", count: 2 },
    { bucketStart: "2025-05-11", count: 10 },
    { bucketStart: "2025-05-18", count: 4 },
  ];
  const options = {
    width: 10,
    height: 6,
    padding: 1,
    bucket: "week",
    minRecordTimestamp: "2025-05-06T12:00:00Z",
    maxRecordTimestamp: "2025-05-20T12:00:00Z",
  };

  assert.equal(trendSparklinePath(buckets, options), "M 5 1");
  assert.equal(
    trendSparklinePartialPath(buckets, options),
    "M 1 5 L 5 1 M 5 1 L 9 4",
  );
  assert.deepEqual(trendSparklineParts(buckets, options).partialPoints, [
    { x: 1, y: 5 },
    { x: 9, y: 4 },
  ]);
  assert.equal(
    trendSparklineTitle(buckets, options),
    "(partial) May 4 – May 18 (partial) · peak 10 (May 11)",
  );
});

test("builds sparkline title from the trimmed range", () => {
  assert.equal(
    trendSparklineTitle(
      [
        { bucketStart: "2025-05-04", count: 0 },
        { bucketStart: "2025-05-11", count: 32 },
        { bucketStart: "2025-05-18", count: 8 },
      ],
    ),
    "May 11 – May 18 · peak 32 (May 11)",
  );
  assert.equal(
    trendSparklineTitle(
      [
        { bucketStart: "2025-05-04", count: 0 },
        { bucketStart: "2025-05-11", count: 32 },
        { bucketStart: "2025-05-18", count: 8 },
      ],
      {
        bucket: "week",
        minRecordTimestamp: "2025-05-12T00:00:00Z",
      },
    ),
    "(partial) May 11 – May 18 · peak 32 (May 11)",
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
