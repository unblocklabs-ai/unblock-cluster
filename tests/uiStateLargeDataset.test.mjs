import assert from "node:assert/strict";
import test from "node:test";

import {
  capLegendClusters,
  classifyLongValue,
  humanizeFieldLabel,
  inferFieldCategory,
  inferFieldGroups,
  parseTokenList,
  recordMatchesQuery,
} from "../src/uiState.js";

test("legend clusters can be capped with overflow metadata", () => {
  const clusters = Array.from({ length: 15 }, (_, index) => ({
    id: String(index),
    name: `Cluster ${index}`,
    items: new Array(index + 1).fill(null),
  }));

  const cappedDefault = capLegendClusters(clusters);
  assert.equal(cappedDefault.visibleClusters.length, 12);
  assert.equal(cappedDefault.overflowCount, 3);
  assert.equal(cappedDefault.overflowLabel, "+3 more");
  assert.equal(cappedDefault.hiddenCount, 3);
  assert.equal(cappedDefault.hasOverflow, true);

  const cappedCustom = capLegendClusters(clusters, { maxVisible: 8 });
  assert.equal(cappedCustom.visibleClusters.length, 8);
  assert.equal(cappedCustom.overflowCount, 7);
  assert.equal(cappedCustom.visibleClusters[0].id, "0");
  assert.equal(cappedCustom.overflowClusters[0].id, "8");
  assert.equal(cappedCustom.overflowLabel, "+7 more");
});

test("field labels are humanized for compact uppercase keys", () => {
  assert.equal(humanizeFieldLabel("HASCANCELLATION"), "Has cancellation");
  assert.equal(humanizeFieldLabel("ORDERSKUS"), "Order SKUs");
  assert.equal(humanizeFieldLabel("CUSTOMERTAGS"), "Customer tags");
});

test("comma-separated values are parsed into individual tokens", () => {
  const skuValue = "A1, B2, C3, \"D,4\", \"E,5\"";
  const parsed = parseTokenList(skuValue);
  assert.deepEqual(parsed.tokens, ["A1", "B2", "C3", "D,4", "E,5"]);
  assert.equal(parsed.totalCount, 5);
  assert.equal(parsed.truncated, false);
});

test("token list parsing reports clipping metadata", () => {
  const parsed = parseTokenList(["red", "green", "blue", "yellow"], {
    maxTokens: 2,
  });
  assert.deepEqual(parsed.tokens, ["red", "green"]);
  assert.equal(parsed.totalCount, 4);
  assert.equal(parsed.truncated, true);
  assert.equal(parsed.overflowCount, 2);
});

test("long text values get clamp metadata for detail rendering", () => {
  assert.equal(classifyLongValue("short one-line text").clamp, false);
  assert.equal(
    classifyLongValue("a".repeat(280)).clamp,
    true,
    "long text should request clamping",
  );
  assert.equal(classifyLongValue("a".repeat(280)).clampLines, 4);

  const tokened = classifyLongValue("a,b,c,d,e,f,g");
  assert.equal(tokened.kind, "token-list");
  assert.equal(tokened.clamp, true);
  assert.equal(tokened.maxVisibleTokens, 6);
  assert.equal(tokened.tokens.length, 6);
});

test("field categories are inferred from naming patterns", () => {
  assert.equal(inferFieldCategory("customerEmail"), "customer");
  assert.equal(inferFieldCategory("issueType"), "ticket");
  assert.equal(inferFieldCategory("refundAmount"), "order");
  assert.equal(inferFieldCategory("sourceTicketId"), "system");
  assert.equal(inferFieldCategory("recordTitle"), "other");
});

test("fields are grouped by inferred category", () => {
  const groups = inferFieldGroups([
    "customerEmail",
    "issueType",
    "refundAmount",
    "x",
    "name",
  ]);
  assert.deepEqual(groups.customer, ["customerEmail"]);
  assert.deepEqual(groups.ticket, ["issueType"]);
  assert.deepEqual(groups.order, ["refundAmount"]);
  assert.deepEqual(groups.system, ["x"]);
  assert.deepEqual(groups.other, ["name"]);
});

test("record search is enriched across record fields", () => {
  const record = {
    id: "ops-001",
    title: "OAuth retry",
    summary: "Webhook timeout after deploy.",
    customerEmail: "support+desk@example.com",
    customPayload: {
      issueType: "refund request",
      tags: "A, B, C",
    },
    skus: ["SKU-1", "SKU-2"],
  };
  const dataset = { titleField: "title", detailField: "summary", recordIdField: "id" };

  assert.equal(recordMatchesQuery(record, dataset, "support"), true);
  assert.equal(recordMatchesQuery(record, dataset, "refund"), true);
  assert.equal(recordMatchesQuery(record, dataset, "oauth"), true);
  assert.equal(recordMatchesQuery(record, dataset, "sku-2"), true);
  assert.equal(recordMatchesQuery(record, dataset, "missing"), false);
});
