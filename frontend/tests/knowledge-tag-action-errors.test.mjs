import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import { isValidElement } from "react";
import ts from "typescript";
import { normalizeApiErrorPayload } from "../src/lib/api-error.mjs";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../src/app/[locale]/admin/tags/page.tsx", import.meta.url), "utf8");
const clientSource = readFileSync(new URL("../src/lib/api-client.ts", import.meta.url), "utf8");
const clientTree = ts.createSourceFile("api-client.ts", clientSource, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS);
const actualClient = ["ApiError", "consumeApiResponse"].map((name) => {
  const matches = clientTree.statements.filter((node) => node.name?.getText(clientTree) === name);
  assert.equal(matches.length, 1);
  return matches[0].getText(clientTree);
}).join("\n");
function execute(code, context) {
  const compiled = ts.transpileModule(code, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }, reportDiagnostics: true });
  assert.equal(compiled.diagnostics.length, 0);
  vm.runInNewContext(compiled.outputText, context, { timeout: 1000 });
}
const api = { exports: {}, normalizeApiErrorPayload };
execute(actualClient + "\nglobalThis.consume = consumeApiResponse;", api);
const clean = (value) => JSON.parse(JSON.stringify(value));
const tag = (id) => ({ id, name: `Owned ${id}`, slug: `owned-${id}`, color: null, description: null, parent_id: null });
const names = ["tags", "loading", "unmapped", "unmappedLoading", "unmappedType", "reconciling", "tagsError", "unmappedError",
  "saving", "deletingId", "editingId", "name", "slug", "color", "description", "parentId"];
function elements(node) {
  if (Array.isArray(node)) return node.flatMap(elements);
  return isValidElement(node) ? [node, ...elements(node.props.children)] : [];
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });
}
async function settled(h) {
  for (let attempt = 0; attempt < 20; attempt++) {
    await new Promise((resolve) => setImmediate(resolve));
    if (!h.state.get("saving") && !h.state.get("reconciling") && h.state.get("deletingId") === null) return;
  }
  assert.fail("actual mutation did not release its busy state");
}
function harness(overrides = {}) {
  const state = new Map(Object.entries({ tags: [tag(101), tag(102)], loading: false, unmappedLoading: false,
    name: " Owned ", slug: " owned ", color: "#123456", description: " Retained description ", parentId: 20, ...overrides }));
  const refs = [], calls = [], toasts = [];
  let index = 0, refIndex = 0, confirmed = true, confirmations = 0;
  const h = { state, calls, toasts, respond: async () => json({}),
    cancelConfirm() { confirmed = false; }, confirmations: () => confirmations };
  const context = { exports: {}, AbortController, confirm: () => { confirmations++; return confirmed; }, require: (name) => {
    if (name === "react") return { ...require("react"), useState: (initial) => {
      const key = names[index++];
      if (!state.has(key)) state.set(key, initial);
      return [state.get(key), (value) => state.set(key, value)];
    }, useRef: (initial) => refs[refIndex++] ??= { current: initial }, useCallback: (callback) => callback, useEffect() {} };
    if (name === "next-intl") return { useTranslations: () => (key) => key };
    if (name === "@/i18n/navigation") return { Link: "a" };
    if (name === "@/providers/ToastProvider") return { useToast: () => ({ toast: (...args) => toasts.push(args) }) };
    if (name === "@/lib/api-client") return { ApiError: api.exports.ApiError, apiFetch: async (url, options) => {
      calls.push({ url, options }); return api.consume(await h.respond(url, options));
    } };
    return require(name);
  } };
  execute(source, context);
  h.render = () => {
    index = refIndex = 0;
    const tree = elements(context.exports.default());
    assert.equal(index, names.length);
    return {
      tree,
      button(label) {
        const matches = tree.filter((node) => node.type === "button" && node.props.children === label);
        assert.equal(matches.length, 1, `unique ${label}`); return matches[0];
      },
      saveButton() {
        const matches = tree.filter((node) => node.type === "button" && node.props.onClick?.name === "handleSubmit");
        assert.equal(matches.length, 1); return matches[0];
      },
      deleteButton(id = 101) {
        const row = tree.find((node) => node.type === "tr" && node.key === String(id));
        assert(row); return elements(row).find((node) => node.type === "button" && node.props.children === "삭제");
      },
    };
  };
  return h;
}

test("fixed tag columns retain a local horizontal scroll container and nonwrapping headers/actions", () => {
  const view = harness().render(), table = view.tree.find((node) => node.type === "table");
  assert.match(table.props.className, /\btable-fixed\b/);
  assert(table.props.className.split(/\s+/).includes("min-w-[56rem]"));
  assert(table.props.className.split(/\s+/).includes("w-full"));
  const wrapper = view.tree.find((node) => node.type === "div" && [node.props.children].flat().includes(table));
  assert(wrapper.props.className.split(/\s+/).includes("overflow-x-auto"));
  const columns = elements(table).filter((node) => node.type === "col");
  assert.deepEqual(columns.map((node) => node.props.className ?? null), ["w-16", "w-[22%]", "w-[20%]", "w-[18%]", null, "w-24"]);
  const headers = elements(table).filter((node) => node.type === "th");
  assert.deepEqual(headers.map((node) => node.props.children), ["색상", "이름", "Slug", "부모", "설명", "작업"]);
  for (const header of headers) assert(header.props.className.split(/\s+/).includes("whitespace-nowrap"));
  for (const row of elements(table).filter((node) => node.type === "tr" && node.key !== null)) {
    const buttons = elements(row).filter((node) => node.type === "button");
    assert.deepEqual(buttons.map((node) => node.props.children), ["수정", "삭제"]);
    assert.equal(buttons[0].props.disabled, false); assert.equal(buttons[1].props.disabled, false);
    for (const button of buttons) for (const token of ["whitespace-nowrap", "shrink-0"]) assert(button.props.className.split(/\s+/).includes(token));
    const group = elements(row).find((node) => node.type === "div");
    assert(group.props.className.split(/\s+/).includes("flex-nowrap"));
  }
});

test("long original tag strings remain encoded text with complete parent/description values and wrapping", () => {
  const parent = { ...tag(101), name: "부모".repeat(50) };
  const child = { ...tag(102), name: "이름".repeat(50), slug: "h04-tag-".repeat(13).slice(0, 100), parent_id: parent.id,
    description: "정상 설명 <script>alert(1)</script> " + "LongUnbrokenDescription".repeat(30) };
  const original = [parent, child], h = harness({ tags: original, parentId: parent.id }), view = h.render();
  const row = view.tree.find((node) => node.type === "tr" && node.key === String(child.id));
  const cells = elements(row).filter((node) => node.type === "td");
  assert.equal(cells.length, 6);
  assert.deepEqual(cells.slice(1, 5).map((node) => node.props.children), [child.name, child.slug, parent.name, child.description]);
  for (const cell of cells.slice(1, 5)) {
    const tokens = cell.props.className.split(/\s+/);
    assert(tokens.includes("whitespace-normal") && tokens.includes("[overflow-wrap:anywhere]"));
    assert(!tokens.some((token) => /truncate|line-clamp|overflow-hidden/.test(token)));
  }
  assert(!view.tree.some((node) => node.type === "script" || node.props.dangerouslySetInnerHTML));
  const parentSelect = view.tree.find((node) => node.type === "select" && !node.props["aria-label"]);
  assert.equal(parentSelect.props.value, parent.id);
  assert.deepEqual(elements(parentSelect).filter((node) => node.type === "option").map((node) => node.props.value), ["", parent.id, child.id]);
  assert.equal(h.state.get("tags"), original); assert.deepEqual(h.calls, []);
});

test("table presentation leaves the original complete business script and ApiError catches unchanged", () => {
  const normalized = source.replace(/\r\n/g, "\n"), index = normalized.indexOf("\n  return (\n");
  assert(index > 0);
  assert.equal(createHash("sha256").update(normalized.slice(0, index)).digest("hex"), "5570063136a1075f7ef3c65a9c11d9db963150a5727f4e521d7939756852dc37");
});

for (const [status, detail] of [
  [409, "매핑 감사 이력이 있는 태그는 영구 삭제할 수 없습니다. 이름·설명을 수정하거나 보존하세요."],
  [409, "하위 지식 태그가 있는 태그는 삭제할 수 없습니다. 하위 태그를 먼저 재분류하세요."],
  [409, "콘텐츠에 연결된 지식 태그는 삭제할 수 없습니다. 연결을 먼저 명시적으로 해제하거나 보존하세요."],
  [404, "태그를 찾을 수 없습니다."], [403, "권한이 없습니다."],
]) {
  test(`actual delete handler preserves the server ${status} reason: ${detail}`, async () => {
    const h = harness({ editingId: 101 }), original = h.state.get("tags");
    h.respond = async () => json({ detail }, status);
    await h.render().deleteButton().props.onClick();
    assert.deepEqual(h.toasts, [["error", detail]]);
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].url, "/api/learn/knowledge-tags/101");
    assert.deepEqual(clean(h.calls[0].options), { method: "DELETE" });
    assert.equal(h.state.get("tags"), original);
    assert.equal(h.state.get("editingId"), 101);
    assert.equal(h.state.get("deletingId"), null);
    assert.equal(h.render().deleteButton().props.disabled, false);
  });
}

test("actual successful DELETE 204 never parses JSON and refreshes both lists without changing the route", async () => {
  const h = harness({ editingId: 101 }); let deleteJsonReads = 0;
  h.respond = async (url, options) => {
    if (options?.method === "DELETE") {
      const response = new Response(null, { status: 204 });
      response.json = () => { deleteJsonReads++; throw Error("204 must not parse JSON"); };
      return response;
    }
    return json(url.endsWith("/unmapped") ? [] : [tag(102)]);
  };
  await h.render().deleteButton().props.onClick();
  assert.equal(deleteJsonReads, 0);
  assert.deepEqual(h.calls.map(({ url }) => url), ["/api/learn/knowledge-tags/101", "/api/learn/knowledge-tags", "/api/learn/knowledge-tags/unmapped"]);
  assert.deepEqual(h.toasts, [["success", "태그가 삭제되었습니다."]]);
  assert.deepEqual(clean(h.state.get("tags")), [tag(102)]);
  assert.equal(h.state.get("editingId"), null);
  assert.equal(h.state.get("name"), "");
  assert.equal(h.state.get("deletingId"), null);
});

test("actual delete cancellation performs no request, toast, or busy transition", async () => {
  const h = harness(); h.cancelConfirm();
  await h.render().deleteButton().props.onClick();
  assert.equal(h.confirmations(), 1); assert.equal(h.calls.length, 0); assert.equal(h.toasts.length, 0);
  assert.equal(h.state.get("deletingId"), null);
});

for (const [editingId, method] of [[null, "POST"], [101, "PUT"]]) {
  test(`actual ${method} tag save preserves its body and server reason while restoring every busy control`, async () => {
    const h = harness({ editingId }), pending = deferred();
    h.respond = () => pending.promise;
    const original = h.state.get("tags"), view = h.render();
    const button = view.saveButton();
    const operation = button.props.onClick();
    assert.equal(h.state.get("saving"), true);
    assert.equal(h.render().saveButton().props.disabled, true);
    assert.equal(h.render().deleteButton().props.disabled, true);
    assert.equal(h.render().button("규칙 기반 자동 매핑").props.disabled, true);
    await view.deleteButton().props.onClick();
    await view.button("규칙 기반 자동 매핑").props.onClick();
    assert.equal(h.calls.length, 1);
    assert.equal(h.confirmations(), 0);
    assert.equal(h.calls[0].url, "/api/learn/knowledge-tags" + (editingId ? "/101" : ""));
    assert.deepEqual(clean(h.calls[0].options), { method, body: { name: " Owned ", slug: " owned ", color: "#123456", description: " Retained description ", parent_id: 20 } });
    pending.resolve(json({ detail: "이미 사용 중인 지식 태그 이름 또는 slug입니다." }, 409)); await operation;
    assert.deepEqual(h.toasts, [["error", "이미 사용 중인 지식 태그 이름 또는 slug입니다."]]);
    assert.equal(h.state.get("saving"), false);
    assert.equal(h.state.get("tags"), original);
    assert.equal(h.state.get("name"), " Owned ");
    assert.equal(h.render().saveButton().props.disabled, false);
    assert.equal(h.render().deleteButton().props.disabled, false);
    assert.equal(h.render().button("규칙 기반 자동 매핑").props.disabled, false);
    assert.equal(h.calls.length, 1);
  });
}

test("actual reconcile keeps POST empty-body semantics, preserves the server reason and releases shared busy state", async () => {
  const h = harness(), pending = deferred(), view = h.render();
  h.respond = () => pending.promise;
  const operation = view.button("규칙 기반 자동 매핑").props.onClick();
  assert.equal(h.render().button("자동 매핑 중...").props.disabled, true);
  assert.equal(h.render().deleteButton().props.disabled, true);
  assert.equal(h.render().button("추가").props.disabled, true);
  await view.button("추가").props.onClick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, "/api/learn/knowledge-tags/reconcile");
  assert.deepEqual(clean(h.calls[0].options), { method: "POST", body: {} });
  pending.resolve(json({ detail: { code: "owned_denial", message: "권한이 없습니다." } }, 403)); await operation; await settled(h);
  assert.deepEqual(h.toasts, [["error", "권한이 없습니다."]]);
  assert.equal(h.state.get("reconciling"), false);
  assert.equal(h.render().button("규칙 기반 자동 매핑").props.disabled, false);
  assert.equal(h.render().deleteButton().props.disabled, false);
  assert.equal(h.render().button("추가").props.disabled, false);
  assert.equal(h.calls.length, 1);
});

for (const [action, fallback] of [["delete", "태그 삭제에 실패했습니다."], ["save", "태그 저장에 실패했습니다."], ["reconcile", "규칙 기반 자동 매핑에 실패했습니다."]]) {
  test(`actual ${action} uses its original fallback only for a non-ApiError and never automatically retries`, async () => {
    const h = harness(); h.respond = async () => { throw Error("private transport detail"); };
    const view = h.render(), button = action === "delete" ? view.deleteButton() : view.button(action === "save" ? "추가" : "규칙 기반 자동 매핑");
    await button.props.onClick(); await settled(h);
    assert.deepEqual(h.toasts, [["error", fallback]]);
    assert.equal(h.calls.length, 1);
    assert.equal(h.state.get("saving"), false);
    assert.equal(h.state.get("reconciling"), false);
    assert.equal(h.state.get("deletingId"), null);
  });
}
