import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../src/app/[locale]/admin/email/page.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("AdminEmailPage.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
assert.equal(ast.parseDiagnostics.length, 0);
const compiled = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const tokens = (node) => new Set((node.props.className ?? "").split(/\s+/));
function elements(node) {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!React.isValidElement(node)) return [];
  return [node, ...elements(node.props.children)];
}
function text(node) {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (Array.isArray(node)) return node.map(text).join("");
  if (React.isValidElement(node)) return text(node.props.children);
  return String(node);
}
function log(id, overrides = {}) {
  return { id, sender_id: "owned-admin", recipients: ["owned@example.invalid"], subject: `Owned subject ${id}`,
    target_type: "user", total_count: 4, success_count: 3, fail_count: 1, error_details: null,
    created_at: "2026-10-18T00:00:00Z", ...overrides };
}
function render(locale = "ko", { logs = [log("owned-one"), log("owned-two", { target_type: "all", fail_count: 0 })], loading = false, page = 2, total = 21 } = {}) {
  const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
  const values = [
    ["activeTab", "history"], ["target", "all"], ["role", "user"], ["subject", ""], ["body", ""],
    ["userSearch", ""], ["searchResults", []], ["selectedUsers", []], ["searching", false],
    ["sending", false], ["result", null], ["error", ""], ["showSendConfirm", false], ["logsLoading", loading],
    ["smtpHost", ""], ["smtpPort", 587], ["smtpFrom", ""], ["smtpTls", true], ["smtpUser", ""], ["smtpPassword", ""],
    ["smtpSaving", false], ["smtpTesting", false], ["smtpLoadState", "idle"], ["smtpMessage", null],
    ["previewHtml", ""], ["logs", logs], ["logPage", page], ["logTotal", total],
  ];
  let stateIndex = 0;
  const calls = [], updates = [];
  const context = { exports: {}, require: (name) => {
    if (name === "react") return { ...React, useState: () => {
      assert(stateIndex < values.length, "unexpected page state");
      const [key, value] = values[stateIndex++];
      return [value, (next) => updates.push({ key, value: next })];
    }, useEffect: () => {}, useCallback: (callback) => callback };
    if (name === "next-intl") return { useTranslations: (namespace) => (key) => {
      assert.equal(typeof messages[namespace]?.[key], "string", `unresolved ${namespace}.${key}`);
      return messages[namespace][key];
    } };
    if (name === "@/lib/api-client") return { ApiError: Error, apiFetch: async (...args) => {
      calls.push(args);
      return { items: logs, total };
    } };
    if (name === "@/components/ui/Button") return { Button: "button" };
    if (name === "@/components/ui/ConfirmModal") return { ConfirmModal: "dialog" };
    if (name === "@/components/ui/RichTextEditor") return { RichTextEditor: "textarea" };
    if (name === "@/lib/rich-content.mjs") return { isRichContentEmpty: (value) => !String(value).trim() };
    return require(name);
  } };
  vm.runInNewContext(compiled, context, { timeout: 1000 });
  const node = context.exports.default();
  assert.equal(stateIndex, values.length);
  assert.equal(calls.length, 0);
  return { node, all: elements(node), html: renderToStaticMarkup(node), messages, calls, updates };
}

for (const locale of ["ko", "en", "ja", "zh"]) {
  test(`actual ${locale} history has one localized keyboard-focusable local scroll region`, () => {
    const h = render(locale), regions = h.all.filter((node) => node.props.role === "region");
    assert.equal(regions.length, 1);
    const region = regions[0];
    assert.equal(region.props["aria-label"], h.messages.adminEmail.tabHistory);
    assert.equal(region.props.tabIndex, 0);
    for (const token of ["overflow-x-auto", "focus-visible:ring-2", "focus-visible:ring-inset", "focus-visible:ring-accent-primary"]) assert(tokens(region).has(token));
    assert.equal(region.props.onKeyDown, undefined);
    const table = elements(region).find((node) => node.type === "table");
    assert(tokens(table).has("w-full")); assert(tokens(table).has("min-w-min"));
    assert(![...tokens(table)].some((token) => /^min-w-\[/.test(token)));
    assert.equal(table.props.style?.minWidth, undefined);
    assert.equal(h.all.filter((node) => node.type === "table").length, 1);
    assert.equal(elements(region).filter((node) => node.type === "button").length, 0);
  });

  test(`actual ${locale} history preserves four columns, original rows and unbroken short metadata`, () => {
    const h = render(locale), headers = h.all.filter((node) => node.type === "th");
    assert.deepEqual(headers.map(text), ["logDate", "logTarget", "logSubject", "logResult"].map((key) => h.messages.adminEmail[key]));
    assert(headers.every((node) => tokens(node).has("whitespace-nowrap")));
    const body = h.all.find((node) => node.type === "tbody"), rows = elements(body).filter((node) => node.type === "tr");
    assert.deepEqual(rows.map((row) => row.key), ["owned-one", "owned-two"]);
    for (const [index, row] of rows.entries()) {
      const cells = elements(row).filter((node) => node.type === "td");
      assert.equal(cells.length, 4);
      for (const cell of [cells[0], cells[1], cells[3]]) assert(tokens(cell).has("whitespace-nowrap"));
      assert(text(cells[0]).length > 0);
      assert.equal(text(cells[1]), index === 0 ? "user" : "all");
      assert.equal(text(cells[2]), `Owned subject ${index === 0 ? "owned-one" : "owned-two"}`);
      assert.equal(cells[2].props.title, text(cells[2]));
      for (const token of ["max-w-xs", "truncate"]) assert(tokens(cells[2]).has(token));
      assert.equal(text(cells[3]), index === 0 ? "3 / 1" : "3");
    }
  });
}

for (const subject of ["LongUnbrokenOwnedEmailSubject".repeat(12), "원문을유지하는긴한글메일제목".repeat(12)]) {
  test("long original subject retains text, title and the existing truncation policy", () => {
    const h = render("ko", { logs: [log("long-subject", { subject })] }), cell = h.all.find((node) => node.type === "td" && node.props.title === subject);
    assert(cell); assert.equal(text(cell), subject); assert(h.html.includes(subject));
    assert(tokens(cell).has("max-w-xs")); assert(tokens(cell).has("truncate"));
  });
}

test("history loading and empty states keep the original four-column spans and no synthetic rows", () => {
  for (const loading of [true, false]) {
    const h = render("ko", { logs: [], loading, page: 1, total: 0 }), body = h.all.find((node) => node.type === "tbody"), cells = elements(body).filter((node) => node.type === "td");
    assert.equal(cells.length, 1); assert.equal(cells[0].props.colSpan, 4);
    assert.equal(elements(cells[0]).some((node) => tokens(node).has("animate-spin")), loading);
    assert.equal(text(cells[0]), loading ? "" : h.messages.adminEmail.logNoData);
    assert.equal(elements(body).filter((node) => node.type === "tr").length, 1);
  }
});

test("previous and next retain exact read-only page-size callbacks and original state updates", async () => {
  const h = render(), previous = h.all.find((node) => node.type === "button" && text(node) === h.messages.adminEmail.logPrev), next = h.all.find((node) => node.type === "button" && text(node) === h.messages.adminEmail.logNext);
  assert.equal(previous.props.disabled, false); assert.equal(next.props.disabled, false);
  await previous.props.onClick(); await next.props.onClick();
  assert.deepEqual(h.calls, [["/api/auth/admin/email/logs?page=1&size=10"], ["/api/auth/admin/email/logs?page=3&size=10"]]);
  assert.deepEqual(h.updates.filter(({ key }) => key === "logPage").map(({ value }) => value), [1, 3]);
  assert.deepEqual(h.updates.filter(({ key }) => key === "logsLoading").map(({ value }) => value), [true, false, true, false]);
});

test("history pagination preserves first and last page disabled boundaries", () => {
  for (const page of [1, 3]) {
    const h = render("en", { page }), previous = h.all.find((node) => node.type === "button" && text(node) === h.messages.adminEmail.logPrev), next = h.all.find((node) => node.type === "button" && text(node) === h.messages.adminEmail.logNext);
    assert.equal(previous.props.disabled, page === 1); assert.equal(next.props.disabled, page === 3);
  }
});
