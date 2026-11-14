import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { EVENT_CATEGORY_RELEASE_STAGE, eventCategoryManagementEnabled } from "../src/lib/event-category-release-stage.mjs";
import { knowledgeExtensionsEnabled } from "../src/lib/knowledge-release-stage.mjs";

const eventExports = {};
vm.runInNewContext(ts.transpile(readFileSync(new URL("../src/types/event.ts", import.meta.url), "utf8"), { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS }), { exports: eventExports });
const { eventCategoryLabel, readEventCategory } = eventExports;

function declaration(file, name, kind) {
  const source = readFileSync(new URL(file, import.meta.url), "utf8");
  const ast = ts.createSourceFile(file, source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  assert.equal(ast.parseDiagnostics.length, 0);
  let found;
  function visit(node) {
    if (node.name?.getText(ast) === name && (kind === "function" ? ts.isFunctionDeclaration(node) : ts.isVariableDeclaration(node))) found = node;
    ts.forEachChild(node, visit);
  }
  visit(ast);
  assert(found, name);
  if (kind === "function") {
    const modifiers = found.modifiers?.filter((modifier) => ![ts.SyntaxKind.ExportKeyword, ts.SyntaxKind.DefaultKeyword].includes(modifier.kind));
    const plain = ts.factory.updateFunctionDeclaration(found, modifiers, found.asteriskToken, found.name, found.typeParameters, found.parameters, found.type, found.body);
    return ts.createPrinter().printNode(ts.EmitHint.Unspecified, plain, ast);
  }
  return (ts.isCallExpression(found.initializer) ? found.initializer.arguments[0] : found.initializer).getText(ast);
}
function evaluate(source, context) {
  return vm.runInNewContext(ts.transpile(`(${source})`, { target: ts.ScriptTarget.ES2022 }), context);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const labels = { ko: "행사", en: "Event", ja: "イベント", zh: "活动" };

test("category display names preserve four locales and fall back without a missing translation key", () => {
  for (const locale of ["ko", "en", "ja", "zh"]) assert.equal(eventCategoryLabel(labels, locale, "key"), labels[locale]);
  assert.equal(eventCategoryLabel(labels, "fr", "key"), "행사");
  assert.equal(eventCategoryLabel({ en: "Event" }, "ko", "key"), "Event");
  assert.equal(eventCategoryLabel(undefined, "ko", "key"), "key");
});

test("URL filters retain registered custom-shaped keys and reject malformed input", () => {
  assert.equal(readEventCategory("c_0123456789abcdef0123456789ab"), "c_0123456789abcdef0123456789ab");
  assert.equal(readEventCategory("workshop"), "workshop");
  for (const input of [null, "", "한글", "<script>", "a".repeat(31)]) assert.equal(readEventCategory(input), "all");
});

test("category management declares stable touch-sized controls without shrinking its edit icon", () => {
  const file = "../src/app/[locale]/admin/events/categories/page.tsx";
  const ast = ts.createSourceFile(file, readFileSync(new URL(file, import.meta.url), "utf8"), ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  const controls = [];
  function visit(node) {
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && ["Button", "button", "input"].includes(node.tagName.getText(ast))) controls.push(node);
    ts.forEachChild(node, visit);
  }
  visit(ast);
  assert.equal(controls.length, 7);
  for (const control of controls) {
    const classes = control.attributes.properties.find((item) => item.name?.getText(ast) === "className")?.initializer?.text?.split(/\s+/) ?? [];
    assert.ok(classes.includes("h-11") || classes.includes("min-h-11"), "every category form control needs a stable 44px minimum height");
    if (control.attributes.properties.some((item) => item.name?.getText(ast) === "title")) {
      for (const value of ["h-11", "w-11", "shrink-0", "p-0"]) assert.ok(classes.includes(value), "edit icon must keep its square touch target");
    }
  }
});

for (const view of ["public", "admin"]) {
  test(`${view} native category selection keeps the key, resets pagination and pushes URL history`, () => {
    const sourcePath = view === "public" ? "../src/app/[locale]/events/page.tsx" : "../src/app/[locale]/admin/events/page.tsx";
    const state = { category: "all", page: 4, writes: [] };
    const setCategory = (value) => { state.category = value; };
    const context = { setCategory, setCategoryFilter: setCategory, setPage: (value) => { state.page = value; },
      writeUrlQuery: (query, mode) => state.writes.push({ query, mode }) };
    const change = evaluate(declaration(sourcePath, "changeCategory"), context);
    change("c_managed-category");
    assert.equal(state.category, "c_managed-category");
    assert.equal(state.page, 1);
    assert.equal(state.writes[0].query.category, "c_managed-category");
    assert.equal(state.writes[0].query.page, null);
    assert.equal(state.writes[0].mode, "push");
    change("all");
    assert.equal(state.writes[1].query.category, null);
    const source = readFileSync(new URL(sourcePath, import.meta.url), "utf8");
    const ast = ts.createSourceFile(sourcePath, source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
    const selects = [];
    function visit(node) { if (ts.isJsxOpeningElement(node) && node.tagName.getText(ast) === "select") selects.push(node); ts.forEachChild(node, visit); }
    visit(ast);
    const filter = selects.find((node) => node.attributes.getText(ast).includes(`${view === "public" ? "public" : "admin"}-event-category-filter`));
    assert(filter, "category filter must be a native select");
    assert(filter.attributes.getText(ast).includes("categoryOptions.loading || Boolean(categoryOptions.error)"), "unavailable category registry must disable selection");
  });
}

function categoryLoader() {
  const state = { categories: [{ key: "old" }], loading: false, error: null };
  const reads = [];
  const context = { AbortController, controllerRef: { current: null }, requestRef: { current: 0 }, admin: true,
    setCategories: (value) => { state.categories = value; }, setLoading: (value) => { state.loading = value; }, setError: (value) => { state.error = value; },
    apiFetch: (url, options) => { const read = { url, options, ...deferred() }; reads.push(read); return read.promise; },
    ApiError: class ApiError extends Error {}, t: (key) => key };
  return { state, reads, reload: evaluate(declaration("../src/components/events/useEventCategories.ts", "reload"), context) };
}

test("category reload rejects a late earlier response even when transport ignores abort", async () => {
  const h = categoryLoader();
  const first = h.reload(), second = h.reload();
  assert.equal(h.reads[0].options.signal.aborted, true);
  assert.equal(h.reads[1].url, "/api/learn/admin/event-categories");
  h.reads[1].resolve({ items: [{ key: "new" }] }); await second;
  h.reads[0].resolve({ items: [{ key: "stale" }] }); await first;
  assert.equal(h.state.categories[0].key, "new");
  assert.equal(h.state.loading, false);
});

test("category load failure preserves prior data and is never treated as an empty registry", async () => {
  const h = categoryLoader();
  const reading = h.reload();
  h.reads[0].reject(new Error("offline")); await reading;
  assert.equal(h.state.categories[0].key, "old");
  assert.equal(h.state.error, "loadFailed");
  assert.equal(h.state.loading, false);
});

function categorySave(overrides = {}) {
  const state = { editing: "new", error: null, busy: false, saved: false, conflicted: false, registrationPending: false, reloads: 0 };
  const puts = [];
  class ApiError extends Error { constructor(code) { super(code); this.code = code; } }
  const context = { editing: "new", draft: { labels, sortOrder: "10" }, operationRef: { current: false },
    createOperationRef: { current: "26a9ba06-6cc4-4990-8e2e-fa2e5fba5123" }, eventCategoryManagementEnabled: () => true,
    listing: { loading: false, error: null, reload: async () => { state.reloads++; } }, conflicted: false,
    LANGUAGES: ["ko", "en", "ja", "zh"].map((code) => ({ code })), ApiError,
    setEditing: (value) => { state.editing = value; }, setBusy: (value) => { state.busy = value; }, setError: (value) => { state.error = value; },
    setSaved: (value) => { state.saved = value; }, setConflicted: (value) => { state.conflicted = value; },
    setRegistrationPending: (value) => { state.registrationPending = value; },
    apiFetch: (url, options) => { const put = { url, options, ...deferred() }; puts.push(put); return put.promise; }, t: (key) => key, tc: (key) => key, ...overrides };
  return { state, puts, context, save: evaluate(declaration("../src/app/[locale]/admin/events/categories/page.tsx", "save", "function"), context), event: { preventDefault() {} } };
}

test("category save sends a structured body once and reloads only after committed success", async () => {
  const h = categorySave();
  const saving = h.save(h.event);
  await h.save(h.event);
  assert.equal(h.puts.length, 1);
  assert.equal(h.puts[0].options.method, "POST");
  assert.equal(typeof h.puts[0].options.body, "object");
  assert.equal(h.puts[0].options.body.labels.ko, "행사");
  assert.equal(h.puts[0].options.body.sort_order, 10);
  assert.equal(h.puts[0].options.body.operation_id, h.context.createOperationRef.current);
  h.puts[0].resolve({ key: "new" }); await saving;
  assert.equal(h.state.saved, true);
  assert.equal(h.state.editing, null);
  assert.equal(h.state.reloads, 1);
});

test("category update captures its key and expected revision; a conflict retains draft", async () => {
  const target = { key: "seminar", revision: 4 };
  const h = categorySave({ editing: target });
  const saving = h.save(h.event);
  assert.equal(h.puts[0].url, "/api/learn/admin/event-categories/seminar");
  assert.equal(h.puts[0].options.body.expected_revision, 4);
  h.puts[0].reject(new h.context.ApiError("event_category_revision_conflict")); await saving;
  assert.equal(h.state.conflicted, true);
  assert.equal(h.context.draft.labels.ko, "행사");
  assert.equal(h.state.error, "conflict");
  assert.equal(h.state.saved, false);
  assert.equal(h.state.reloads, 1);
});

test("duplicate-name errors remain editable and are not mistaken for revision conflicts", async () => {
  const h = categorySave({ editing: { key: "seminar", revision: 4 } });
  const saving = h.save(h.event);
  h.puts[0].reject(new h.context.ApiError("event_category_duplicate")); await saving;
  assert.equal(h.state.conflicted, false);
  assert.equal(h.state.error, "event_category_duplicate");
  assert.equal(h.state.reloads, 0);
  assert.equal(h.context.operationRef.current, false);
});

test("uncertain registration retains its operation and replays the identical request", async () => {
  const h = categorySave();
  const first = h.save(h.event);
  h.puts[0].reject(new Error("response lost")); await first;
  assert.equal(h.state.registrationPending, true);
  assert.equal(h.state.error, "registrationPending");
  const retry = h.save(h.event);
  assert.equal(h.puts[1].options.body.operation_id, h.puts[0].options.body.operation_id);
  assert.deepEqual(h.puts[1].options.body, h.puts[0].options.body);
  h.puts[1].resolve({ key: "already-created" }); await retry;
  assert.equal(h.state.registrationPending, false);
  assert.equal(h.state.saved, true);
});

test("empty numeric order, unavailable registry, and conflicts never send a write", async () => {
  for (const overrides of [{ draft: { labels, sortOrder: "" } }, { listing: { loading: true } }, { listing: { error: "offline" } }, { conflicted: true }, { eventCategoryManagementEnabled: () => false }]) {
    const h = categorySave(overrides);
    await h.save(h.event);
    assert.equal(h.puts.length, 0);
  }
});

test("category release stages leave knowledge extensions unchanged and enable only the final stage", () => {
  assert.equal(knowledgeExtensionsEnabled(), true);
  for (const stage of [undefined, "backend", "core", "extended", "unknown", null]) {
    assert.equal(eventCategoryManagementEnabled(stage), stage === undefined && EVENT_CATEGORY_RELEASE_STAGE === "enabled");
  }
  assert.equal(eventCategoryManagementEnabled("enabled"), true);
  assert.ok(["backend", "enabled"].includes(EVENT_CATEGORY_RELEASE_STAGE));
});

test("disabled category management rejects the direct route before mounting its editor", () => {
  const source = declaration("../src/app/[locale]/admin/events/categories/page.tsx", "EventCategoriesPage", "function");
  let renders = 0;
  const wrapper = vm.runInNewContext(ts.transpile(`(${source})`, { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React }), {
    eventCategoryManagementEnabled: () => false, notFound: () => { throw new Error("not_found"); },
    EventCategoriesEditor: () => {}, React: { createElement: () => { renders++; } },
  });
  assert.throws(() => wrapper(), /not_found/);
  assert.equal(renders, 0);
});
