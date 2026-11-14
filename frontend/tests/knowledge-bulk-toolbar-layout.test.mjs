import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import { isValidElement } from "react";
import ts from "typescript";

const source = readFileSync(new URL("../src/components/admin/knowledge/KnowledgeProposalBulkReview.tsx", import.meta.url), "utf8");
const require = createRequire(import.meta.url);
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const context = { exports: {}, require: (name) => {
  if (name === "next-intl") return { useTranslations: () => (key, options) => options?.count === undefined ? key : `${key}:${options.count}` };
  if (name === "@/components/ui/Button") return { Button: "button" };
  return require(name);
} };
vm.runInNewContext(compiled, context, { timeout: 1000 });
const Component = context.exports.KnowledgeProposalBulkReview;

function elements(element) {
  if (Array.isArray(element)) return element.flatMap(elements);
  if (!isValidElement(element)) return [];
  return [element, ...elements(element.props.children)];
}

function render(overrides = {}) {
  return elements(Component({ selectedCount: 3, allSelected: false, reason: "current draft", disabled: false,
    canApprove: true, results: [], onSelectPage: () => {}, onReasonChange: () => {}, onReview: () => {}, onReload: () => {}, ...overrides }));
}

function reasonInput(tree) {
  return tree.find((element) => element.type === "input" && element.props["aria-label"] === "reason");
}

test("the actual reason input occupies its own full-width row below sm", () => {
  const tree = render();
  const input = reasonInput(tree);
  const classes = input.props.className.split(" ");
  for (const token of ["w-full", "min-w-0", "flex-none"]) assert(classes.includes(token), `missing mobile layout token ${token}`);
  assert(!classes.includes("flex-1"), "an unbounded mobile flex shrink must not collapse the reason input again");
  const row = tree.find((element) => element.type === "div" && [element.props.children].flat().includes(input));
  assert.equal(row.props.className, "flex flex-wrap gap-2", "the input's actual parent must permit review actions to wrap");
});

test("wide layouts retain a container-safe reading width and can wrap actions", () => {
  const classes = reasonInput(render()).props.className.split(" ");
  for (const token of ["sm:w-auto", "sm:min-w-[min(100%,16rem)]", "sm:flex-1"]) assert(classes.includes(token), `missing wide layout token ${token}`);
  assert(!classes.includes("truncate"));
});

test("reason value limit disabled state and draft callback remain unchanged", () => {
  const changes = [];
  const input = reasonInput(render({ reason: "검토 사유 원문", disabled: true, onReasonChange: (value) => changes.push(value) }));
  assert.equal(input.props.value, "검토 사유 원문");
  assert.equal(input.props.maxLength, 1000);
  assert.equal(input.props.disabled, true);
  input.props.onChange({ target: { value: "새 검토 사유" } });
  assert.deepEqual(changes, ["새 검토 사유"]);
});

test("approve reject and retire retain their handlers and independent-review guard", () => {
  const actions = [];
  const tree = render({ canApprove: false, onReview: (action) => actions.push(action) });
  for (const action of ["approve", "reject", "retire"]) {
    const button = tree.find((element) => element.type === "button" && element.props.children === action);
    assert.equal(button.props.disabled, action === "approve");
    button.props.onClick();
  }
  assert.deepEqual(actions, ["approve", "reject", "retire"]);
  for (const overrides of [{ selectedCount: 0 }, { disabled: true }]) {
    const buttons = render(overrides).filter((element) => element.type === "button");
    assert(buttons.every((element) => element.props.disabled));
  }
});

test("selection result wrapping full names errors and reload remain unchanged", () => {
  const selected = [], reloads = [];
  const results = [{ proposal_id: "own-proposal", status: "failed", name: "작업 소유 출발 과정 → 작업 소유 도착 과정", error: "공개 조건을 확인하세요." }];
  const tree = render({ results, onSelectPage: (value) => selected.push(value), onReload: () => reloads.push(true) });
  const checkbox = tree.find((element) => element.type === "input" && element.props.type === "checkbox");
  checkbox.props.onChange({ target: { checked: true } });
  assert.deepEqual(selected, [true]);
  const item = tree.find((element) => element.type === "li");
  assert.equal(item.props.className, "break-words");
  assert(item.props.children.includes(results[0].name));
  assert(elements(item).some((element) => element.type === "span" && element.props.children === results[0].error));
  const reload = tree.find((element) => element.type === "button" && element.props.children === "retry");
  reload.props.onClick();
  assert.deepEqual(reloads, [true]);
});

test("restored bulk controls leave state eligibility to per-item server results", () => {
  for (const canApprove of [true, false]) {
    const tree = render({ canApprove });
    for (const action of ["approve", "reject", "retire"]) {
      const button = tree.find((element) => element.type === "button" && element.props.children === action);
      assert.equal(button.props.disabled, action === "approve" && !canApprove, action);
    }
  }
});
