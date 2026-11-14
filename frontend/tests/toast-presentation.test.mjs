import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";

const require = createRequire(process.env.CTF_TOAST_DEPENDENCY_STAGE ? path.join(process.env.CTF_TOAST_DEPENDENCY_STAGE, "package.json") : import.meta.url);
const React = require("react"), ts = require("typescript");
const ToastPrimitive = require("@radix-ui/react-toast"), { X } = require("lucide-react");
const source = readFileSync(new URL("../src/providers/ToastProvider.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const tokens = (node) => new Set((node.props.className ?? "").split(/\s+/));
function elements(node) {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!React.isValidElement(node)) return [];
  return [node, ...elements(node.props.children)];
}

class FocusTarget {
  constructor(toastId = null) { this.toastId = toastId; }
  closest(selector) { assert.equal(selector, "[data-toast-id]"); return this.toastId === null ? null : this; }
  getAttribute(name) { assert.equal(name, "data-toast-id"); return this.toastId; }
}

function keyboardEvent(target, key = "Escape", defaultPrevented = false) {
  return { target, key, defaultPrevented, preventCalls: 0,
    preventDefault() { this.defaultPrevented = true; this.preventCalls++; } };
}

function harness(locale = "ko") {
  // Keep real Radix/lucide element identities; only state and translation hooks are deterministic.
  let state = [], sequence = 0;
  const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
  const context = { exports: {}, Element: FocusTarget, crypto: { randomUUID: () => `toast-${++sequence}` }, require: (name) => {
    if (name === "react") return { ...React, useState: () => [state, (update) => { state = typeof update === "function" ? update(state) : update; }], useCallback: (callback) => callback };
    if (name === "next-intl") return { useTranslations: (namespace) => (key) => messages[namespace][key] };
    return require(name);
  } };
  vm.runInNewContext(compiled, context, { timeout: 1000 });
  const child = React.createElement("main", null, React.createElement("input", { defaultValue: "Unchanged page draft" }), "Page content");
  const render = () => context.exports.ToastProvider({ children: child });
  const toast = (...args) => render().props.value.toast(...args);
  return { render, toast, child, closeLabel: messages.common.close };
}

test("only the toast subtree changes identity at empty boundaries while page children stay outside it", () => {
  const ui = harness();
  function provider(key) {
    const tree = ui.render();
    assert.equal(tree.key, null);
    assert(Array.isArray(tree.props.children)); assert.equal(tree.props.children.length, 2);
    const [page, radix] = tree.props.children;
    assert.equal(page, ui.child); assert.equal(radix.type, ToastPrimitive.Provider); assert.equal(radix.key, key);
    assert(Array.isArray(radix.props.children)); assert.equal(radix.props.children.length, 2);
    assert(!elements(radix).includes(ui.child));
    assert.equal(radix.props.children[1].type, ToastPrimitive.Viewport);
    assert.equal(elements(page).find((node) => node.type === "input").props.defaultValue, "Unchanged page draft");
    return radix;
  }
  provider("idle");
  for (let index = 1; index <= 6; index++) { ui.toast("info", `Item ${index}`); provider("active"); }
  let roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
  assert.equal(roots.length, 5);
  while (roots.length > 1) {
    roots[0].props.onOpenChange(false); provider("active");
    roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
  }
  roots[0].props.onOpenChange(false); provider("idle");
  ui.toast("success", "New batch"); provider("active");
});

test("retained consumer and late close callbacks cannot erase a regenerated batch", () => {
  const ui = harness(), notify = ui.render().props.value.toast;
  notify("info", "Old batch");
  const old = elements(ui.render()).find((node) => node.type === ToastPrimitive.Root);
  old.props.onOpenChange(false);
  assert.equal(elements(ui.render()).find((node) => node.type === ToastPrimitive.Provider).key, "idle");
  notify("warning", "New batch", "Consumer remains available outside Radix");
  old.props.onOpenChange(false);
  let roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
  assert.deepEqual(roots.map((node) => node.key), ["toast-2"]);
  assert.equal(elements(roots[0]).find((node) => node.type === ToastPrimitive.Title).props.children, "New batch");
  notify("success", "Another active item");
  roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
  assert.deepEqual(roots.map((node) => node.key), ["toast-2", "toast-3"]);
  assert.equal(elements(ui.render()).find((node) => node.type === ToastPrimitive.Provider).key, "active");
});

test("actual ToastProvider keeps the newest five notifications, order and separate repeated messages", () => {
  const ui = harness();
  for (let index = 1; index <= 7; index++) ui.toast("info", `Title ${index}`, `Description ${index}`);
  let roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
  assert.deepEqual(roots.map((node) => node.key), ["toast-3", "toast-4", "toast-5", "toast-6", "toast-7"]);
  assert.deepEqual(roots.map((root) => elements(root).find((node) => node.type === ToastPrimitive.Title).props.children), ["Title 3", "Title 4", "Title 5", "Title 6", "Title 7"]);
  ui.toast("info", "Repeated"); ui.toast("info", "Repeated");
  roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
  assert.equal(roots.length, 5);
  assert.notEqual(roots[3].key, roots[4].key);
});

test("actual onOpenChange only removes the matching closed notification", () => {
  const ui = harness(); ui.toast("success", "First"); ui.toast("error", "Second");
  let roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
  roots[0].props.onOpenChange(true);
  assert.equal(elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root).length, 2);
  roots[0].props.onOpenChange(false);
  roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
  assert.deepEqual(roots.map((node) => node.key), ["toast-2"]);
});

test("actual TSX Escape callback closes only the focused item across capture and bubble callbacks", () => {
  const ui = harness();
  for (let index = 1; index <= 6; index++) ui.toast("info", `Burst ${index}`);
  let roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
  roots.find((node) => node.key === "toast-4").props.onOpenChange(false);
  roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
  assert.deepEqual(roots.map((node) => node.key), ["toast-2", "toast-3", "toast-5", "toast-6"]);
  for (const root of roots) assert.equal(root.props["data-toast-id"], root.key);
  const focused = roots[0], highest = roots.at(-1), event = keyboardEvent(new FocusTarget(focused.props["data-toast-id"]));
  highest.props.onEscapeKeyDown(event);
  if (!event.defaultPrevented) highest.props.onOpenChange(false);
  focused.props.onEscapeKeyDown(event);
  if (!event.defaultPrevented) focused.props.onOpenChange(false);
  assert.equal(event.preventCalls, 1);
  assert.deepEqual(elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root).map((node) => node.key), ["toast-3", "toast-5", "toast-6"]);
});

test("outside and foreign focus leave Radix's last-item fallback untouched", () => {
  for (const target of [new FocusTarget(), new FocusTarget("other-provider-toast"), null, {}]) {
    const ui = harness(); ui.toast("info", "First"); ui.toast("success", "Second");
    let roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
    const highest = roots.at(-1), event = keyboardEvent(target);
    highest.props.onEscapeKeyDown(event);
    assert.equal(event.preventCalls, 0); assert.equal(event.defaultPrevented, false);
    assert.equal(elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root).length, 2);
    highest.props.onOpenChange(false);
    roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
    assert.deepEqual(roots.map((node) => node.key), ["toast-1"]);
  }
});

test("prevented events and other keys do not delete items or consume callbacks", () => {
  const ui = harness(); ui.toast("info", "First"); ui.toast("success", "Second");
  const roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root), focused = roots[0];
  for (const event of [keyboardEvent(new FocusTarget(focused.key), "Escape", true), keyboardEvent(new FocusTarget(focused.key), "Enter")]) {
    roots.at(-1).props.onEscapeKeyDown(event); assert.equal(event.preventCalls, 0);
    assert.deepEqual(elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root).map((node) => node.key), ["toast-1", "toast-2"]);
  }
  const notify = ui.render().props.value.toast;
  roots.at(-1).props.onEscapeKeyDown(keyboardEvent(new FocusTarget(focused.key)));
  focused.props.onOpenChange(false);
  notify("warning", "After Escape", "Consumer callback still works");
  const current = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
  assert.deepEqual(current.map((node) => node.key), ["toast-2", "toast-3"]);
  assert.equal(elements(current[1]).find((node) => node.type === ToastPrimitive.Title).props.children, "After Escape");
});

for (const locale of ["ko", "en", "ja", "zh"]) {
  test(`actual ${locale} toast uses Radix Close with the existing translated label and lucide icon`, () => {
    const ui = harness(locale); ui.toast("warning", "Title", "Description");
    const nodes = elements(ui.render());
    const close = nodes.find((node) => node.type === ToastPrimitive.Close);
    assert(close);
    assert.equal(close.props.type, "button");
    assert.equal(close.props["aria-label"], ui.closeLabel);
    assert.equal(close.props.title, ui.closeLabel);
    for (const token of ["h-8", "w-8", "shrink-0", "hover:bg-bg-elevated", "focus-visible:outline-none", "focus-visible:ring-2"]) assert(tokens(close).has(token));
    for (const key of ["onClick", "onKeyDown", "asChild", "disabled"]) assert(!Object.hasOwn(close.props, key));
    const icon = elements(close).find((node) => node.type === X);
    assert(icon); assert.equal(icon.props["aria-hidden"], "true");
  });
}

test("all four actual toast types have an opaque surface and preserve their distinct border colors", () => {
  const ui = harness();
  const types = [["success", "border-emerald-500"], ["error", "border-red-500"], ["warning", "border-amber-500"], ["info", "border-blue-500"]];
  for (const [type] of types) ui.toast(type, "LongTitle".repeat(40), "LongDescription".repeat(60));
  const roots = elements(ui.render()).filter((node) => node.type === ToastPrimitive.Root);
  assert.equal(roots.length, types.length);
  for (const [index, root] of roots.entries()) {
    for (const token of ["flex", "min-w-0", "shrink-0", "items-start", "gap-3", "bg-bg-surface", types[index][1]]) assert(tokens(root).has(token));
    assert(![...tokens(root)].some((token) => /bg-.*\/|opacity-|bg-bg-(secondary|tertiary)/.test(token)));
    const [text, close] = [root.props.children].flat();
    assert.equal(text.type, "div");
    assert.equal(close.type, ToastPrimitive.Close);
    for (const token of ["min-w-0", "flex-1"]) assert(tokens(text).has(token));
    for (const type of [ToastPrimitive.Title, ToastPrimitive.Description]) {
      const node = elements(root).find((entry) => entry.type === type);
      assert.equal(node.props.children, type === ToastPrimitive.Title ? "LongTitle".repeat(40) : "LongDescription".repeat(60));
      for (const token of ["whitespace-normal", "break-words"]) assert(tokens(node).has(token));
      for (const token of ["truncate", "whitespace-nowrap", "overflow-hidden", "line-clamp-1"]) assert(!tokens(node).has(token));
    }
  }
  const css = readFileSync(new URL("../src/app/globals.css", import.meta.url), "utf8");
  assert.match(css, /--color-bg-surface:\s*#[a-f0-9]{6}\b/i);
  assert.match(css, /--color-bg-elevated:\s*#[a-f0-9]{6}\b/i);
});

test("actual viewport uses its fixed containing block height and keeps scrolling and root sizing", () => {
  const ui = harness(); ui.toast("info", "Title");
  const nodes = elements(ui.render());
  const viewport = nodes.find((node) => node.type === ToastPrimitive.Viewport);
  const height = "max-h-[calc(100%-2rem)]";
  assert.deepEqual([...tokens(viewport)].filter((token) => token.startsWith("max-h-")), [height]);
  assert(!tokens(viewport).has("max-h-[calc(100dvh-2rem)]"));
  for (const token of ["fixed", "bottom-4", "left-4", "right-4", "overflow-y-auto", "flex-col", "max-w-[calc(100vw-2rem)]", "sm:w-96"]) assert(tokens(viewport).has(token));
  assert(tokens(nodes.find((node) => node.type === ToastPrimitive.Root)).has("shrink-0"));
});

test("actual provider preserves Radix timing, pause and swipe and adds only the scoped Escape guard", () => {
  const ui = harness(); ui.toast("info", "Title");
  const nodes = elements(ui.render());
  const provider = nodes.find((node) => node.type === ToastPrimitive.Provider);
  assert.equal(provider.props.swipeDirection, "right");
  assert.deepEqual(Object.keys(provider.props).sort(), ["children", "swipeDirection"]);
  const root = nodes.find((node) => node.type === ToastPrimitive.Root);
  assert.deepEqual(Object.keys(root.props).sort(), ["children", "className", "data-toast-id", "onEscapeKeyDown", "onOpenChange"]);
  assert.equal(typeof root.props.onEscapeKeyDown, "function");
  assert(!/setTimeout|setInterval|onSwipeEnd|onPause|onResume/.test(source));
  const radix = readFileSync(require.resolve("@radix-ui/react-toast"), "utf8");
  assert.match(radix, /duration = 5e3/);
  assert.match(radix, /wrapper\.addEventListener\("focusin", handlePause\)/);
  assert.match(radix, /wrapper\.addEventListener\("pointermove", handlePause\)/);
  assert.match(radix, /window\.addEventListener\("blur", handlePause\)/);
  assert.match(radix, /window\.addEventListener\("focus", handleResume\)/);
  assert.match(radix, /onSwipeEnd:[\s\S]*?setOpen\(false\)/);
  assert.match(radix, /onEscapeKeyDown:[\s\S]*?handleClose\(\)/);
  assert.match(radix, /onClick:.*composeEventHandlers\)\(props\.onClick, interactiveContext\.onClose\)/);
});
