import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/components/wargame/LearningPathPanel.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("LearningPathPanel.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
assert.equal(ast.parseDiagnostics.length, 0);

function elementsWithTag(tag) {
  const matches = [];
  function visit(node) {
    if (ts.isJsxOpeningElement(node) && node.tagName.getText(ast) === tag) matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(ast);
  return matches;
}

function attribute(element, name) {
  return element.attributes.properties.find((item) => ts.isJsxAttribute(item)
    && item.name.getText(ast) === name)?.initializer?.text;
}

test("learning path items retain their original border, background and padding", () => {
  const items = elementsWithTag("li");
  assert.equal(items.length, 1);
  assert.equal(attribute(items[0], "className"), "flex flex-col gap-3 rounded-lg border border-border bg-bg-primary p-3 sm:flex-row sm:items-center sm:justify-between");
});

test("the ready learning path retains the original panel surface", () => {
  const panels = elementsWithTag("section").filter((element) => attribute(element, "aria-labelledby") === "learning-path-title");
  assert.equal(panels.length, 1);
  assert.equal(attribute(panels[0], "className"), "rounded-xl border border-border bg-bg-surface p-5");
});
