import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const css = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

function declarationsFor(selector) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`));
  assert.ok(match, `missing CSS rule for ${selector}`);
  return match[1];
}

function assertDeclaration(selector, property, value) {
  const declarations = declarationsFor(selector);
  assert.match(
    declarations,
    new RegExp(`${property}\\s*:\\s*${value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`),
  );
}

test("side inspector and detail fields wrap long text", () => {
  assertDeclaration(
    "#app",
    "grid-template-columns",
    "minmax(0, 1fr) minmax(320px, var(--inspector-width))",
  );
  assertDeclaration(".inspector", "overflow-wrap", "anywhere");
  assertDeclaration(".inspector", "min-width", "0");
  assertDeclaration(".details", "min-width", "0");
  assertDeclaration(".field-table", "min-width", "0");
  assertDeclaration(".field-row", "min-width", "0");
  assertDeclaration(".field-row strong", "overflow-wrap", "anywhere");
  assertDeclaration(".field-row strong", "word-break", "break-word");
});

test("record grid and legend stay within narrow containers", () => {
  assertDeclaration(
    ".record-grid",
    "grid-template-columns",
    "repeat(auto-fill, minmax(min(100%, 300px), 1fr))",
  );
  assertDeclaration(".record-card", "min-width", "0");
  assertDeclaration(".legend-item", "max-width", "100%");
  assertDeclaration(".legend-item", "min-width", "0");
  assertDeclaration(".legend-item span:nth-child(2)", "text-overflow", "ellipsis");
  assertDeclaration(".legend", "max-height", "min(260px, 42vh)");
  assertDeclaration(".legend", "overflow-y", "auto");
});

test("legend overflow affordance and map controls have explicit interactions", () => {
  assertDeclaration(".legend-overflow", "display", "inline-flex");
  assertDeclaration(".legend-overflow", "border", "1px dashed rgba(184, 95, 54, 0.45)");
  assertDeclaration(".map-control", "width", "auto");
  assertDeclaration(".map-control", "height", "40px");
  assertDeclaration(".map-control-fit", "min-width", "84px");
  assertDeclaration(".map-control-clear", "min-width", "84px");
});

test("inspector has a desktop resize handle", () => {
  assertDeclaration(".inspector-resizer", "cursor", "col-resize");
  assertDeclaration(".inspector-resizer", "touch-action", "none");
  assertDeclaration(".inspector-resizer", "width", "12px");
});

test("drawer fields, clusters, and chips support dense, expandable detail sections", () => {
  assertDeclaration(".field-table", "max-height", "min(56vh, 520px)");
  assertDeclaration(".field-row strong", "-webkit-line-clamp", "var(--field-clamp-lines)");
  assertDeclaration(".field-row.is-expanded strong", "-webkit-line-clamp", "var(--field-clamp-lines-expanded)");
  assertDeclaration(".field-group", "border-top", "1px solid var(--line)");
  assertDeclaration(".field-group-title", "text-transform", "uppercase");
  assertDeclaration(".token-chips", "display", "flex");
  assertDeclaration(".token-chip", "border-radius", "999px");
  assertDeclaration(".token-chip.more", "border-style", "dashed");
});

test("hover preview, cluster browser, and selected-cluster emphasis classes exist", () => {
  assertDeclaration(".hover-preview", "position", "absolute");
  assertDeclaration(".hover-preview", "pointer-events", "none");
  assertDeclaration(".cluster-browser-backdrop", "position", "fixed");
  assertDeclaration(".cluster-browser-panel", "position", "fixed");
  assertDeclaration(".is-selected-cluster", "position", "relative");
});
