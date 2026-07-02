import assert from "node:assert/strict";
import test from "node:test";

import {
  boundedInspectorWidth,
  recordImageUrlForField,
  savedInspectorWidth,
  selectionFromSearch,
  selectionSearchParams,
} from "../src/uiState.js";

const records = [
  { __index: 0, id: "r-007", image: "https://images.unsplash.com/photo.jpg" },
  { __index: 1, id: "r-008" },
  { __index: 2 },
];
const clusters = [{ id: "2", name: "French Bistro" }];
const baseUrl = "https://viewer.example/";

test("missing inspector width storage leaves the CSS default untouched", () => {
  assert.equal(savedInspectorWidth(null), null);
  assert.equal(savedInspectorWidth(""), null);
  assert.equal(savedInspectorWidth("420"), 420);
  assert.equal(savedInspectorWidth("not-a-width"), null);
});

test("inspector width stays inside usable desktop bounds", () => {
  assert.equal(boundedInspectorWidth(420, 1200), 420);
  assert.equal(boundedInspectorWidth(200, 1200), 320);
  assert.equal(boundedInspectorWidth(900, 1200), 620);
  assert.equal(boundedInspectorWidth(620, 700), 320);
});

test("selection state resolves from shareable URL params", () => {
  assert.deepEqual(selectionFromSearch("?cluster=French%20Bistro", records, clusters), {
    type: "cluster",
    value: clusters[0],
  });
  assert.deepEqual(selectionFromSearch("?record=r-007", records, clusters), {
    type: "record",
    value: records[0],
  });
  assert.deepEqual(selectionFromSearch("?recordIndex=2", records, clusters), {
    type: "record",
    value: records[2],
  });
});

test("selection state serializes without clobbering unrelated params", () => {
  const clusterParams = selectionSearchParams("foo=bar&record=r-007", {
    type: "cluster",
    value: clusters[0],
  });
  assert.equal(clusterParams.get("foo"), "bar");
  assert.equal(clusterParams.get("record"), null);
  assert.equal(clusterParams.get("cluster"), "French Bistro");

  const recordParams = selectionSearchParams("foo=bar&cluster=French%20Bistro", {
    type: "record",
    value: records[0],
  });
  assert.equal(recordParams.get("foo"), "bar");
  assert.equal(recordParams.get("cluster"), null);
  assert.equal(recordParams.get("record"), "r-007");
});

test("record thumbnail URLs come from the trusted image policy", () => {
  assert.equal(
    recordImageUrlForField(records[0], "image", baseUrl),
    records[0].image,
  );
  assert.equal(
    recordImageUrlForField(
      { image: "https://attacker.example/photo.jpg" },
      "image",
      baseUrl,
    ),
    null,
  );
});
