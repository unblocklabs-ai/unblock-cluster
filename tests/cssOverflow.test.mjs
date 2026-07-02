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
});

test("inspector has a desktop resize handle", () => {
  assertDeclaration(".inspector-resizer", "cursor", "col-resize");
  assertDeclaration(".inspector-resizer", "touch-action", "none");
  assertDeclaration(".inspector-resizer", "width", "12px");
});
