import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import { isValidElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../src/app/[locale]/admin/inquiries/page.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;

function elements(element) {
  if (Array.isArray(element)) return element.flatMap(elements);
  if (!isValidElement(element)) return [];
  return [element, ...elements(element.props.children)];
}

function render(selected = true) {
  const selection = { id: selected ? "owned-inquiry" : null, generation: 1 };
  const summary = { id: "owned-inquiry", subject: "Owned inquiry", name: "Owned author", email: "owned@example.test",
    status: "open", created_at: "2026-10-13T00:00:00Z" };
  const detail = { ...summary, content: "Owned content", replies: [{ id: "owned-reply", admin_name: "Owned moderator",
    content: "Owned answer", created_at: "2026-10-13T00:01:00Z" }] };
  const values = [[summary], 1, 1, "all", false, selection, selected ? { selection, data: detail } : null,
    false, null, "Owned draft", {}];
  let stateIndex = 0;
  const calls = [];
  const react = require("react");
  const context = { exports: {}, require: (name) => {
    if (name === "react") return { ...react, useState: () => [values[stateIndex++], () => {}],
      useRef: (value) => ({ current: value }), useCallback: (callback) => callback,
      useEffect: () => {}, useLayoutEffect: () => {} };
    if (name === "next-intl") return { useTranslations: () => (key) => key };
    if (name === "@/providers/AuthProvider") return { useAuth: () => ({ user: { id: "admin" } }) };
    if (name === "@/providers/ToastProvider") return { useToast: () => ({ toast: () => {} }) };
    if (name === "@/components/ui/Button") return { Button: "button" };
    if (name === "@/components/ui/RichTextEditor") return { RichTextEditor: "textarea" };
    if (name === "@/components/learn/MarkdownRenderer") return { MarkdownRenderer: "article" };
    if (name === "@/lib/rich-content.mjs") return { isRichContentEmpty: (value) => !value.trim() };
    if (name === "@/lib/api-client") return { apiFetch: (...args) => calls.push(args), ApiError: Error };
    return require(name);
  } };
  vm.runInNewContext(compiled, context, { timeout: 1000 });
  const element = context.exports.default();
  const html = renderToStaticMarkup(element);
  assert.equal(calls.length, 0);
  assert.equal(stateIndex, values.length);
  return { tree: elements(element), html };
}

const tokens = (element) => new Set((element.props.className ?? "").split(/\s+/));

test("the actual inquiry render stacks full-width list and detail below lg and keeps two columns above lg", () => {
  const { tree, html } = render();
  const body = tree.find((element) => element.type === "div" && tokens(element).has("lg:flex-row"));
  assert(body);
  assert(tokens(body).has("flex-col"));
  const panels = body.props.children.filter(isValidElement);
  assert.equal(panels.length, 2);
  for (const panel of panels) {
    for (const token of ["w-full", "min-w-0", "lg:w-1/2"]) assert(tokens(panel).has(token), `missing ${token}`);
    assert(!tokens(panel).has("w-1/2"));
  }
  assert(html.includes("Owned inquiry"));
  assert(html.includes("Owned moderator"));
});

test("the actual unselected inquiry list remains full-width", () => {
  const { tree } = render(false);
  const body = tree.find((element) => element.type === "div" && tokens(element).has("lg:flex-row"));
  const panels = body.props.children.filter(isValidElement);
  assert.equal(panels.length, 1);
  assert(tokens(panels[0]).has("w-full"));
  assert(!tokens(panels[0]).has("lg:w-1/2"));
});

test("actual title, contact, reply metadata and action parents allow wrapping within their panels", () => {
  const { tree } = render();
  const title = tree.find((element) => element.type === "h2" && element.props.children === "Owned inquiry");
  assert(tokens(title).has("break-words"));
  for (const text of ["Owned author", "owned@example.test", "Owned moderator"]) {
    const node = tree.find((element) => element.type === "span" && element.props.children === text);
    assert(tokens(node).has("max-w-full"));
    assert(tokens(node).has("break-words"));
  }
  const moderator = tree.find((element) => element.type === "span" && element.props.children === "Owned moderator");
  const metadata = tree.find((element) => element.type === "div" && [element.props.children].flat().includes(moderator));
  assert(tokens(metadata).has("flex-wrap"));
  const send = tree.find((element) => element.type === "button" && element.props.children === "inquirySend");
  const actions = tree.find((element) => element.type === "div" && [element.props.children].flat().includes(send));
  assert(tokens(actions).has("flex-wrap"));
});

test("actual inquiry status labels remain unbroken when the table is narrow", () => {
  const { tree } = render();
  const badges = tree.filter((element) => element.type === "span" && element.props.children === "inquiryStatusOpen");
  assert.equal(badges.length, 2);
  for (const badge of badges) {
    assert(tokens(badge).has("whitespace-nowrap"));
    assert(tokens(badge).has("shrink-0"));
  }
});

test("the narrow inquiry table reserves status width and retains dates beneath the author", () => {
  const { tree } = render();
  const table = tree.find((element) => element.type === "table");
  assert(tokens(table).has("table-fixed"));
  const status = tree.find((element) => element.type === "th" && element.props.children === "inquiryStatus");
  assert(tokens(status).has("w-32"));
  const time = tree.find((element) => element.type === "th" && element.props.children === "colTime");
  assert(tokens(time).has("hidden"));
  assert(tokens(time).has("md:table-cell"));
  const mobileDate = tree.find((element) => element.type === "span" && tokens(element).has("md:hidden"));
  assert.equal(typeof mobileDate.props.children, "string");
  assert(mobileDate.props.children.length > 0);
});
