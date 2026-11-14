import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const require = createRequire(import.meta.url);
const dialogSource = readFileSync(new URL("../src/components/admin/knowledge/KnowledgeTagDeleteDialog.tsx", import.meta.url), "utf8");
const pageSource = readFileSync(new URL("../src/app/[locale]/admin/tags/page.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("tags.tsx", pageSource, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
const declarations = new Map();
function visit(node) {
  if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer && ts.isArrowFunction(node.initializer)) declarations.set(node.name.text, node.initializer.getText(ast));
  ts.forEachChild(node, visit);
}
visit(ast);
const compile = (code) => ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;

function harness({ explicitReturn = true } = {}) {
  const focused = [], modalActions = [], state = {}, requests = [];
  class Element {
    constructor(name) { this.name = name; this.isConnected = true; }
    focus() { focused.push(this.name); }
  }
  class ApiError extends Error {
    constructor(status) { super(`HTTP ${status}`); this.status = status; }
  }
  const trigger = new Element("delete-button"), heading = new Element("tag-heading"), returnFocusRef = { current: trigger };
  const dialog = { showModal() { modalActions.push("open"); }, close() { modalActions.push("close"); } };
  let effect, cancelled = 0;
  const context = { exports: {}, HTMLElement: Element, document: { activeElement: trigger },
    require(name) {
      if (name === "react") return { useEffect: (callback) => { effect = callback; }, useId: () => "delete-title", useRef: () => ({ current: dialog }) };
      if (name === "next-intl") return { useTranslations: () => (key) => key };
      if (name === "lucide-react") return { AlertTriangle: "icon" };
      return require(name);
    },
  };
  vm.runInNewContext(compile(dialogSource), context, { timeout: 1000 });
  const tree = context.exports.KnowledgeTagDeleteDialog({ name: "Web", preview: null, loading: false, deleting: false, error: null, onRetry() {}, onConfirm() {}, onCancel() { cancelled++; }, ...(explicitReturn ? { returnFocusRef } : {}) });
  const cleanup = effect();
  const target = { id: 42, name: "Web" };
  const page = { Error, ApiError, HTMLElement: Element, document: { activeElement: new Element("unrelated") }, AbortController,
    deleteReturnFocusRef: returnFocusRef, tagHeadingRef: { current: heading }, deleteTargetRef: { current: target },
    deletePreviewRequest: { current: null }, deleteRequestBusy: { current: false }, mutationBusy: { current: false },
    deletePreview: { tag: target, confirmation: "a".repeat(64) }, deletePreviewLoading: false, tags: [target], editingId: null,
    apiFetch: async (...args) => { requests.push(args); }, toast() {}, td: (key) => key, resetForm() {}, readTagDeletionPreview: (data) => data,
    fetchTags: async () => { trigger.isConnected = false; }, fetchUnmapped: async () => {},
    loadDeletePreview: async () => {},
  };
  for (const name of ["DeleteTarget", "DeletePreview", "DeletePreviewError", "DeletePreviewLoading", "DeletingId"]) page[`set${name}`] = (value) => { state[name] = value; };
  function actual(name) {
    assert(declarations.has(name));
    vm.runInNewContext(compile(`globalThis.actual = ${declarations.get(name)}`), page, { timeout: 1000 });
    return page.actual;
  }
  page.closeDelete = actual("closeDelete");
  return { page, actual, trigger, heading, returnFocusRef, focused, modalActions, state, requests, tree, cleanup, ApiError, get cancelled() { return cancelled; } };
}

test("cancel and Escape restore the original delete trigger", () => {
  const h = harness();
  h.actual("handleDelete")(42, h.trigger);
  assert.equal(h.returnFocusRef.current, h.trigger);
  let prevented = false;
  h.tree.props.onCancel({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(h.cancelled, 1);
  h.cleanup();
  assert.deepEqual(h.focused, ["delete-button"]);
  assert.deepEqual(h.modalActions, ["open", "close"]);
});

test("successful deletion returns focus to the stable heading after the row is removed", async () => {
  const h = harness();
  await h.actual("confirmDelete")();
  assert.equal(h.returnFocusRef.current, h.heading);
  assert.equal(h.trigger.isConnected, false);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0][1].method, "DELETE");
  assert(h.requests[0][0].includes(`confirmation=${"a".repeat(64)}`));
  h.cleanup();
  assert.deepEqual(h.focused, ["tag-heading"]);
  assert.equal(h.page.deleteRequestBusy.current, false);
});

test("preview 404 returns focus to the heading without another DELETE", async () => {
  const h = harness();
  h.page.apiFetch = async (...args) => { h.requests.push(args); throw new h.ApiError(404); };
  await h.actual("loadDeletePreview")(h.page.deleteTargetRef.current);
  assert.equal(h.returnFocusRef.current, h.heading);
  assert.equal(h.trigger.isConnected, false);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0][0], "/api/learn/knowledge-tags/42/deletion-preview");
  h.cleanup();
  assert.deepEqual(h.focused, ["tag-heading"]);
});

test("dialog cleanup dereferences the latest target and remains compatible without the optional ref", () => {
  const h = harness();
  h.returnFocusRef.current = h.heading;
  h.trigger.isConnected = false;
  h.cleanup();
  assert.deepEqual(h.focused, ["tag-heading"]);
  const fallback = harness({ explicitReturn: false });
  fallback.cleanup();
  assert.deepEqual(fallback.focused, ["delete-button"]);
});

test("the heading remains focusable outside the reloadable tag rows", () => {
  assert.match(pageSource, /<h1 ref=\{tagHeadingRef\} tabIndex=\{-1\}/);
  assert.match(pageSource, /returnFocusRef=\{deleteReturnFocusRef\}/);
});
