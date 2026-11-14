import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const helperSource = readFileSync(new URL("../src/lib/url-query.ts", import.meta.url), "utf8");
const helperProgram = ts.transpileModule(helperSource, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;

function historyHarness(initialUrl = "http://localhost:13101/ctf/ko/learn?retained=one#kept") {
  let location = new URL(initialUrl), stateReads = 0;
  const originalState = Object.freeze({ application: Object.freeze({ untouched: "original" }) });
  let currentState = originalState;
  const calls = [];
  const history = {
    get state() { stateReads += 1; return currentState; },
    pushState(data, title, url) {
      assert.equal(this, history);
      calls.push({ method: "pushState", data, title, url });
      currentState = data; location = new URL(url, location);
    },
    replaceState(data, title, url) {
      assert.equal(this, history);
      calls.push({ method: "replaceState", data, title, url });
      currentState = data; location = new URL(url, location);
    },
  };
  const window = { history, get location() { return location; } };
  const context = { exports: {}, URL, window };
  vm.runInNewContext(helperProgram, context, { timeout: 1000 });
  return { window, history, calls, originalState, write: context.exports.writeUrlQuery,
    currentUrl: () => new URL(location), stateReads: () => stateReads };
}

function learnSwitchCallback(harness, setViewOverride, writeUrlQuery) {
  const source = readFileSync(new URL("../src/app/[locale]/learn/page.tsx", import.meta.url), "utf8");
  const syntax = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  assert.equal(syntax.parseDiagnostics.length, 0);
  const matches = [];
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(syntax) === "switchView") matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(syntax);
  assert.equal(matches.length, 1);
  const initializer = matches[0].initializer;
  assert.equal(initializer.expression.getText(syntax), "useCallback");
  const expression = initializer.arguments[0].getText(syntax);
  const program = ts.transpileModule("globalThis.invoke = " + expression + ";", {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const context = { URL, window: harness.window, setViewOverride, writeUrlQuery };
  vm.runInNewContext(program, context, { timeout: 1000 });
  return context.invoke;
}

test("writeUrlQuery passes null without copying or reading existing history state", () => {
  const h = historyHarness();
  h.write({ map_types: "course,knowledge_tag" }, "push");
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].method, "pushState");
  assert.equal(h.calls[0].data, null);
  assert.equal(h.calls[0].title, "");
  assert.equal(h.stateReads(), 0);
  assert.deepEqual(h.originalState, { application: { untouched: "original" } });
  assert.equal(Object.isFrozen(h.originalState), true);
  assert.equal(Object.isFrozen(h.originalState.application), true);
});

test("default and explicit replace stay distinct from push", () => {
  const h = historyHarness();
  h.write({ q: "first" });
  h.write({ page: 2 }, "replace");
  h.write({ page: 3 }, "push");
  assert.deepEqual(h.calls.map((call) => call.method), ["replaceState", "replaceState", "pushState"]);
  assert.equal(h.calls.every((call) => call.data === null && call.title === ""), true);
  assert.equal(h.currentUrl().searchParams.get("q"), "first");
  assert.equal(h.currentUrl().searchParams.get("page"), "3");
  assert.equal(h.stateReads(), 0);
});

test("updates preserve pathname, unrelated query, focus and hash with correct encoding", () => {
  const initial = "http://localhost:13101/ctf/ko/learn?view=map&focus_type=course&focus_id=course-id&retained=a%2Bb#selected-node";
  const h = historyHarness(initial), query = "text \ud55c\uae00 &+=#";
  h.write({ map_q: query, page: 2 }, "push");
  const result = h.currentUrl();
  assert.equal(result.pathname, "/ctf/ko/learn");
  assert.equal(result.searchParams.get("view"), "map");
  assert.equal(result.searchParams.get("focus_type"), "course");
  assert.equal(result.searchParams.get("focus_id"), "course-id");
  assert.equal(result.searchParams.get("retained"), "a+b");
  assert.equal(result.searchParams.get("map_q"), query);
  assert.equal(result.searchParams.get("page"), "2");
  assert.equal(result.hash, "#selected-node");
});

test("default deletion values remove only requested keys while true, zero and numbers survive", () => {
  const h = historyHarness("http://localhost:13101/ctf/ko/learn?a=1&b=1&c=1&d=1&retained=one#kept");
  h.write({ a: null, b: undefined, c: "", d: false, enabled: true, zero: 0, count: 42 });
  const result = h.currentUrl();
  for (const key of ["a", "b", "c", "d"]) assert.equal(result.searchParams.has(key), false);
  assert.equal(result.searchParams.get("enabled"), "1");
  assert.equal(result.searchParams.get("zero"), "0");
  assert.equal(result.searchParams.get("count"), "42");
  assert.equal(result.searchParams.get("retained"), "one");
  assert.equal(result.hash, "#kept");
});

test("the same exact URL is a no-op for either history mode", () => {
  const h = historyHarness("http://localhost:13101/ctf/ko/learn?page=2#kept");
  h.write({ page: 2, absent: null }, "push");
  h.write({}, "replace");
  assert.equal(h.calls.length, 0);
  assert.equal(h.stateReads(), 0);
  assert.equal(h.currentUrl().href, "http://localhost:13101/ctf/ko/learn?page=2#kept");
});

test("the helper never accesses a history state getter owned by another layer", () => {
  const h = historyHarness();
  Object.defineProperty(h.history, "state", { get() { throw new Error("history state must remain opaque"); } });
  assert.doesNotThrow(() => h.write({ page: 2 }, "push"));
  assert.equal(h.calls[0].data, null);
  assert.deepEqual(h.originalState, { application: { untouched: "original" } });
});

test("SSR without window does not attempt history access", () => {
  const context = { exports: {}, URL };
  vm.runInNewContext(helperProgram, context, { timeout: 1000 });
  assert.doesNotThrow(() => context.exports.writeUrlQuery({ view: "map" }, "push"));
  assert.equal(context.exports.writeUrlQuery({ q: null }), undefined);
});

test("Learn switchView uses the actual shared writer and preserves immediate local override", () => {
  const h = historyHarness("http://localhost:13101/ctf/ko/learn?focus_type=course&focus_id=course-id&retained=one#kept");
  const events = [];
  const invoke = learnSwitchCallback(h, (view) => events.push(["override", view]), (updates, mode) => {
    events.push(["write", updates.view, mode]); h.write(updates, mode);
  });
  invoke("map"); invoke("map"); invoke("list");
  assert.deepEqual(events, [["override", "map"], ["write", "map", "push"], ["override", "map"], ["write", "map", "push"],
    ["override", "list"], ["write", null, "push"]]);
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls.every((call) => call.method === "pushState" && call.data === null), true);
  assert.equal(h.currentUrl().searchParams.has("view"), false);
  assert.equal(h.currentUrl().searchParams.get("focus_id"), "course-id");
  assert.equal(h.currentUrl().searchParams.get("retained"), "one");
  assert.equal(h.currentUrl().hash, "#kept");
  assert.equal(h.stateReads(), 0);
});
