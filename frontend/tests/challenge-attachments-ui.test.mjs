import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import vm from "node:vm";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url), source = readFileSync(new URL("../src/components/admin/challenges/StepFiles.tsx", import.meta.url), "utf8");
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
const walk = node => Array.isArray(node) ? node.flatMap(walk) : React.isValidElement(node) ? [node, ...walk(node.props.children)] : [];
function render(locale, overrides = {}) {
  const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8")), selected = [], clicks = [], calls = [];
  const context = { exports: {}, require(name) {
    if (name === "react") return { ...React, useState: () => [false, () => {}], useRef: () => ({ current: { click: () => clicks.push("browse") } }) };
    if (name === "next-intl") return { useTranslations: namespace => key => { assert.equal(typeof messages[namespace]?.[key], "string", `${namespace}.${key}`); return messages[namespace][key]; } };
    if (name === "lucide-react") return Object.fromEntries(["ArrowDown", "ArrowUp", "File", "FolderUp", "LoaderCircle", "RefreshCw", "RotateCcw", "Trash2"].map(key => [key, "svg"]));
    return require(name);
  } };
  vm.runInNewContext(code, context, { timeout: 1000 });
  const controller = Object.fromEntries(["check", "remove", "retry", "move"].map(key => [key, (...args) => calls.push([key, ...args])])); controller.add = files => selected.push(files);
  const props = { attachments: { controller, items: [{ id: "own-upload", name: "<script>原本.zip", byte_size: 20, state: "pending", existing: false, ready_for_attach: false, canRetry: false, reconcile_required: true, error: null }], error: null, conflict: false }, legacy: [{ id: null, legacy_index: 0, name: "missing.txt", state: "unavailable" }], disabled: false, onRemoveLegacy: index => calls.push(["legacy", index]), onReload: () => calls.push(["reload"]), ...overrides };
  const node = context.exports.StepFiles(props); return { node, all: walk(node), selected, clicks, calls, html: renderToStaticMarkup(node), messages };
}
for (const locale of ["ko", "en", "ja", "zh"]) test(`${locale} attachment controls have real localized labels, escaped names and keyboard access`, () => {
  const h = render(locale), drop = h.all.find(node => node.props.role === "button"); assert.equal(drop.props.tabIndex, 0); assert.equal(drop.props["aria-label"], h.messages.challenge.attachmentFiles);
  drop.props.onKeyDown({ key: "Enter", preventDefault() {} }); assert.deepEqual(h.clicks, ["browse"]);
  assert(!h.html.includes("<script>原本")); assert(h.html.includes("&lt;script&gt;原本.zip"));
  for (const button of h.all.filter(node => node.type === "button")) { assert.equal(button.props.type, "button"); assert(button.props["aria-label"] || button.props.children); }
  const status = h.all.find(node => node.props.role === "status"); assert(renderToStaticMarkup(status).includes(h.messages.challenge.attachmentPending));
  const query = h.all.find(node => node.type === "button" && node.props.title === h.messages.challenge.attachmentCheckStatus); query.props.onClick(); assert.deepEqual(h.calls[0], ["check", "own-upload"]);
  assert(!h.all.some(node => node.type === "button" && node.props.title === h.messages.common.retry));
});
test("native File input clears its value before selection so choosing the same file again works", () => {
  const h = render("ko"), input = h.all.find(node => node.type === "input"), original = new File(["bytes"], "same.bin"), target = { files: [original], value: "same.bin" };
  input.props.onChange({ currentTarget: target }); assert.equal(target.value, ""); target.value = "same.bin"; input.props.onChange({ currentTarget: target });
  assert.equal(h.selected.length, 2); assert.equal(h.selected[0][0], original); assert.equal(h.selected[1][0], original);
});
test("busy controls cannot invoke add/drop or keyboard browsing and all mutations stay disabled", () => {
  const h = render("ko", { disabled: true }), drop = h.all.find(node => node.props.role === "button");
  drop.props.onKeyDown({ key: " ", preventDefault() {} }); drop.props.onDrop({ preventDefault() {}, dataTransfer: { files: [new File(["a"], "x")] } });
  assert.equal(drop.props.tabIndex, -1); assert.equal(h.selected.length, 0); assert.equal(h.clicks.length, 0);
  assert(h.all.filter(node => node.type === "button").every(node => node.props.disabled));
});
test("Wizard keeps File RAM ownership, owner-bound safe restore, attachment-only dirty and parent save leases", () => {
  const wizard = readFileSync(new URL("../src/components/admin/challenges/ChallengeWizard.tsx", import.meta.url), "utf8"), hook = readFileSync(new URL("../src/lib/use-challenge-attachments.ts", import.meta.url), "utf8");
  assert(wizard.includes("const attachmentFiles = useRef(new Map<string, File>())"));
  assert(wizard.includes("stored.ownerId !== user.id")); assert(wizard.includes("attachments.controller.restore(stored.attachmentDraft)"));
  assert(wizard.includes("JSON.stringify(data.files) !== JSON.stringify(init.files)")); assert(wizard.includes("attachments.dirty"));
  assert(wizard.indexOf('lease = operation.claim("save")') < wizard.lastIndexOf("attachments.controller.prepareSave(lease"));
  assert(wizard.includes("attachments.controller.acceptSaved(saved, lease)")); assert(!wizard.includes("expected_attachment_revision"));
  assert(hook.includes('form.append("file", body as File)')); assert(hook.includes('method: "PUT", requestOwner: owner')); assert(!hook.includes("localStorage"));
});
test("actual Wizard restore callback checks the captured owner epoch before parent apply or storage removal", async () => {
  const wizard = readFileSync(new URL("../src/components/admin/challenges/ChallengeWizard.tsx", import.meta.url), "utf8"), ast = ts.createSourceFile("Wizard.tsx", wizard, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX), effects = [];
  function visit(node) { if (ts.isCallExpression(node) && node.expression.getText(ast) === "useEffect" && node.arguments[0]?.getText(ast).includes('localStorage.getItem("challenge-draft")')) effects.push(node.arguments[0].getText(ast)); ts.forEachChild(node, visit); }
  visit(ast); assert.equal(effects.length, 1);
  for (const outcome of ["same", "changed", "unmounted"]) {
    let epoch = 1, resolve; const restore = new Promise(yes => { resolve = yes; }), writes = [], ready = [{ id: "verified", name: "safe.bin" }], saved = JSON.stringify({ ownerId: "own", data: { title: "saved draft", attachments: [{ id: "untrusted" }] }, attachmentDraft: { draftId: "draft" } });
    const context = { initialMode: "create", initialData: undefined, initialChallengeId: undefined, user: { id: "own" },
      dataRef: { current: { title: "before", attachments: ready, attachmentRevision: 2 } }, attachments: { controller: { restore: () => restore } },
      localStorage: { getItem: () => saved, removeItem: () => writes.push("remove") }, captureApiRequestOwner: () => ({ epoch }), isCurrentApiRequestOwner: owner => owner.epoch === epoch,
      setData: value => writes.push(value), toast: () => writes.push("toast"), tchall: key => key };
    const output = ts.transpileModule(`globalThis.effect = ${effects[0]};`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
    vm.runInNewContext(output, context, { timeout: 1000 }); const cleanup = context.effect(); assert.equal(writes.length, 0);
    if (outcome === "changed") epoch = 2; if (outcome === "unmounted") cleanup(); resolve(true); await restore; await Promise.resolve();
    if (outcome === "same") { assert.equal(context.dataRef.current.title, "saved draft"); assert.equal(context.dataRef.current.attachments, ready); assert.deepEqual(writes.slice(1), ["toast", "remove"]); }
    else { assert.equal(context.dataRef.current.title, "before"); assert.equal(writes.length, 0); }
  }
});
test("filename-only legacy drafts remain visible as unavailable and explicit removal renumbers only legacy indices", () => {
  const wizard = readFileSync(new URL("../src/components/admin/challenges/ChallengeWizard.tsx", import.meta.url), "utf8"), ast = ts.createSourceFile("Wizard.tsx", wizard, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let expression, remove; function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(ast) === "legacyAttachments") expression = node.initializer.getText(ast);
    if (ts.isJsxAttribute(node) && node.name.getText(ast) === "onRemoveLegacy") remove = node.initializer.expression.getText(ast);
    ts.forEachChild(node, visit);
  } visit(ast); assert(expression && remove);
  const privateFile = { id: "private-owned", legacy_index: null, name: "private.zip", state: "ready" }, known = { id: null, legacy_index: 1, name: "original.txt", state: "ready", download_path: "/api/learn/challenges/7/files/1" };
  const data = { files: ["lost/path/name.zip", "old-object-path"], attachments: [privateFile, known] }, writes = [], context = { data, dataRef: { current: data }, operation: { claim: () => ({}), release: () => writes.push("release") }, updateData(partial) { context.dataRef.current = { ...context.dataRef.current, ...partial }; } };
  vm.runInNewContext(ts.transpileModule(`globalThis.rows = ${expression}; globalThis.remove = ${remove};`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText, context, { timeout: 1000 });
  assert.equal(context.rows[0].name, "name.zip"); assert.equal(context.rows[0].state, "unavailable"); assert.equal(context.rows[0].download_path, null); assert.equal(context.rows[1], known);
  assert.deepEqual(data.files, ["lost/path/name.zip", "old-object-path"]); context.remove(0);
  assert.equal(JSON.stringify(context.dataRef.current.files), '["old-object-path"]'); assert.equal(context.dataRef.current.attachments[0], privateFile); assert.equal(context.dataRef.current.attachments[1].legacy_index, 0); assert.equal(context.dataRef.current.attachments[1].download_path, known.download_path); assert.deepEqual(writes, ["release"]);
});
