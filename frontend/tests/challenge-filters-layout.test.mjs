import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../src/components/admin/challenges/ChallengeFilters.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("ChallengeFilters.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
assert.equal(ast.parseDiagnostics.length, 0);
assert.deepEqual(ast.statements.filter(ts.isImportDeclaration).map((node) => node.moduleSpecifier.text), ["next-intl"]);
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
function harness(locale, replacements = {}) {
  const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
  const calls = { search: [], category: [], type: [], server: [] };
  const denied = () => { calls.server.push("attempt"); throw new Error("FILTER_TEST_NO_SERVER_CALLS"); };
  const context = { exports: {}, fetch: denied, XMLHttpRequest: denied, WebSocket: denied, require: (name) => {
    if (name === "next-intl") return { useTranslations: (namespace) => (key) => {
      const value = replacements[`${namespace}.${key}`] ?? messages[namespace]?.[key];
      assert.equal(typeof value, "string", `unresolved ${namespace}.${key}`);
      return value;
    } };
    assert.equal(name, "react/jsx-runtime", "filters cannot import a network or business client");
    return require(name);
  } };
  vm.runInNewContext(compiled, context, { timeout: 1000 });
  const render = (options = {}) => context.exports.ChallengeFilters({ search: "original query", categoryFilter: "crypto", typeFilter: "competition",
    onSearchChange: (value) => calls.search.push(value), onCategoryChange: (value) => calls.category.push(value), onTypeChange: (value) => calls.type.push(value), ...options });
  return { render, messages, calls, categories: Array.from(context.exports.CATEGORIES) };
}

for (const locale of ["ko", "en", "ja", "zh"]) {
  test(`actual ${locale} filters preserve localized names, controlled values and six category options`, () => {
    const h = harness(locale), node = h.render(), all = elements(node), input = all.find((item) => item.type === "input"), select = all.find((item) => item.type === "select"), options = all.filter((item) => item.type === "option");
    assert.equal(input.props.value, "original query");
    assert.equal(input.props.placeholder, h.messages.admin.searchChallenges);
    assert.equal(input.props["aria-label"], h.messages.admin.searchChallenges);
    assert.equal(select.props.value, "crypto"); assert.equal(select.props["aria-label"], h.messages.admin.allCategories);
    assert.deepEqual(h.categories, ["web", "pwn", "rev", "crypto", "forensic", "misc"]);
    assert.deepEqual(options.map((item) => item.props.value), ["all", ...h.categories]);
    assert.deepEqual(options.map(text), [h.messages.admin.allCategories, "Web", "Pwn", "Rev", "Crypto", "Forensic", "Misc"]);
    assert.deepEqual(all.filter((item) => item.type === "button").map(text), [h.messages.common.all, h.messages.admin.challengeType_wargame, h.messages.admin.challengeType_competition]);
    assert(renderToStaticMarkup(node).includes('value="crypto" selected=""'));
    assert.deepEqual(h.calls.server, []);
  });
  test(`actual ${locale} type tabs retain one selected state and their exact callbacks`, () => {
    const h = harness(locale), values = ["", "wargame", "competition"];
    for (const [selected, typeFilter] of values.entries()) {
      const buttons = elements(h.render({ typeFilter })).filter((item) => item.type === "button");
      assert.equal(buttons.length, 3);
      buttons.forEach((button, index) => {
        assert.equal(tokens(button).has("bg-accent-primary"), index === selected);
        assert.equal(tokens(button).has("text-white"), index === selected);
        assert.equal(tokens(button).has("text-text-secondary"), index !== selected);
      });
      buttons[selected].props.onClick();
    }
    assert.deepEqual(h.calls.type, values); assert.deepEqual(h.calls.search, []); assert.deepEqual(h.calls.category, []); assert.deepEqual(h.calls.server, []);
  });
  test(`actual ${locale} search and category changes preserve values without mutating props or calling a server`, () => {
    const h = harness(locale), query = '한글 query "><script>original()</script>', node = h.render({ search: query }), all = elements(node), input = all.find((item) => item.type === "input"), select = all.find((item) => item.type === "select");
    input.props.onChange({ target: { value: "updated 검색 / 検索 / 搜索" } }); input.props.onChange({ target: { value: "" } });
    select.props.onChange({ target: { value: "web" } }); select.props.onChange({ target: { value: "all" } });
    assert.deepEqual(h.calls.search, ["updated 검색 / 検索 / 搜索", ""]); assert.deepEqual(h.calls.category, ["web", "all"]); assert.deepEqual(h.calls.type, []);
    assert.equal(input.props.value, query); assert.equal(select.props.value, "crypto");
    const markup = renderToStaticMarkup(node); assert(!markup.includes("<script>original()</script>")); assert(markup.includes("&lt;script&gt;original()&lt;/script&gt;"));
    const rerendered = elements(h.render({ search: "replacement", categoryFilter: "all" }));
    assert.equal(rerendered.find((item) => item.type === "input").props.value, "replacement"); assert.equal(rerendered.find((item) => item.type === "select").props.value, "all");
    assert.deepEqual(h.calls.server, []);
  });
  test(`actual ${locale} filters stack narrowly and preserve the desktop row without hiding or shrinking text`, () => {
    const node = harness(locale).render(), all = elements(node), wrappers = all.filter((item) => item.type === "div"), input = all.find((item) => item.type === "input"), select = all.find((item) => item.type === "select");
    const tabs = wrappers.find((item) => tokens(item).has("gap-1")), fields = wrappers.find((item) => tokens(item).has("gap-3"));
    for (const token of ["flex", "flex-wrap", "min-w-0", "max-w-full"]) assert(tokens(tabs).has(token));
    for (const token of ["flex", "flex-col", "sm:flex-row", "min-w-0", "max-w-full"]) assert(tokens(fields).has(token));
    for (const token of ["min-w-0", "w-full", "max-w-full", "flex-1", "text-sm"]) assert(tokens(input).has(token));
    for (const token of ["min-w-0", "w-full", "max-w-full", "sm:w-auto", "text-sm"]) assert(tokens(select).has(token));
    for (const button of all.filter((item) => item.type === "button")) for (const token of ["min-w-0", "max-w-full", "whitespace-normal", "break-words", "text-sm"]) assert(tokens(button).has(token));
    assert(!all.some((item) => [...tokens(item)].some((token) => /truncate|line-clamp|overflow-hidden|whitespace-nowrap|scale-|text-\[/.test(token))));
  });
}

test("long localized tab names remain original text in one wrapping button each", () => {
  const label = "긴분류원문LongUnbrokenClassification".repeat(5), h = harness("ko", { "admin.challengeType_competition": label }), node = h.render(), buttons = elements(node).filter((item) => item.type === "button");
  assert.equal(buttons.length, 3); assert.equal(text(buttons[2]), label); assert(renderToStaticMarkup(node).includes(label));
  assert(tokens(buttons[2]).has("break-words")); assert(tokens(buttons[2]).has("max-w-full")); assert.deepEqual(h.calls.server, []);
});
