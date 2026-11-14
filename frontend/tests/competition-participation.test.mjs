import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const text = readFileSync(new URL("../src/app/[locale]/ctf/[id]/page.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("competition-page.tsx", text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
assert.equal(ast.parseDiagnostics.length, 0);
const page = ast.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "CompetitionDetailPage");
assert.ok(page?.body);
const names = ["assignmentState", "isJoined", "needsCategorySelection", "canAccessChallenges"];
const declarations = page.body.statements.filter(ts.isVariableStatement).flatMap((node) =>
  node.declarationList.declarations.filter((declaration) => names.includes(declaration.name.getText(ast)))
);
assert.deepEqual(declarations.map((node) => node.name.getText(ast)), names);
const source = declarations.map((node) => `const ${node.getText(ast)};`).join("\n");
const script = ts.transpileModule(`${source}\nresult = { ${names.join(", ")} };`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText;
function participation(comp) {
  const context = { comp };
  vm.runInNewContext(script, context, { timeout: 1000 });
  return JSON.parse(JSON.stringify(context.result));
}

test("an upcoming participant stays joined before challenge assignment", () => {
  assert.deepEqual(participation({ joined: true, assignment: { state: "not_joined" } }), {
    assignmentState: "not_joined", isJoined: true, needsCategorySelection: false, canAccessChallenges: false,
  });
});

test("an unjoined visitor does not gain enrollment or challenge access", () => {
  assert.deepEqual(participation({ joined: false, assignment: { state: "not_joined" } }), {
    assignmentState: "not_joined", isJoined: false, needsCategorySelection: false, canAccessChallenges: false,
  });
});

test("a joined participant must still select a category before seeing challenges", () => {
  assert.deepEqual(participation({ joined: true, assignment: { state: "select_category" } }), {
    assignmentState: "select_category", isJoined: true, needsCategorySelection: true, canAccessChallenges: false,
  });
});

test("assigned participants retain existing challenge access", () => {
  assert.deepEqual(participation({ joined: true, assignment: { state: "assigned" } }), {
    assignmentState: "assigned", isJoined: true, needsCategorySelection: false, canAccessChallenges: true,
  });
});

test("an explicit unjoined response is not replaced by the assignment fallback", () => {
  assert.equal(participation({ joined: false, assignment: { state: "assigned" } }).isJoined, false);
});

test("legacy responses retain their assignment and joined fallbacks", () => {
  assert.equal(participation({ assignment: { state: "assigned" } }).isJoined, true);
  assert.equal(participation({ assignment: { state: "not_joined" } }).isJoined, false);
  assert.equal(participation({ joined: true }).isJoined, true);
  assert.equal(participation(null).isJoined, false);
});
