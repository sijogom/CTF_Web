import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/app/[locale]/community/[id]/page.tsx", import.meta.url), "utf8");
const file = ts.createSourceFile("community.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const buttons = [];
function visit(node) {
  if (ts.isJsxElement(node) && node.openingElement.tagName.getText(file) === "button") buttons.push(node);
  ts.forEachChild(node, visit);
}
visit(file);
function classes(element) {
  const attribute = element.openingElement.attributes.properties.find((item) => ts.isJsxAttribute(item) && item.name.getText(file) === "className");
  assert(attribute && ts.isStringLiteral(attribute.initializer));
  return new Set(attribute.initializer.text.split(/\s+/));
}
function ancestors(node) {
  const found = [];
  for (let parent = node.parent; parent; parent = parent.parent) {
    if (ts.isJsxElement(parent)) found.push(parent);
  }
  return found;
}
function handler(button) {
  const attribute = button.openingElement.attributes.properties.find((item) => ts.isJsxAttribute(item) && item.name.getText(file) === "onClick");
  if (!attribute || !ts.isJsxExpression(attribute.initializer)) return null;
  const expression = attribute.initializer.expression;
  return ts.isArrowFunction(expression) && ts.isCallExpression(expression.body) ? expression.body : null;
}
const commentActions = buttons.filter((button) => {
  const call = handler(button);
  return call && (call.expression.getText(file) === "startEditComment"
    || (call.expression.getText(file) === "requestContentRemoval" && call.arguments[0]?.getText(file) === '"comment"'));
});

test("actual comment action labels do not shrink into single-character columns", () => {
  assert.equal(commentActions.length, 3);
  for (const button of commentActions) {
    assert(classes(button).has("whitespace-nowrap"));
    assert(classes(button).has("shrink-0"));
    const parents = ancestors(button).filter((node) => node.openingElement.tagName.getText(file) === "div");
    assert(classes(parents[0]).has("shrink-0"));
    assert(classes(parents[1]).has("flex-wrap"));
    assert(classes(parents[1]).has("gap-y-2"));
  }
});

test("comment edit and removal controls retain their exact target handlers", () => {
  const calls = commentActions.map(handler);
  assert.equal(calls.filter((call) => call.expression.getText(file) === "startEditComment" && call.arguments[0].getText(file) === "comment").length, 1);
  assert.equal(calls.filter((call) => call.expression.getText(file) === "requestContentRemoval" && call.arguments[1].getText(file) === "comment.id").length, 2);
});
