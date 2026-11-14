import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { KNOWLEDGE_RELEASE_STAGE, knowledgeExtensionsEnabled } from "../src/lib/knowledge-release-stage.mjs";

test("core release keeps extension UI inactive", () => {
  assert.equal(knowledgeExtensionsEnabled("core"), false);
});

test("extended release activates the verified extension UI", () => {
  assert.equal(knowledgeExtensionsEnabled("extended"), true);
});

test("unknown release stages fail closed", () => {
  for (const value of [undefined, null, "", "unknown", true]) {
    if (value === undefined) continue;
    assert.equal(knowledgeExtensionsEnabled(value), false);
  }
  assert.ok(["core", "extended"].includes(KNOWLEDGE_RELEASE_STAGE));
  assert.equal(knowledgeExtensionsEnabled(), KNOWLEDGE_RELEASE_STAGE === "extended");
});

test("core and unknown releases block the actual bulk handler before any request or draft mutation", async () => {
  const source = readFileSync(new URL("../src/app/[locale]/admin/knowledge-relations/page.tsx", import.meta.url), "utf8");
  const tree = ts.createSourceFile("knowledge-relations.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  const matches = [];
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === "reviewBulk") matches.push(node.initializer);
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.equal(matches.length, 1);
  for (const stage of ["core", "unknown"]) {
    const mutations = [];
    const context = {
      knowledgeExtensionsEnabled: () => knowledgeExtensionsEnabled(stage),
      isSuperAdmin: true, reviewBusyRef: { current: false }, selectedProposalIds: ["fixture"],
      apiFetch: () => mutations.push("request"),
    };
    const program = ts.transpileModule(`globalThis.review = ${matches[0].getText(tree)}`, {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
    }).outputText;
    vm.runInNewContext(program, context, { timeout: 1000 });
    await context.review("approve");
    assert.deepEqual(mutations, []);
    assert.equal(context.reviewBusyRef.current, false);
  }
});
