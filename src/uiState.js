import { trustedImageUrl } from "./urlPolicy.js";

export const INSPECTOR_WIDTH_KEY = "dataAtlasInspectorWidth";
const INSPECTOR_MIN_WIDTH = 320;
const INSPECTOR_MAX_WIDTH = 620;
const DEFAULT_LEGEND_VISIBLE_LIMIT = 12;

export function capLegendClusters(clusters = [], options = {}) {
  const clustersArray = Array.isArray(clusters) ? clusters : [];
  const limit = Number.isFinite(options.maxVisible)
    ? Math.max(1, Math.floor(options.maxVisible))
    : DEFAULT_LEGEND_VISIBLE_LIMIT;
  const visibleClusters = clustersArray.slice(0, limit);
  const overflowClusters = clustersArray.slice(limit);
  return {
    visibleClusters,
    overflowClusters,
    overflowCount: overflowClusters.length,
    hiddenCount: overflowClusters.length,
    totalCount: clustersArray.length,
    overflowLabel:
      overflowClusters.length > 0 ? `+${overflowClusters.length} more` : "",
    hasOverflow: overflowClusters.length > 0,
  };
}

const fallbackRecordIdFields = [
  "id",
  "ticketId",
  "sourceTicketId",
  "sourceId",
  "issueId",
  "key",
];

export function savedInspectorWidth(rawValue) {
  if (rawValue === null || rawValue.trim() === "") return null;
  const width = Number(rawValue);
  return Number.isFinite(width) ? width : null;
}

export function boundedInspectorWidth(
  width,
  viewportWidth,
  minWidth = INSPECTOR_MIN_WIDTH,
  maxWidth = INSPECTOR_MAX_WIDTH,
) {
  const availableMaxWidth = Math.min(
    maxWidth,
    Math.max(minWidth, viewportWidth - 420),
  );
  return Math.max(minWidth, Math.min(availableMaxWidth, width));
}

export function recordIdentity(record, dataset = {}) {
  const fields = [
    dataset.recordIdField,
    ...fallbackRecordIdFields,
  ].filter(Boolean);
  for (const field of fields) {
    const value = record?.[field];
    if (value !== undefined && value !== null && String(value) !== "") {
      return String(value);
    }
  }
  return null;
}

export function parseTokenList(value, options = {}) {
  const tokenLimit = Number.isFinite(options.maxTokens) ? options.maxTokens : null;
  const parsed = [];

  const addToken = (token) => {
    if (token === undefined || token === null) return;
    const cleaned = String(token).trim();
    if (!cleaned) return;
    parsed.push(cleaned);
  };

  const tokenize = (input) => {
    if (input == null) return;
    if (Array.isArray(input)) {
      input.forEach((item) => tokenize(item));
      return;
    }
    if (typeof input === "string" && input.includes(",")) {
      let current = "";
      let inSingleQuote = false;
      let inDoubleQuote = false;
      for (const char of input) {
        if (char === "'" && !inDoubleQuote) {
          inSingleQuote = !inSingleQuote;
          continue;
        }
        if (char === `"` && !inSingleQuote) {
          inDoubleQuote = !inDoubleQuote;
          continue;
        }
        if (char === "," && !inSingleQuote && !inDoubleQuote) {
          addToken(current);
          current = "";
          continue;
        }
        current += char;
      }
      addToken(current);
      return;
    }
    if (typeof input === "object") {
      for (const token of Object.values(input)) {
        tokenize(token);
      }
      return;
    }
    addToken(input);
  };

  tokenize(value);
  const finalTokens =
    tokenLimit === null ? parsed : parsed.slice(0, tokenLimit);
  return {
    tokens: finalTokens,
    totalCount: parsed.length,
    truncated: tokenLimit !== null && parsed.length > tokenLimit,
    overflowCount:
      tokenLimit !== null && parsed.length > tokenLimit
        ? parsed.length - tokenLimit
        : 0,
  };
}

const LONG_VALUE_DEFAULTS = {
  shortTextLength: 120,
  mediumTextLength: 260,
  longTextLines: 4,
  tokenListMaxVisible: 6,
};

export function classifyLongValue(value, options = {}) {
  const config = { ...LONG_VALUE_DEFAULTS, ...options };
  const isEmpty = value === undefined || value === null || value === "";
  if (isEmpty) {
    return {
      kind: "empty",
      clamp: false,
      showMore: false,
      tokens: [],
      reason: "empty",
    };
  }

  if (typeof value === "boolean") {
    return {
      kind: "boolean",
      clamp: false,
      showMore: false,
      value: value ? "true" : "false",
      reason: "boolean",
    };
  }

  if (typeof value === "number" || typeof value === "bigint") {
    return {
      kind: "number",
      clamp: false,
      showMore: false,
      text: String(value),
      reason: "number",
    };
  }

  if (Array.isArray(value) || (typeof value === "string" && value.includes(","))) {
    const parsedTokens = parseTokenList(value, {
      maxTokens: config.tokenListMaxVisible + 1,
    });
    const visible = parsedTokens.tokens.slice(0, config.tokenListMaxVisible);
    return {
      kind: "token-list",
      text: visible.join(", "),
      tokens: visible,
      clamp: parsedTokens.totalCount > config.tokenListMaxVisible,
      showMore: parsedTokens.totalCount > config.tokenListMaxVisible,
      tokenCount: parsedTokens.totalCount,
      maxVisibleTokens: config.tokenListMaxVisible,
      overflowCount:
        parsedTokens.totalCount > config.tokenListMaxVisible
          ? parsedTokens.totalCount - config.tokenListMaxVisible
          : 0,
      reason: parsedTokens.totalCount > config.tokenListMaxVisible ? "token-overflow" : "token-list",
    };
  }

  const text = typeof value === "string" ? value : JSON.stringify(value);
  if (text.length <= config.shortTextLength) {
    return {
      kind: "text",
      clamp: false,
      showMore: false,
      text,
      reason: "short-text",
    };
  }

  if (text.length <= config.mediumTextLength) {
    return {
      kind: "text",
      clamp: false,
      showMore: false,
      text,
      reason: "medium-text",
    };
  }

  return {
    kind: "text",
    clamp: true,
    showMore: true,
    text,
    clampLines: config.longTextLines,
    reason: "long-text",
  };
}

const FIELD_CATEGORY_PATTERNS = [
  {
    category: "customer",
    match: (field) =>
      /(^|_)customer/i.test(field) ||
      /(^|_)email/i.test(field) ||
      /(^|_)name/i.test(field) && /customer/i.test(field),
  },
  {
    category: "ticket",
    match: (field) =>
      /(^|_)issue/i.test(field) ||
      /(^|_)(message|msg)/i.test(field) ||
      /(^|_)ticket/i.test(field) &&
        !/ticketid/i.test(field),
  },
  {
    category: "order",
    match: (field) =>
      /(^|_)order/i.test(field) ||
      /(^|_)sku/i.test(field) ||
      /refund/i.test(field) ||
      /cancellation/i.test(field) ||
      /subscription/i.test(field),
  },
];

export function inferFieldCategory(field = "") {
  const fieldValue = String(field);
  if (!fieldValue) return "other";
  if (fieldValue.startsWith("__")) return "system";
  const systemKeys = new Set([
    "id",
    "__index",
    "x",
    "y",
    "clusterId",
    "groupValue",
    "clusterLabel",
    "source",
    "image",
  ]);
  if (systemKeys.has(fieldValue)) return "system";
  if (/(^|_)source(id|TicketId)?$/i.test(fieldValue)) return "system";
  if (/(^|_)created|updated|ingested/i.test(fieldValue)) return "system";
  for (const rule of FIELD_CATEGORY_PATTERNS) {
    if (rule.match(fieldValue)) return rule.category;
  }
  return "other";
}

export function inferFieldGroups(fields = []) {
  const groups = {
    customer: [],
    ticket: [],
    order: [],
    system: [],
    other: [],
  };
  for (const field of fields) {
    const category = inferFieldCategory(field);
    groups[category].push(field);
  }
  return groups;
}

const HUMAN_LABEL_ACRONYMS = new Set([
  "api",
  "ip",
  "uuid",
  "uid",
  "url",
]);
const HUMAN_LABEL_ACRONYM_DISPLAY = {
  id: "ID",
  ids: "IDs",
  sku: "SKU",
  skus: "SKUs",
};
const KNOWN_TOKEN_WORDS = [
  "customer",
  "ticket",
  "issue",
  "message",
  "messages",
  "inbound",
  "outbound",
  "refund",
  "refunds",
  "cancellation",
  "cancellations",
  "subscription",
  "order",
  "sku",
  "skus",
  "tag",
  "tags",
  "email",
  "id",
  "name",
  "type",
  "value",
  "status",
  "created",
  "updated",
  "source",
  "title",
  "detail",
  "summary",
  "has",
  "image",
  "url",
];

function titleCaseToken(token, index = 0) {
  const lowerToken = String(token).toLowerCase();
  if (HUMAN_LABEL_ACRONYM_DISPLAY[lowerToken]) {
    return HUMAN_LABEL_ACRONYM_DISPLAY[lowerToken];
  }
  if (HUMAN_LABEL_ACRONYMS.has(lowerToken)) return token.toUpperCase();
  if (index === 0) {
    return `${lowerToken.charAt(0).toUpperCase()}${lowerToken.slice(1)}`;
  }
  return lowerToken;
}

function splitAllCapsToken(token) {
  const lower = String(token).toLowerCase();
  const memo = new Map();
  const orderedWords = [...KNOWN_TOKEN_WORDS].sort((a, b) => b.length - a.length);

  const scan = (start) => {
    if (start === lower.length) return [];
    if (memo.has(start)) return memo.get(start);

    let best = null;
    for (const word of orderedWords) {
      if (!lower.startsWith(word, start)) continue;
      const tail = scan(start + word.length);
      if (tail !== null) {
        best = [word, ...tail];
        break;
      }
    }
    if (best === null) {
      const defaultSlice = lower.slice(start);
      best = [defaultSlice];
    }
    memo.set(start, best);
    return best;
  };

  return scan(0);
}

export function humanizeFieldLabel(field = "") {
  const label = String(field).trim();
  if (!label) return "";

  const cleaned = label
    .replace(/[_-]+/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
    .trim();

  let tokens = cleaned.length
    ? cleaned.split(/\s+/).filter(Boolean)
    : [label];

  if (
    tokens.length === 1 &&
    /^[A-Z0-9]+$/.test(tokens[0]) &&
    tokens[0] !== tokens[0].toLowerCase()
  ) {
    tokens = splitAllCapsToken(tokens[0]);
  }

  return tokens.map((token, index) => titleCaseToken(token, index)).join(" ");
}

function collectSearchValues(value, values = [], depth = 0) {
  if (value === undefined || value === null || depth > 3) return values;
  if (Array.isArray(value)) {
    for (const item of value) collectSearchValues(item, values, depth + 1);
    return values;
  }
  if (typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      collectSearchValues(key, values, depth + 1);
      collectSearchValues(item, values, depth + 1);
    }
    return values;
  }
  values.push(String(value));
  return values;
}

export function recordMatchesQuery(record, dataset = {}, query = "") {
  const normalized = String(query || "").trim().toLocaleLowerCase();
  if (!normalized) return true;

  const fields = [
    dataset.recordIdField,
    ...fallbackRecordIdFields,
    dataset.titleField,
    dataset.detailField,
  ].filter(Boolean);

  const allFieldKeys = [
    ...new Set([
      ...fields,
      ...Object.keys(record || {}).filter(
        (field) =>
          field !== "__index" &&
          field !== "x" &&
          field !== "y" &&
          !field.startsWith("__"),
      ),
    ]),
  ];
  const values = allFieldKeys.flatMap((field) => {
    const value = record?.[field];
    if (value === undefined || value === null) return [];
    return collectSearchValues(value);
  });
  return values.some((value) =>
    String(value).toLocaleLowerCase().includes(normalized),
  );
}

export function selectionFromSearch(search, records, clusters, dataset = {}) {
  const params = new URLSearchParams(search);
  const recordParam = params.get("record");
  const recordIndexParam = params.get("recordIndex");
  const clusterParam = params.get("cluster");
  if (recordParam) {
    const record = records.find((entry) => recordIdentity(entry, dataset) === recordParam);
    if (record) return { type: "record", value: record };
  }
  if (recordIndexParam) {
    const record = records.find(
      (entry) => String(entry.__index) === recordIndexParam,
    );
    if (record) return { type: "record", value: record };
  }
  if (clusterParam) {
    const cluster = clusters.find(
      (entry) => entry.id === clusterParam || entry.name === clusterParam,
    );
    if (cluster) return { type: "cluster", value: cluster };
  }
  return null;
}

export function selectionSearchParams(currentSearch, selection, dataset = {}) {
  const params = new URLSearchParams(currentSearch);
  params.delete("cluster");
  params.delete("record");
  params.delete("recordIndex");
  if (selection?.type === "cluster") {
    params.set("cluster", selection.value.name);
  } else if (selection?.type === "record") {
    const record = selection.value;
    const identity = recordIdentity(record, dataset);
    if (identity) {
      params.set("record", identity);
    } else {
      params.set("recordIndex", String(record.__index));
    }
  }
  return params;
}

export function tokenStorageKey(graphId) {
  return graphId ? `dataGraphApiToken:${graphId}` : "dataGraphApiToken";
}

export function recordImageUrlForField(record, imageField, baseUrl) {
  return trustedImageUrl(imageField ? record?.[imageField] : null, baseUrl);
}
