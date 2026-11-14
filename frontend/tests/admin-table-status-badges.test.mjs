import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const cases = [
  ["account state", "components/admin/users/UserTable.tsx", "user.is_active", "active", "inactive"],
  ["email verification", "components/admin/users/UserTable.tsx", "user.email_verified", "emailVerified", "emailNotVerified"],
  ["submission result", "app/[locale]/admin/submissions/page.tsx", "log.is_correct", "filterCorrect", "filterIncorrect"],
];

for (const [name, file, condition, positive, negative] of cases) {
  test(`${name} badge keeps the complete status on one line`, () => {
    const source = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
    const ast = ts.createSourceFile(file, source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
    assert.equal(ast.parseDiagnostics.length, 0);
    const badges = [];
    function visit(node) {
      if (ts.isJsxElement(node) && node.openingElement.tagName.getText(ast) === "span"
        && node.children.some((child) => ts.isJsxExpression(child) && child.expression
          && ts.isConditionalExpression(child.expression) && child.expression.condition.getText(ast) === condition)) badges.push(node);
      ts.forEachChild(node, visit);
    }
    visit(ast);
    assert.equal(badges.length, 1);
    const badge = badges[0];
    const className = badge.openingElement.attributes.properties.find((item) => ts.isJsxAttribute(item) && item.name.text === "className");
    assert(className && ts.isJsxExpression(className.initializer) && ts.isTemplateExpression(className.initializer.expression));
    const tokens = className.initializer.expression.head.text.trim().split(/\s+/);
    for (const token of ["inline-flex", "whitespace-nowrap", "rounded-full", "py-0.5", "text-xs"]) assert(tokens.includes(token), token);
    const status = badge.children.find((child) => ts.isJsxExpression(child) && child.expression && ts.isConditionalExpression(child.expression)).expression;
    assert.equal(status.whenTrue.getText(ast), `t("${positive}")`);
    assert.equal(status.whenFalse.getText(ast), `t("${negative}")`);
  });
}

test("submission tables keep readable columns inside their existing scroll containers", () => {
  const file = "app/[locale]/admin/submissions/page.tsx";
  const source = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
  const ast = ts.createSourceFile(file, source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  assert.equal(ast.parseDiagnostics.length, 0);
  const tables = [];
  function visit(node) {
    if (ts.isJsxElement(node) && node.openingElement.tagName.getText(ast) === "table") tables.push(node);
    ts.forEachChild(node, visit);
  }
  visit(ast);
  const logs = tables.filter((node) => node.getText(ast).includes("logs.map((log)"));
  assert.equal(logs.length, 1);
  const attribute = (node, name) => node.openingElement.attributes.properties.find((item) => ts.isJsxAttribute(item) && item.name.text === name)?.initializer?.text;
  assert.equal(attribute(logs[0], "className"), "w-full min-w-[56rem] text-sm");
  assert(ts.isJsxElement(logs[0].parent));
  assert(attribute(logs[0].parent, "className").split(/\s+/).includes("overflow-x-auto"));
  assert.equal((logs[0].getText(ast).match(/<th\s/g) ?? []).length, 6);
  const metrics = tables.filter((node) => node.getText(ast).includes("metrics.map((metric)"));
  assert.equal(metrics.length, 1);
  assert.equal(attribute(metrics[0], "className"), "w-full min-w-[56rem] text-sm");
  assert.equal((metrics[0].getText(ast).match(/<th\s/g) ?? []).length, 5);
  let metricContainer = metrics[0].parent;
  while (metricContainer && !ts.isJsxElement(metricContainer)) metricContainer = metricContainer.parent;
  assert(metricContainer);
  assert(attribute(metricContainer, "className").split(/\s+/).includes("overflow-x-auto"));
});
