import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import { Download } from "lucide-react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const source = readFileSync(new URL("../src/app/[locale]/market/[id]/page.tsx", import.meta.url), "utf8");
const syntax = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
const buttons = [];
function visit(node) {
  if (ts.isJsxElement(node) && node.openingElement.tagName.getText(syntax) === "button") {
    const handler = node.openingElement.attributes.properties.find((attribute) =>
      ts.isJsxAttribute(attribute) && attribute.name.text === "onClick");
    if (handler?.initializer?.expression?.getText(syntax) === "handleDownload") buttons.push(node);
  }
  ts.forEachChild(node, visit);
}
visit(syntax);
assert.equal(buttons.length, 2, "buyer and seller download controls must both be covered");
const require = createRequire(import.meta.url);

for (const [index, role] of ["buyer", "seller"].entries()) {
  test(`${role} download keeps the actual filename, wrapping label, and fixed icon`, () => {
    const filename = "very_long_unbroken_marketplace_original_filename_0123456789.txt";
    const handleDownload = () => {};
    const context = { exports: {}, require, Download, handleDownload, item: { original_filename: filename }, t: () => "download" };
    const program = ts.transpileModule(`globalThis.button = (${buttons[index].getText(syntax)});`, {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
    }).outputText;
    vm.runInNewContext(program, context, { timeout: 1000 });
    const button = context.button;
    assert.equal(button.props.onClick, handleDownload);
    assert.ok(button.props.className.split(" ").includes("min-w-0"));
    assert.ok(button.props.className.split(" ").includes("px-3"));
    const [icon, label] = button.props.children;
    assert.equal(icon.type, Download);
    assert.ok(icon.props.className.split(" ").includes("shrink-0"));
    assert.equal(label.type, "span");
    assert.ok(label.props.className.split(" ").includes("min-w-0"));
    assert.ok(label.props.className.split(" ").includes("[overflow-wrap:anywhere]"));
    assert.ok(renderToStaticMarkup(button).includes(filename), "the full filename must remain readable, not truncated");
  });
}
