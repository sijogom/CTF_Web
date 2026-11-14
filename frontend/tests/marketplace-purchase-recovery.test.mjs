import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { parsePurchaseStatus } from "../src/lib/marketplace-settlement.mjs";

const source = readFileSync(new URL("../src/lib/use-marketplace-purchase.ts", import.meta.url), "utf8");
const syntax = ts.createSourceFile("use-marketplace-purchase.ts", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS);

function uniqueNode(predicate) {
  const matches = [];
  function visit(node) {
    if (predicate(node)) matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(syntax);
  assert.equal(matches.length, 1);
  return matches[0];
}

function callbackSource(name) {
  const declaration = uniqueNode((node) => ts.isVariableDeclaration(node) && node.name.getText(syntax) === name);
  assert.ok(ts.isCallExpression(declaration.initializer));
  return `const ${name} = ${declaration.initializer.arguments[0].getText(syntax)};`;
}

const matching = uniqueNode((node) => ts.isFunctionDeclaration(node) && node.name?.text === "matchingSnapshot");
// Product callback bodies are executed unchanged; only API and React state adapters are injected.
const program = ts.transpileModule([
  matching.getText(syntax), callbackSource("refreshStatus"), callbackSource("purchase"),
  "globalThis.callbacks = { refreshStatus, purchase };",
].join("\n"), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText;

function harness(statusPayload) {
  class ApiError extends Error {}
  const calls = [];
  const identity = "buyer:item";
  const context = {
    identity, itemId: "item", userId: "buyer", errorMessage: "purchase status unavailable",
    identityRef: { current: identity }, requestRef: { current: 0 }, purchaseInFlight: { current: false },
    snapshot: { identity, settlement: null, points: 1000, busy: false, error: null },
    ApiError, parsePurchaseStatus,
  };
  context.setSnapshot = (update) => { context.snapshot = update(context.snapshot); };
  context.apiFetch = async (path, options = {}) => {
    calls.push({ path, method: options.method ?? "GET", body: options.body });
    if (options.method === "POST") throw context.postError ?? new TypeError("injected lost purchase response");
    if (path.endsWith("/purchase-status")) {
      if (context.statusError) throw context.statusError;
      return context.statusBehavior ? context.statusBehavior() : statusPayload;
    }
    if (context.balanceError) throw context.balanceError;
    return { points: 863 };
  };
  vm.runInNewContext(program, context, { filename: "marketplace-purchase.actual-callbacks.js", timeout: 1000 });
  return { context, calls, ApiError, ...context.callbacks };
}

test("a lost POST reply followed by confirmed completion clears the transport error", async () => {
  const h = harness({ status: "completed", operation_id: "op-1", price_paid: 137 });
  const result = await h.purchase(137);
  assert.equal(result?.status, "completed");
  assert.equal(h.context.snapshot.settlement.purchased, true);
  assert.equal(h.context.snapshot.error, null);
  assert.equal(h.context.snapshot.points, 863);
  assert.equal(h.context.snapshot.busy, false);
  assert.equal(h.calls.filter((call) => call.method === "POST").length, 1);
});

test("a confirmed processing operation replaces the lost-response error without granting access", async () => {
  const h = harness({ status: "processing", operation_id: "op-1", retryable: true });
  const result = await h.purchase();
  assert.equal(result?.status, "processing");
  assert.equal(h.context.snapshot.error, null);
  assert.equal(h.context.snapshot.settlement.purchased, false);
  assert.equal(h.context.snapshot.settlement.operationId, "op-1");
  assert.equal(h.context.snapshot.busy, false);
});

test("a definitive rejection retains its server error and grants no purchase", async () => {
  const h = harness({ status: "rejected", failure_code: "insufficient_points" });
  h.context.postError = new h.ApiError("insufficient points");
  await h.purchase();
  assert.equal(h.context.snapshot.error, "insufficient points");
  assert.equal(h.context.snapshot.settlement.purchased, false);
  assert.equal(h.context.snapshot.settlement.failureCode, "insufficient_points");
  assert.equal(h.context.snapshot.busy, false);
});

test("unknown settlement remains an error and never assumes success or deducts points", async () => {
  const h = harness(null);
  h.context.statusError = new TypeError("injected status failure");
  assert.equal(await h.purchase(), null);
  assert.equal(h.context.snapshot.error, "purchase status unavailable");
  assert.equal(h.context.snapshot.settlement, null);
  assert.equal(h.context.snapshot.points, 1000);
  assert.equal(h.context.snapshot.busy, false);
});

test("a price-conflict response with no operation keeps the actionable error", async () => {
  const h = harness({ status: "not_started" });
  h.context.postError = new h.ApiError("price changed; refresh before purchase");
  await h.purchase(137);
  assert.equal(h.context.snapshot.error, "price changed; refresh before purchase");
  assert.equal(h.context.snapshot.settlement.purchased, false);
});

test("balance refresh failure cannot turn a confirmed purchase into a failed purchase", async () => {
  const h = harness({ status: "completed", price_paid: 137 });
  h.context.balanceError = new TypeError("injected balance failure");
  assert.equal((await h.purchase())?.status, "completed");
  assert.equal(h.context.snapshot.error, null);
  assert.equal(h.context.snapshot.points, 1000, "a failed balance read must not guess the new balance");
});

test("a late old-account result cannot replace the new account state", async () => {
  const h = harness({ status: "completed", price_paid: 137 });
  h.context.statusBehavior = () => {
    h.context.identityRef.current = "new-buyer:item";
    return { status: "completed", price_paid: 137 };
  };
  assert.equal(await h.purchase(), null);
  assert.equal(h.context.snapshot.settlement, null);
  assert.equal(h.context.snapshot.error, null);
});
