import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import { isValidElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
function load(relative, resolve = require) {
  const source = readFileSync(new URL(relative, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const context = { exports: {}, require: resolve };
  vm.runInNewContext(compiled, context, { timeout: 1000 });
  return context.exports;
}
const utils = load("../src/lib/utils.ts");
const { WizardStepper } = load("../src/components/admin/WizardStepper.tsx", (name) => name === "@/lib/utils" ? utils : require(name));
const keys = ["stepBasicInfo", "stepFlag", "stepFiles", "stepEnv", "stepHints", "stepPreview"];
const tokens = (node) => new Set((node.props.className ?? "").split(/\s+/));
const children = (node) => [node.props.children].flat().filter(isValidElement);
const buttons = (tree) => children(children(tree)[0]);

for (const locale of ["ko", "en", "ja", "zh"]) {
  test(`actual ${locale} stepper preserves all six labels, navigation boundaries and state colors`, () => {
    const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
    const steps = keys.map((key) => ({ label: messages.admin[key], icon: "" }));
    for (let currentStep = 0; currentStep < steps.length; currentStep++) {
      const clicks = [];
      const tree = WizardStepper({ steps, currentStep, onStepClick: (index) => clicks.push(index) });
      const stepButtons = buttons(tree);
      assert.equal(stepButtons.length, 6);
      for (const [index, button] of stepButtons.entries()) {
        assert.equal(button.type, "button");
        assert.equal(button.props.disabled, index > currentStep + 1);
        for (const token of ["flex", "min-w-0", "w-full", "flex-wrap", "items-center", "gap-2"]) assert(tokens(button).has(token));
        const [number, label] = children(button);
        for (const token of ["h-6", "w-6", "shrink-0"]) assert(tokens(number).has(token));
        assert.equal(number.props.children, index < currentStep ? "\u2713" : index + 1);
        for (const token of ["min-w-0", "whitespace-normal", "break-words", "text-left"]) assert(tokens(label).has(token));
        assert.equal(label.props.children, steps[index].label);
        assert(tokens(button).has(index === currentStep ? "bg-accent-primary" : index < currentStep ? "text-accent-success" : "text-text-muted"));
        if (!button.props.disabled) button.props.onClick();
      }
      assert.deepEqual(clicks, steps.map((_, index) => index).filter((index) => index <= currentStep + 1));
      const html = renderToStaticMarkup(tree);
      for (const step of steps) assert(html.includes(step.label));
    }
  });
}

test("actual stepper queries its named parent content container, not viewport breakpoints or its own grid", () => {
  const tree = WizardStepper({ steps: keys.map((label) => ({ label, icon: "" })), currentStep: 0 });
  for (const token of ["@container/wizard", "mb-8", "min-w-0", "w-full"]) assert(tokens(tree).has(token));
  assert.equal(children(tree).length, 1);
  const grid = children(tree)[0];
  assert.equal(grid.type, "div");
  for (const token of ["grid", "grid-cols-2", "gap-2", "@min-[28rem]/wizard:grid-cols-3", "@min-[48rem]/wizard:grid-cols-6"]) assert(tokens(grid).has(token));
  assert(!tokens(tree).has("grid"));
  assert(!tokens(grid).has("@container/wizard"));
  for (const element of [tree, grid]) {
    assert(!tokens(element).has("flex"));
    assert([...tokens(element)].every((token) => !/^(?:sm|lg):/.test(token)));
  }
});

test("actual stepper still tolerates an omitted navigation callback", () => {
  const tree = WizardStepper({ steps: [{ label: "First", icon: "" }], currentStep: 0 });
  assert.doesNotThrow(() => buttons(tree)[0].props.onClick());
});

test("actual stepper preserves long label text and React output encoding without clipping classes", () => {
  const label = "<unsafe>" + "LongUnbrokenLabel".repeat(16);
  const tree = WizardStepper({ steps: [{ label, icon: "" }], currentStep: 0 });
  const text = children(buttons(tree)[0])[1];
  assert.equal(text.props.children, label);
  for (const token of ["truncate", "whitespace-nowrap", "overflow-hidden", "line-clamp-1"]) assert(!tokens(text).has(token));
  const html = renderToStaticMarkup(tree);
  assert(html.includes("&lt;unsafe&gt;"));
  assert(!html.includes("<unsafe>"));
});

test("the English environment label can move whole to another flex line without removing the long-word fallback", () => {
  const messages = JSON.parse(readFileSync(new URL("../messages/en.json", import.meta.url), "utf8"));
  const tree = WizardStepper({ steps: keys.map((key) => ({ label: messages.admin[key], icon: "" })), currentStep: 2 });
  const button = buttons(tree)[3], [number, label] = children(button);
  assert.equal(label.props.children, "Environment");
  assert(tokens(button).has("flex-wrap"));
  assert(!tokens(button).has("flex-nowrap"));
  for (const token of ["h-6", "w-6", "shrink-0"]) assert(tokens(number).has(token));
  for (const token of ["min-w-0", "whitespace-normal", "break-words"]) assert(tokens(label).has(token));
  assert(!tokens(label).has("whitespace-nowrap"));
  assert.equal(button.props.disabled, false);
});
