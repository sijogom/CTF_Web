import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const product = readFileSync(new URL("../src/app/[locale]/wargame/[id]/page.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("wargame-page.tsx", product, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
assert.equal(ast.parseDiagnostics.length, 0);
let callback;
let visibleHints;
function visit(node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(ast) === "unlockHint") callback = node.initializer.getText(ast);
  if (ts.isVariableDeclaration(node) && node.name.getText(ast) === "visibleUnlockedHints") visibleHints = node.initializer.getText(ast);
  ts.forEachChild(node, visit);
}
visit(ast);
assert(callback);

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness() {
  const response = deferred();
  const calls = [], toasts = [], loading = [], refreshes = [];
  const owner = Object.freeze({ ownerId: "A", epoch: 1 });
  let currentOwner = true, hints = {};
  const context = {
    hintLoading: null, hintRequestRef: { current: 0 }, mountedRef: { current: true },
    hintViewRef: { current: { generation: 1 } }, user: { id: "A" }, challengeId: 7,
    unlockedHintViewRef: { current: 0 },
    captureApiRequestOwner: (id) => { assert.equal(id, "A"); return owner; },
    isCurrentApiRequestOwner: (captured) => captured === owner && currentOwner,
    apiFetch: async (pathname, options) => { calls.push({ pathname, options }); return response.promise; },
    refresh: async () => { refreshes.push(context.user.id); },
    setHintLoading: (value) => loading.push(value), setUnlockedHints: (update) => { hints = update(hints); },
    toast: (...args) => toasts.push(args), ApiError: class ApiError extends Error {}, t: (key) => key,
  };
  vm.runInNewContext(ts.transpileModule(`globalThis.unlock = ${callback};`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText, context, { timeout: 1000 });
  return { context, response, calls, toasts, loading, refreshes, owner, hints: () => hints,
    changeOwner() { currentOwner = false; context.user = { id: "B" }; } };
}

test("confirmed current-owner hint refreshes Auth stats once without changing the reward flow", async () => {
  const h = harness();
  const pending = h.context.unlock(9);
  assert.equal(h.calls[0].pathname, "/api/learn/challenges/7/hints/9/unlock");
  assert.equal(h.calls[0].options.requestOwner, h.owner);
  assert.equal(h.refreshes.length, 0);
  h.response.resolve({ content: "owned hint" });
  await pending;
  assert.equal(h.hints()[9], "owned hint");
  assert.deepEqual(h.refreshes, ["A"]);
  assert.deepEqual(h.loading, [9, null]);
});

test("failed hint does not refresh stats or pretend the charge succeeded", async () => {
  const h = harness();
  const pending = h.context.unlock(9);
  h.response.reject(new Error("request failed"));
  await pending;
  assert.equal(h.refreshes.length, 0);
  assert.equal(Object.keys(h.hints()).length, 0);
  assert.equal(h.toasts.length, 1);
});

for (const change of ["owner", "route", "unmount", "superseded"]) {
  test(`late hint after ${change} never refreshes the current account or displays old content`, async () => {
    const h = harness();
    const pending = h.context.unlock(9);
    if (change === "owner") h.changeOwner();
    if (change === "route") h.context.hintViewRef.current.generation += 1;
    if (change === "unmount") h.context.mountedRef.current = false;
    if (change === "superseded") h.context.hintRequestRef.current += 1;
    h.response.resolve({ content: "old owner hint" });
    await pending;
    assert.equal(h.refreshes.length, 0);
    assert.equal(Object.keys(h.hints()).length, 0);
    assert.equal(h.toasts.length, 0);
  });
}

test("Auth refresh failure preserves the already confirmed hint and does not resubmit", async () => {
  const h = harness();
  h.context.refresh = async () => { h.refreshes.push("A"); throw new Error("Auth unavailable"); };
  const pending = h.context.unlock(9);
  h.response.resolve({ content: "confirmed hint" });
  await pending;
  assert.equal(h.hints()[9], "confirmed hint");
  assert.equal(h.calls.length, 1);
  assert.equal(h.refreshes.length, 1);
  assert.equal(h.toasts.length, 0);
});

test("an unavailable session is rejected before a hint POST or stats refresh", async () => {
  const h = harness();
  h.context.captureApiRequestOwner = () => { throw new h.context.ApiError("session unavailable"); };
  await h.context.unlock(9);
  assert.equal(h.calls.length, 0);
  assert.equal(h.refreshes.length, 0);
});

test("an already displayed hint is hidden immediately when the view owner changes, before effects run", () => {
  const context = { unlockedHintViewRef: { current: 1 }, hintViewRef: { current: { generation: 1 } }, unlockedHints: { 9: "A hint" } };
  vm.runInNewContext(`globalThis.visible = ${visibleHints};`, context);
  assert.equal(context.visible[9], "A hint");
  context.hintViewRef.current.generation = 2;
  vm.runInNewContext(`globalThis.visible = ${visibleHints};`, context);
  assert.equal(Object.keys(context.visible).length, 0);
});
