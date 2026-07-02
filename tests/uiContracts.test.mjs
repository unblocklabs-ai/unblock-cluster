import assert from "node:assert/strict";
import test from "node:test";

import {
  boundedInspectorWidth,
  recordIdentity,
  recordImageUrlForField,
  recordMatchesQuery,
  savedInspectorWidth,
  selectionFromSearch,
  selectionSearchParams,
  tokenStorageKey,
} from "../src/uiState.js";

const records = [
  { __index: 0, id: "r-007", image: "https://images.unsplash.com/photo.jpg" },
  { __index: 1, id: "r-008" },
  { __index: 2 },
];
const clusters = [{ id: "2", name: "French Bistro" }];
const baseUrl = "https://viewer.example/";
const ticketDataset = {
  recordIdField: "sourceTicketId",
  titleField: "name",
  detailField: "summary",
};

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

test("selection state uses configured record ids and ticket fallbacks", () => {
  const ticketRecords = [
    { __index: 0, id: "internal-1", sourceTicketId: "TICKET-42", name: "Alpha" },
    { __index: 1, ticketId: "TICKET-43", name: "Beta" },
  ];

  assert.equal(recordIdentity(ticketRecords[0], ticketDataset), "TICKET-42");
  assert.equal(recordIdentity(ticketRecords[1], {}), "TICKET-43");
  assert.equal(
    recordIdentity(
      { __index: 2, externalReference: "EXT-99", name: "Gamma" },
      { recordIdField: "externalReference" },
    ),
    "EXT-99",
  );
  assert.deepEqual(
    selectionFromSearch("?record=TICKET-42", ticketRecords, clusters, ticketDataset),
    {
      type: "record",
      value: ticketRecords[0],
    },
  );
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

  const ticketParams = selectionSearchParams(
    "foo=bar&cluster=French%20Bistro",
    {
      type: "record",
      value: { __index: 0, id: "internal-1", sourceTicketId: "TICKET-42" },
    },
    ticketDataset,
  );
  assert.equal(ticketParams.get("record"), "TICKET-42");
});

test("record query matches ids, title, and details", () => {
  const record = {
    sourceTicketId: "OPS-101",
    name: "OAuth callback failure",
    summary: "Token refresh fails after deploy.",
  };
  assert.equal(recordMatchesQuery(record, ticketDataset, "OPS-101"), true);
  assert.equal(recordMatchesQuery(record, ticketDataset, "callback"), true);
  assert.equal(recordMatchesQuery(record, ticketDataset, "refresh"), true);
  assert.equal(recordMatchesQuery(record, ticketDataset, "billing"), false);
});

test("token storage can be scoped to a data graph", () => {
  assert.equal(tokenStorageKey("dg_abc"), "dataGraphApiToken:dg_abc");
  assert.equal(tokenStorageKey(null), "dataGraphApiToken");
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
