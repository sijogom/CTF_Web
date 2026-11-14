import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../src/components/admin/challenges/ChallengeTable.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("ChallengeTable.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
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
function harness(locale = "ko", isSuperAdmin = true) {
  const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
  const calls = { publish: [], edit: [], delete: [] };
  const context = { exports: {}, require: (name) => {
    if (name === "next-intl") return { useTranslations: (namespace) => (key) => {
      assert.equal(typeof messages[namespace]?.[key], "string", `unresolved ${namespace}.${key}`);
      return messages[namespace][key];
    } };
    if (name === "@/providers/AuthProvider") return { useAuth: () => ({ isSuperAdmin }) };
    if (name === "@/i18n/navigation") return { useRouter: () => ({ push: (href) => calls.edit.push(href) }) };
    return require(name);
  } };
  vm.runInNewContext(compiled, context, { timeout: 1000 });
  const render = (challenges, options = {}) => context.exports.ChallengeTable({ challenges, loading: false, actionLoading: null,
    onTogglePublish: (challenge) => calls.publish.push(challenge), onDelete: (challenge) => calls.delete.push(challenge), ...options });
  return { render, messages, calls };
}
const challenge = (id, overrides = {}) => ({ id, name: `Owned ${id}`, category: "misc", difficulty: "medium", value: 123, solves: 7, challenge_type: "wargame", is_published: false, ...overrides });

for (const locale of ["ko", "en", "ja", "zh"]) {
  test(`actual ${locale} table keeps eight localized columns and one semantic row per challenge`, () => {
    const h = harness(locale), first = challenge(21), second = challenge(22, { challenge_type: "competition", is_published: true }), node = h.render([first, second]), all = elements(node);
    assert.equal(all.filter((item) => item.type === "table").length, 1);
    const headers = all.filter((item) => item.type === "th"), body = all.find((item) => item.type === "tbody"), rows = elements(body).filter((item) => item.type === "tr");
    const keys = ["colTitle", "challengeTypeLabel", "colCategory", "colDifficulty", "colPoints", "colSolves", "colStatus", "colActions"];
    assert.deepEqual(headers.map(text), keys.map((key) => h.messages.admin[key]));
    assert(headers.every((header) => header.props.scope === "col"));
    assert.equal(rows.length, 2); assert.deepEqual(rows.map((row) => row.key), ["21", "22"]);
    rows.forEach((row, index) => {
      const cells = elements(row).filter((item) => item.type === "td"); assert.equal(cells.length, 8);
      assert.deepEqual(cells.map((cell) => cell.props["data-label"]), keys.map((key) => h.messages.admin[key]));
      assert.equal(text(cells[0]), [first, second][index].name); assert.equal(text(cells[2]), "misc");
      assert.equal(text(cells[3]), h.messages.difficulty.medium); assert.equal(text(cells[4]), "123"); assert.equal(text(cells[5]), "7");
      assert.equal(text(cells[1]), h.messages.admin[index ? "challengeType_competition" : "challengeType_wargame"]);
      assert.equal(text(cells[6]), h.messages.admin[index ? "published" : "unpublished"]);
      assert.equal(elements(row).filter((item) => item.type === "button").length, 3);
    });
  });
  test(`actual ${locale} admin callbacks retain exact challenge identity and locale router edit path`, () => {
    const h = harness(locale), value = challenge(21), buttons = elements(h.render([value])).filter((node) => node.type === "button");
    buttons.find((button) => text(button) === h.messages.admin.unpublished).props.onClick();
    buttons.find((button) => text(button) === h.messages.admin.chalEdit).props.onClick();
    buttons.find((button) => text(button) === h.messages.admin.deleteChallenge).props.onClick();
    assert.equal(h.calls.publish.length, 1); assert.equal(h.calls.publish[0], value);
    assert.deepEqual(h.calls.edit, ["/admin/challenges/21/edit"]); assert.equal(h.calls.delete.length, 1); assert.equal(h.calls.delete[0], value);
  });
  test(`actual ${locale} moderator has no duplicate or hidden delete callback and keeps publish/edit`, () => {
    const h = harness(locale, false), value = challenge(21), buttons = elements(h.render([value])).filter((node) => node.type === "button");
    assert.equal(buttons.length, 2); assert(!buttons.some((button) => text(button) === h.messages.admin.deleteChallenge));
    buttons[0].props.onClick(); buttons[1].props.onClick(); assert.deepEqual(h.calls.delete, []); assert.equal(h.calls.publish[0], value); assert.deepEqual(h.calls.edit, ["/admin/challenges/21/edit"]);
  });
}

for (const title of ["LongUnbrokenOwnedChallengeTitle".repeat(8), "긴한글무공백문제제목원문을보존해서읽습니다".repeat(8)]) {
  test("long original title has a full mobile row and shrinkable wrapping without clipping", () => {
    const h = harness(), node = h.render([challenge(21, { name: title })]), cells = elements(node).filter((item) => item.type === "td"), classes = tokens(cells[0]);
    assert.equal(text(cells[0]), title); assert(renderToStaticMarkup(node).includes(title));
    for (const token of ["col-span-2", "min-w-0", "[overflow-wrap:anywhere]"]) assert(classes.has(token));
    assert(![...classes].some((token) => /truncate|line-clamp|overflow-hidden|whitespace-nowrap/.test(token)));
    const actions = cells[7], layout = elements(actions).find((item) => item.type === "div");
    assert(tokens(actions).has("col-span-2")); assert(tokens(layout).has("min-w-0")); assert(tokens(layout).has("flex-wrap"));
  });
}

test("the same table switches stacked cells to fixed eight-column desktop comparison without duplicate controls", () => {
  const node = harness().render([challenge(21)]), all = elements(node), table = all.find((item) => item.type === "table"), row = all.find((item) => item.type === "tbody").props.children[0];
  for (const token of ["block", "w-full", "@min-[56rem]/challenges:table", "@min-[56rem]/challenges:table-fixed"]) assert(tokens(table).has(token));
  for (const token of ["grid", "grid-cols-2", "@min-[56rem]/challenges:table-row"]) assert(tokens(row).has(token));
  assert(tokens(node).has("@container/challenges")); assert(tokens(node).has("rounded-lg")); assert(!tokens(node).has("overflow-x-auto"));
  const head = all.find((item) => item.type === "thead"); assert(tokens(head).has("sr-only")); assert(!tokens(head).has("hidden"));
  assert(tokens(head).has("@min-[56rem]/challenges:not-sr-only")); assert(tokens(head).has("@min-[56rem]/challenges:table-header-group"));
  for (const cell of all.filter((item) => item.type === "td")) for (const token of ["min-w-0", "@min-[56rem]/challenges:table-cell", "before:content-[attr(data-label)]", "@min-[56rem]/challenges:before:hidden"]) assert(tokens(cell).has(token));
  assert(!all.some((item) => [...tokens(item)].some((token) => /^(?:sm|md|lg|xl|2xl):/.test(token))));
  const widths = all.filter((item) => item.type === "th").map((header) => Number(/w-\[(\d+)%\]/.exec(header.props.className)[1]));
  assert.equal(widths.length, 8); assert.equal(widths.reduce((sum, width) => sum + width, 0), 100);
  assert.equal(all.filter((item) => item.type === "button").length, 3);
});

for (const isSuperAdmin of [true, false]) {
  test(`${isSuperAdmin ? "admin" : "moderator"} publish busy remains scoped to its row and loading/empty states are unchanged`, () => {
    const h = harness("ko", isSuperAdmin), first = challenge(21), second = challenge(22), node = h.render([first, second], { actionLoading: 21 }), rows = elements(node).filter((item) => item.type === "tbody")[0].props.children;
    rows.forEach((row, index) => {
      const buttons = elements(row).filter((item) => item.type === "button");
      assert.equal(buttons[0].props.disabled, index === 0); assert.equal(text(buttons[0]), index === 0 ? "..." : h.messages.admin.unpublished);
      assert.equal(buttons.length, isSuperAdmin ? 3 : 2); assert.equal(buttons[1].props.disabled, undefined);
      if (isSuperAdmin) assert.equal(buttons[2].props.disabled, undefined);
    });
    assert.equal(h.render([]), null);
    const loading = h.render([first], { loading: true }); assert.equal(elements(loading).filter((item) => item.type === "table" || item.type === "button").length, 0); assert(elements(loading).some((item) => tokens(item).has("animate-spin")));
  });
}
