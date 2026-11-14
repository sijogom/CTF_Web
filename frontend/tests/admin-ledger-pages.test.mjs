import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const require = createRequire(import.meta.url), React = require("react");
const helperContext = { exports: {}, Date };
vm.runInNewContext(ts.transpileModule(readFileSync(new URL("../src/lib/admin-ledger-state.ts", import.meta.url), "utf8"), { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
} }).outputText, helperContext);
const helpers = helperContext.exports;
const typecheckRoots = (owned, nextEnv, exists = existsSync) => exists(nextEnv) ? [...owned, nextEnv] : [...owned];
class ApiError extends Error {}
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function elements(node) {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!React.isValidElement(node)) return [];
  return [node, ...elements(node.props.children)];
}
function harness(page, locale = "ko", overrides = {}) {
  const source = readFileSync(new URL(`../src/app/[locale]/admin/${page}/page.tsx`, import.meta.url), "utf8"), syntax = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX), names = [];
  assert.equal(syntax.parseDiagnostics.length, 0);
  function visit(node) {
    if (ts.isVariableDeclaration(node) && ts.isArrayBindingPattern(node.name) && node.initializer && ts.isCallExpression(node.initializer) && node.initializer.expression.getText(syntax) === "useState") names.push(node.name.elements[0].getText(syntax));
    ts.forEachChild(node, visit);
  }
  visit(syntax);
  const values = new Map(), refs = [], calls = [], effects = [], effectRecords = [], callbacks = [], memos = [], writes = [], listeners = new Map(), translations = new Map(), messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
  let stateIndex = 0, refIndex = 0, callbackIndex = 0, memoIndex = 0, effectIndex = 0, location = new URL(`http://localhost:13101/ctf/${locale}/admin/${page}?retained=one#kept`);
  const unchanged = (before, next) => before && next && before.length === next.length && before.every((value, index) => Object.is(value, next[index]));
  const memo = (store, index, create, deps) => { if (!store[index] || !unchanged(store[index].deps, deps)) store[index] = { deps, value: create() }; return store[index].value; };
  const toast = () => {};
  const h = { owner: { user: { id: "admin-1", role: "admin" }, sessionStatus: "authenticated" }, respond: async () => ({ items: [], total: 0, page: 1, size: 50 }),
    values, calls, effects, writes, listeners, messages, setUrl(value) { location = new URL(value, location); }, get url() { return location; } };
  const window = { get location() { return location; }, addEventListener(name, fn) { listeners.set(name, fn); }, removeEventListener(name) { listeners.delete(name); } };
  const mockReact = { ...React, useState(initial) { const name = names[stateIndex++]; assert(name); if (!values.has(name)) values.set(name, name in overrides ? overrides[name] : typeof initial === "function" ? initial() : initial);
    return [values.get(name), (update) => values.set(name, typeof update === "function" ? update(values.get(name)) : update)]; },
    useRef(initial) { const index = refIndex++; refs[index] ??= { current: initial }; return refs[index]; },
    useCallback(fn, deps) { return memo(callbacks, callbackIndex++, () => fn, deps); }, useMemo(fn, deps) { return memo(memos, memoIndex++, fn, deps); },
    useEffect(fn, deps) { const index = effectIndex++; if (!effectRecords[index] || !unchanged(effectRecords[index].deps, deps)) effectRecords[index] = { deps, fn, dirty: true, cleanup: effectRecords[index]?.cleanup };
      effects.push(fn); } };
  const context = { exports: {}, URL, URLSearchParams, AbortController, window, setTimeout, clearTimeout, require(name) {
    if (name === "react") return mockReact;
    if (name === "next-intl") return { useLocale: () => locale, useTranslations: (namespace) => {
      if (!translations.has(namespace)) translations.set(namespace, (key, args = {}) => { let text = messages[namespace][key]; assert.equal(typeof text, "string", `${locale}:${namespace}.${key}`);
        for (const [name, value] of Object.entries(args)) text = text.replace(`{${name}}`, String(value)); return text; });
      return translations.get(namespace); } };
    if (name === "next/navigation") return { useSearchParams: () => location.searchParams };
    if (name === "@/providers/AuthProvider") return { useAuth: () => h.owner };
    if (name === "@/providers/ToastProvider") return { useToast: () => ({ toast }) };
    if (name === "@/lib/admin-ledger-state") return helpers;
    if (name === "@/lib/api-client") return { ApiError, apiFetch(...args) { calls.push(args); return h.respond(...args); } };
    if (name === "@/lib/url-query") return { readPositiveInt: (value) => /^\d+$/.test(value ?? "") && Number(value) >= 1 ? Number(value) : 1,
      writeUrlQuery(updates, mode) { writes.push({ updates, mode }); for (const [key, value] of Object.entries(updates)) if (value === null || value === "") location.searchParams.delete(key); else location.searchParams.set(key, value); } };
    if (name === "@/lib/list-response.mjs") return { parseListPage: (value) => value };
    if (name === "@/components/ui/ListPagination") return { ListPagination: () => null };
    return require(name);
  } };
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText, context);
  h.render = () => { stateIndex = 0; refIndex = 0; callbackIndex = 0; memoIndex = 0; effectIndex = 0; effects.length = 0; return elements(context.exports.default()); };
  h.commit = async () => { for (const record of effectRecords) if (record.dirty) { record.cleanup?.(); record.dirty = false; record.cleanup = record.fn(); } await flush(); };
  h.runFetch = async () => { h.render(); const effect = effects.find((fn) => fn.toString().includes("void fetchLogs()")); assert(effect); effect(); await flush(); };
  return h;
}
const log = { id: "8a29cbe5-238a-4f96-8ca9-17724c658bcd", user_id: "student-1", username: "Student", challenge_id: 7, challenge_name: "Challenge", challenge_type: "wargame",
  submitted_flag_recorded: true, submitted_flag_length: 8, submitted_flag_masked: "********", submitted_flag_revealable: true, is_correct: false, submission_context: "wargame", submitted_at: null };
function submissionHarness(locale = "ko") { return harness("submissions", locale, { logs: [log], loading: false, total: 1 }); }
function open(h) { h.render().find((node) => node.type === "button" && node.props.children === h.messages.admin.submissionReveal).props.onClick(); return h.render(); }
function reason(h, value) { h.render().find((node) => node.type === "textarea").props.onChange({ target: { value } }); }
function submit(h) { h.render().find((node) => node.type === "form").props.onSubmit({ preventDefault() {} }); }

test("actual reveal dialog is localized, focuses its reason and cancels with no POST in four locales", () => {
  for (const locale of ["ko", "en", "ja", "zh"]) {
    const h = submissionHarness(locale), tree = open(h), dialog = tree.find((node) => node.type === "dialog"), textarea = tree.find((node) => node.type === "textarea");
    let focused = 0, opened = 0; dialog.props.ref.current = { open: false, showModal() { opened++; this.open = true; }, close() { this.open = false; } }; textarea.props.ref.current = { focus() { focused++; } };
    h.effects.find((fn) => fn.toString().includes("dialog.showModal"))(); assert.equal(opened, 1); assert.equal(focused, 1);
    assert(dialog.props.className.includes("w-[calc(100vw-24px)]")); assert(dialog.props.className.includes("overflow-y-auto"));
    assert(tree.some((node) => node.type === "label" && node.props.children === h.messages.admin.revealReasonLabel));
    let prevented = 0; dialog.props.onCancel({ preventDefault() { prevented++; } }); assert.equal(prevented, 1); assert.equal(h.values.get("revealTarget"), null); assert.equal(h.calls.length, 0);
  }
});

test("actual confirm sends one POST body, locks cancel and input, then displays only the matching committed original", async () => {
  const h = submissionHarness(); let resolve; h.respond = () => new Promise((done) => { resolve = done; }); open(h); reason(h, "  Approved review  "); submit(h); submit(h);
  let tree = h.render(); assert.equal(h.calls.length, 1); assert.equal(h.calls[0][0], `/api/learn/submissions/logs/${log.id}/flag`);
  assert.equal(h.calls[0][1].method, "POST"); assert.equal(h.calls[0][1].body.reason, "Approved review"); assert.equal(tree.find((node) => node.type === "textarea").props.disabled, true);
  tree.find((node) => node.type === "dialog").props.onCancel({ preventDefault() {} }); assert.notEqual(h.values.get("revealTarget"), null);
  resolve({ id: log.id, submitted_flag: "<literal original>", submitted_at: null }); await flush(); tree = h.render();
  assert.equal(h.values.get("revealTarget"), null); assert(tree.some((node) => node.props.children === "<literal original>")); assert.equal(h.calls.length, 1);
});

test("actual validation and server failures retain the same reason draft without exposing originals", async () => {
  const h = submissionHarness(); open(h);
  for (const value of ["  ", "<b>reason</b>", "a".repeat(1001)]) { reason(h, value); submit(h); assert.equal(h.calls.length, 0); assert(h.values.get("revealError")); }
  for (const status of [422, 404, 409, 503]) {
    h.respond = async () => { const error = new ApiError(`Failure ${status}`); error.status = status; throw error; }; reason(h, "Retained draft"); submit(h); await flush();
    assert.equal(h.values.get("revealReason"), "Retained draft"); assert.equal(h.values.get("revealPending"), false); assert.equal(h.values.get("revealError"), `Failure ${status}`);
    assert.equal(Object.keys(h.values.get("revealedFlags")).length, 0);
  }
});

test("actual filter invalidation and account changes reject late reveal responses, while moderator has no reveal control", async () => {
  for (const change of ["filter", "owner", "unknown"]) {
    const h = submissionHarness(); let resolve; h.respond = () => new Promise((done) => { resolve = done; }); open(h); reason(h, "Review"); submit(h);
    if (change === "filter") h.render().find((node) => node.type === "select").props.onChange({ target: { value: "ctf" } });
    else { h.owner = change === "owner" ? { user: { id: "admin-2", role: "admin" }, sessionStatus: "authenticated" } : { user: null, sessionStatus: "unknown" }; h.render(); }
    resolve({ id: log.id, submitted_flag: "Must not appear", submitted_at: null }); await flush();
    assert.equal(Object.keys(h.values.get("revealedFlags")).length, 0); assert(!h.render().some((node) => node.props.children === "Must not appear"));
  }
  const moderator = submissionHarness(); moderator.owner.user.role = "moderator";
  assert(!moderator.render().some((node) => node.type === "button" && node.props.children === moderator.messages.admin.submissionReveal));
});

test("an old reveal result or finally callback cannot display another target's value or unlock its newer request", async () => {
  const h = submissionHarness(), waiting = []; h.respond = () => new Promise((resolve) => waiting.push(resolve));
  open(h); reason(h, "First reason"); submit(h);
  h.render().find((node) => node.type === "select").props.onChange({ target: { value: "ctf" } });
  const second = { ...log, id: "34404765-72b2-4c72-82f5-faf77120d227", challenge_name: "Other challenge" };
  h.values.set("logs", [second]); open(h); reason(h, "Second reason"); submit(h);
  waiting[0]({ id: log.id, submitted_flag: "Old target value", submitted_at: null }); await flush();
  assert.equal(h.values.get("revealPending"), true); assert.equal(h.values.get("revealTarget").scope.submissionId, second.id);
  assert.equal(Object.keys(h.values.get("revealedFlags")).length, 0);
  waiting[1]({ id: second.id, submitted_flag: "Second target value", submitted_at: null }); await flush();
  assert.equal(h.values.get("revealPending"), false); assert.equal(h.values.get("revealedFlags")[second.id], "Second target value");
  assert.equal(h.values.get("revealedFlags")[log.id], undefined); assert.equal(h.calls.length, 2);
});

test("already revealed text is immediately hidden when the owner or confirmed session changes", async () => {
  for (const owner of [{ user: { id: "admin-2", role: "admin" }, sessionStatus: "authenticated" }, { user: { id: "admin-1", role: "admin" }, sessionStatus: "unknown" }]) {
    const h = submissionHarness(); h.respond = async () => ({ id: log.id, submitted_flag: "Owner-only original", submitted_at: null });
    open(h); reason(h, "Review"); submit(h); await flush(); assert(h.render().some((node) => node.props.children === "Owner-only original"));
    h.owner = owner; assert(!h.render().some((node) => node.props.children === "Owner-only original"));
  }
});

test("actual audit time controls preserve other URL filters, reset page1 and send inclusive aware UTC boundaries", async () => {
  for (const locale of ["ko", "en", "ja", "zh"]) {
    const h = harness("audit", locale); h.setUrl(`http://localhost:13101/ctf/${locale}/admin/audit?action=user.&q=kept&page=4&retained=one#kept`);
    const inputs = h.render().filter((node) => node.type === "input" && node.props.type === "datetime-local"); assert.equal(inputs.length, 2);
    assert.equal(inputs[0].props.step, "1");
    inputs[0].props.onChange({ target: { value: "2026-10-18T10:20:30" } });
    h.render().find((node) => node.props.id === "audit-created-from-fraction").props.onChange({ target: { value: "123456" } });
    const from = h.url.searchParams.get("created_from"); assert.equal(from, helpers.auditInstantFromLocal("2026-10-18T10:20:30.123456"));
    assert.equal(h.url.searchParams.has("page"), false); assert.equal(h.url.searchParams.get("action"), "user."); assert.equal(h.url.searchParams.get("q"), "kept"); assert.equal(h.url.hash, "#kept");
    h.render().find((node) => node.props.id === "audit-created-to").props.onChange({ target: { value: "2026-10-18T10:20:30" } });
    h.render().find((node) => node.props.id === "audit-created-to-fraction").props.onChange({ target: { value: "123456" } });
    await h.runFetch(); const query = new URL(h.calls.at(-1)[0], h.url).searchParams;
    assert.equal(query.get("created_from"), from); assert.equal(query.get("created_to"), from); assert.equal(query.get("page"), "1"); assert.equal(query.get("action_prefix"), "user."); assert.equal(query.get("search"), "kept");
  }
});

test("actual audit Back/Forward URL restoration reloads both times and rejects invalid ranges without presenting an empty result", async () => {
  const h = harness("audit"); h.render(); h.effects.find((fn) => fn.toString().includes('addEventListener("popstate"'))();
  for (const query of ["created_from=2026-10-18T10%3A00%3A00", "created_from=2026-10-19T00%3A00%3A00Z&created_to=2026-10-18T00%3A00%3A00Z"]) {
    h.setUrl("/ctf/ko/admin/audit?" + query); h.listeners.get("popstate")(); await h.runFetch(); const tree = h.render();
    assert.equal(h.calls.length, 0); assert(tree.some((node) => node.props.role === "alert")); assert.equal(tree.filter((node) => node.type === "input" && node.props.type === "datetime-local").length, 2);
    assert(!tree.some((node) => node.props.children === h.messages.admin.noLogs));
  }
  h.setUrl("/ctf/ko/admin/audit?created_from=2026-10-18T00%3A00%3A00.000001Z&created_to=2026-10-19T00%3A00%3A00Z&page=1"); h.listeners.get("popstate")(); await h.runFetch();
  assert.equal(h.calls.length, 1); assert.equal(new URL(h.calls[0][0], h.url).searchParams.get("created_from"), "2026-10-18T00:00:00.000001Z");
  const restored = h.render();
  assert.equal(restored.find((node) => node.props.id === "audit-created-from").props.value, helpers.auditTimeInputParts("2026-10-18T00:00:00.000001Z").dateTime);
  assert.equal(restored.find((node) => node.props.id === "audit-created-from-fraction").props.value, "000001");
});

test("audit precision fields are explicitly labeled in four locales and preserve fractions across native date edits and reload", async () => {
  for (const locale of ["ko", "en", "ja", "zh"]) {
    const h = harness("audit", locale), instant = "2026-10-18T00:00:01.000001Z";
    h.setUrl(`/ctf/${locale}/admin/audit?created_from=${encodeURIComponent(instant)}&action=user.&q=kept&page=3`);
    let tree = h.render();
    for (const side of ["from", "to"]) {
      const id = `audit-created-${side}-fraction`, input = tree.find((node) => node.props.id === id), label = tree.find((node) => node.type === "label" && node.props.htmlFor === id);
      assert.equal(input.props.type, "text"); assert.equal(input.props.inputMode, "numeric"); assert.equal(input.props.maxLength, 6); assert.equal(input.props.pattern, "[0-9]{0,6}");
      assert.equal(label.props.children, h.messages.admin[side === "from" ? "auditCreatedFromFraction" : "auditCreatedToFraction"]);
      assert.equal(tree.find((node) => node.props.id === `audit-created-${side}`).props.step, "1");
    }
    const expectedParts = helpers.auditTimeInputParts(instant);
    assert.equal(tree.find((node) => node.props.id === "audit-created-from").props.value, expectedParts.dateTime);
    assert.equal(tree.find((node) => node.props.id === "audit-created-from-fraction").props.value, "000001");
    const nextLocal = expectedParts.dateTime.replace(/:01$/, ":02");
    tree.find((node) => node.props.id === "audit-created-from").props.onChange({ target: { value: nextLocal } });
    await h.runFetch();
    const expected = instant.replace(":01.", ":02.");
    assert.equal(h.url.searchParams.get("created_from"), expected);
    assert.equal(new URL(h.calls.at(-1)[0], h.url).searchParams.get("created_from"), expected);
    assert.equal(h.render().find((node) => node.props.id === "audit-created-from-fraction").props.value, "000001");
    const reloaded = harness("audit", locale); reloaded.setUrl(h.url.href); tree = reloaded.render();
    assert.equal(tree.find((node) => node.props.id === "audit-created-from").props.value, nextLocal);
    assert.equal(tree.find((node) => node.props.id === "audit-created-from-fraction").props.value, "000001");
    tree.find((node) => node.type === "button" && node.props["aria-label"] === reloaded.messages.admin.auditClearFrom).props.onClick();
    await reloaded.runFetch(); tree = reloaded.render();
    assert.equal(tree.find((node) => node.props.id === "audit-created-from").props.value, "");
    assert.equal(tree.find((node) => node.props.id === "audit-created-from-fraction").props.value, "");
    assert.equal(reloaded.url.searchParams.has("created_from"), false);
    assert.equal(new URL(reloaded.calls[0][0], reloaded.url).searchParams.has("created_from"), false);
    assert.equal(reloaded.url.searchParams.get("q"), "kept");
  }
});

test("partial fractional digits remain exact and an empty native date preserves the incomplete draft but sends no API", async () => {
  const h = harness("audit"), commit = async () => { h.render(); await h.commit(); }; await commit();
  h.render().find((node) => node.props.id === "audit-created-from-fraction").props.onChange({ target: { value: "000001" } });
  const initialCalls = h.calls.length; await commit(); let tree = h.render();
  assert.equal(h.calls.length, initialCalls); assert(tree.some((node) => node.props.role === "alert"));
  assert(!tree.some((node) => node.props.children === h.messages.admin.noLogs));
  assert.equal(tree.find((node) => node.props.id === "audit-created-from-fraction").props.value, "000001");
  assert.equal(h.url.searchParams.has("created_from"), false);
  tree.find((node) => node.props.id === "audit-created-from").props.onChange({ target: { value: "2026-10-18T10:20:30" } });
  await commit(); assert.equal(h.calls.length, initialCalls + 1);
  for (const fraction of ["0", "00", "001", "1234", "00001", "000001", ""]) {
    h.render().find((node) => node.props.id === "audit-created-from-fraction").props.onChange({ target: { value: fraction } });
    await commit();
    assert.equal(h.url.searchParams.get("created_from"), helpers.auditInstantFromLocal(`2026-10-18T10:20:30${fraction ? `.${fraction}` : ""}`));
  }
  for (const invalid of ["1234567", "a", "1.5", "-1", "１"]) {
    const calls = h.calls.length, before = h.url.href;
    h.render().find((node) => node.props.id === "audit-created-from-fraction").props.onChange({ target: { value: invalid } });
    h.render(); await h.commit(); assert.equal(h.calls.length, calls); assert.equal(h.url.href, before);
    assert.equal(h.render().find((node) => node.props.id === "audit-created-from-fraction").props.value, "");
  }
  h.render().find((node) => node.props.id === "audit-created-from-fraction").props.onChange({ target: { value: "000001" } });
  h.render().find((node) => node.props.id === "audit-created-from").props.onChange({ target: { value: "" } });
  const calls = h.calls.length; await commit(); tree = h.render();
  assert.equal(h.calls.length, calls); assert.equal(tree.find((node) => node.props.id === "audit-created-from-fraction").props.value, "000001");
  tree.find((node) => node.type === "button" && node.props["aria-label"] === h.messages.admin.auditClearFrom).props.onClick();
  await commit(); assert.equal(h.calls.length, calls + 1); assert(!h.render().some((node) => node.props.role === "alert"));
});

test("actual audit API failure remains distinct from a successful zero-result page and keeps filters available for retry", async () => {
  const h = harness("audit"); h.setUrl("/ctf/ko/admin/audit?created_from=2026-10-18T00%3A00%3A00Z&action=auth.login");
  h.respond = async () => { throw new ApiError("Server unavailable"); }; await h.runFetch(); let tree = h.render();
  assert(tree.some((node) => node.props.role === "alert")); assert(!tree.some((node) => node.props.children === h.messages.admin.noLogs));
  assert.equal(tree.filter((node) => node.type === "input" && node.props.type === "datetime-local").length, 2);
  h.respond = async () => ({ items: [], total: 0, page: 1, size: 50 }); await h.runFetch(); tree = h.render();
  assert(!tree.some((node) => node.props.role === "alert")); assert(tree.some((node) => node.props.children === h.messages.admin.noLogs));
  assert.equal(h.calls[0][0], h.calls[1][0]);
});

test("same audit query popstate preserves the pending GET while unknown query/hash and search draft are restored", async () => {
  const h = harness("audit"); let resolve; h.respond = () => new Promise((done) => { resolve = done; }); h.render(); await h.commit();
  assert.equal(h.calls.length, 1); const signal = h.calls[0][1].signal;
  h.values.set("search", "Uncommitted draft"); h.setUrl("/ctf/ko/admin/audit?retained=changed&unknown=one#new-hash"); h.listeners.get("popstate")(); h.render(); await h.commit();
  assert.equal(h.calls.length, 1); assert.equal(signal.aborted, false); assert.equal(h.values.get("search"), "");
  const row = { id: "audit-1", created_at: "2026-10-18T00:00:00Z", actor: "Admin", action: "auth.login", target: "one", ip_address: "127.0.0.1" };
  resolve({ items: [row], total: 1, page: 1, size: 50 }); await flush(); assert.equal(h.values.get("loading"), false); assert.equal(h.values.get("logs")[0].id, row.id);
});

test("changed audit time popstate aborts the old GET, excludes its late response and makes one new exact-range GET", async () => {
  const h = harness("audit"), waiting = []; h.respond = () => new Promise((done) => waiting.push(done)); h.render(); await h.commit();
  const signal = h.calls[0][1].signal;
  h.setUrl("/ctf/ko/admin/audit?created_from=2026-10-18T00%3A00%3A00Z&created_to=2026-10-19T00%3A00%3A00Z"); h.listeners.get("popstate")(); h.render(); await h.commit();
  assert.equal(signal.aborted, true); assert.equal(h.calls.length, 2);
  const query = new URL(h.calls[1][0], h.url).searchParams; assert.equal(query.get("created_from"), "2026-10-18T00:00:00Z"); assert.equal(query.get("created_to"), "2026-10-19T00:00:00Z");
  waiting[0]({ items: [{ id: "stale" }], total: 1, page: 1, size: 50 }); await flush(); assert.equal(h.values.get("logs").length, 0); assert.equal(h.values.get("loading"), true);
  waiting[1]({ items: [], total: 0, page: 1, size: 50 }); await flush(); assert.equal(h.values.get("logs").length, 0); assert.equal(h.values.get("loading"), false);
});

test("explicit accessible time clear recovers an invalid URL in four locales without discarding action/search", async () => {
  for (const locale of ["ko", "en", "ja", "zh"]) {
    const h = harness("audit", locale); h.setUrl(`/ctf/${locale}/admin/audit?action=user.&q=kept&created_from=invalid&page=4`);
    await h.runFetch(); let tree = h.render(); const clear = tree.find((node) => node.type === "button" && node.props["aria-label"] === h.messages.admin.auditClearFrom);
    assert(clear); assert.equal(clear.props.title, h.messages.admin.auditClearFrom); assert(clear.props.className.includes("h-11")); assert(clear.props.className.includes("w-11"));
    assert.equal(tree.find((node) => node.props.id === "audit-created-from").props.value, ""); clear.props.onClick(); await h.runFetch(); tree = h.render();
    assert.equal(h.url.searchParams.has("created_from"), false); assert.equal(h.url.searchParams.has("page"), false); assert.equal(h.url.searchParams.get("q"), "kept");
    assert.equal(h.calls.length, 1); const query = new URL(h.calls[0][0], h.url).searchParams; assert.equal(query.has("created_from"), false); assert.equal(query.get("action_prefix"), "user.");
    assert.equal(query.get("search"), "kept"); assert(!tree.some((node) => node.props.role === "alert"));
    for (const label of tree.filter((node) => node.type === "label")) assert.equal(elements(label).filter((node) => node.type === "button").length, 0);
  }
});

test("the owned pages and helper type-check in memory against the existing project compiler contract", () => {
  const root = fileURLToPath(new URL("../", import.meta.url)), config = ts.readConfigFile(path.join(root, "tsconfig.json"), ts.sys.readFile);
  assert.equal(config.error, undefined);
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, root), files = ["src/app/[locale]/admin/submissions/page.tsx", "src/app/[locale]/admin/audit/page.tsx", "src/lib/admin-ledger-state.ts"].map((file) => path.join(root, file));
  for (const exists of [existsSync, () => false]) {
    const roots = typecheckRoots(files, path.join(root, "next-env.d.ts"), exists);
    assert.deepEqual(roots.slice(0, files.length), files);
    const program = ts.createProgram(roots, { ...parsed.options, noEmit: true, incremental: false });
    const owned = new Set(files.map((file) => path.resolve(file).toLowerCase()));
    const diagnostics = ts.getPreEmitDiagnostics(program).filter((item) => !item.file || owned.has(path.resolve(item.file.fileName).toLowerCase()));
    assert.equal(diagnostics.length, 0, ts.formatDiagnosticsWithColorAndContext(diagnostics, { getCurrentDirectory: () => root, getCanonicalFileName: (file) => file, getNewLine: () => "\n" }));
  }
});

test("framework-derived next-env roots are conditional while all three owned TypeScript roots remain mandatory", () => {
  const owned = Object.freeze(["submissions.tsx", "audit.tsx", "admin-ledger-state.ts"]), next = "stage/next-env.d.ts";
  assert.deepEqual(typecheckRoots(owned, next, () => true), [...owned, next]);
  assert.deepEqual(typecheckRoots(owned, next, () => false), [...owned]);
});
