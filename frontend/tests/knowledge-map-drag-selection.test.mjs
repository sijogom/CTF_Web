import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/components/learn/KnowledgeGraphMap.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("KnowledgeGraphMap.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
assert.equal(ast.parseDiagnostics.length, 0);
const declarations = new Map(), flowElements = [], canvasElements = [];
function attribute(node, name) {
  return node.attributes.properties.find((item) => ts.isJsxAttribute(item) && item.name.getText(ast) === name);
}
function visit(node) {
  if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
    const value = node.initializer;
    if (ts.isCallExpression(value) && value.expression.getText(ast) === "useCallback") declarations.set(node.name.text, value.arguments[0].getText(ast));
  }
  if (ts.isJsxOpeningElement(node)) {
    if (node.tagName.getText(ast) === "ReactFlow") flowElements.push(node);
    if (attribute(node, "ref")?.initializer?.expression?.getText(ast) === "canvasRef") canvasElements.push(node);
  }
  ts.forEachChild(node, visit);
}
visit(ast);
const plain = (value) => JSON.parse(JSON.stringify(value));

function harness({ selected = false, focusTarget = false } = {}) {
  const first = { id: "course:1", type: "course", entity_id: "1", name: "Course" };
  const target = { id: "challenge:2", type: "challenge", entity_id: "2", name: "Challenge" };
  const nodes = [first, target].map((mapNode, index) => ({ id: mapNode.id, selected: index === 1 && selected, position: { x: 100 + 220 * index, y: 120 }, data: { mapNode } }));
  const edge = { id: "edge:1", selected: false, data: { mapEdge: { id: "edge:1", source: first.id, target: target.id } } };
  const state = { graph: { graph_revision: "r1", focus: focusTarget ? target : first }, focus: focusTarget ? target : first,
    initialFocusKey: "course:1", selectedNodeId: selected ? target.id : null, selectedEdge: null, detailsOpen: false };
  const focusChanges = [], selectedIds = [], viewportIntents = [];
  class Element {
    constructor(kind, id) { this.kind = kind; this.dataset = { id }; }
    closest(selector) {
      if (this.kind === "node" && selector.includes("react-flow__node")) return this;
      if (this.kind === "edge" && selector.includes("react-flow__edge")) return this;
      return null;
    }
  }
  const context = { Element, Boolean,
    graphRequestRef: { current: { id: 7 } }, userSelectionIntentRef: { current: null },
    markViewportControlIntent: (element) => viewportIntents.push(element),
    selectNode: (id) => { selectedIds.push(id); state.selectedNodeId = id; },
    setSelectedEdge: (value) => { state.selectedEdge = value; },
    setDetailsOpen: (value) => { state.detailsOpen = value; },
    setFocus: (value) => { state.focus = value; },
    onFocusChange: (value) => focusChanges.push(plain(value)),
  };
  const names = ["commitNodeSelection", "commitEdgeSelection", "clearSelection", "commitResultingSelection", "intentMatchesCurrentGraph", "onNodeClick", "markUserSelectionIntent", "onCanvasPointerDownCapture", "onCanvasKeyDownCapture", "onCanvasKeyDown", "onSelectionStart", "onSelectionEnd", "onSelectionChange"];
  for (const name of names) assert(declarations.has(name), name);
  const program = `globalThis.bind = (graph, focus, initialFocusKey, nodes, edges) => {
    ${names.map((name) => `const ${name} = ${declarations.get(name)};`).join("\n")}
    return { ${names.join(", ")} };
  };`;
  vm.runInNewContext(ts.transpileModule(program, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context, { timeout: 1000 });
  let handlers;
  const rebind = () => { handlers = context.bind(state.graph, state.focus, state.initialFocusKey, nodes, [edge]); };
  rebind();
  return { state, nodes, edge, first, target, context, focusChanges, selectedIds, viewportIntents, rebind,
    get handlers() { return handlers; },
    targetElement: new Element("node", target.id),
    pointer(element, keys = {}) { handlers.onCanvasPointerDownCapture({ target: element, ctrlKey: false, metaKey: false, ...keys }); },
    keyCapture(key, keys = {}) { handlers.onCanvasKeyDownCapture({ target: this.targetElement, key, ctrlKey: false, metaKey: false, ...keys }); },
    selection(selectedNodes = [], selectedEdges = []) { handlers.onSelectionChange({ nodes: selectedNodes, edges: selectedEdges }); },
  };
}

test("the actual ReactFlow disables drag-start selection without disabling drag, pan, or rectangle selection", () => {
  assert.equal(flowElements.length, 1);
  const flow = flowElements[0];
  assert.equal(attribute(flow, "selectNodesOnDrag")?.initializer?.expression?.kind, ts.SyntaxKind.FalseKeyword);
  assert.equal(flow.attributes.properties.filter((item) => ts.isJsxAttribute(item) && item.name.getText(ast) === "selectNodesOnDrag").length, 1);
  assert(!flow.attributes.properties.some(ts.isJsxSpreadAttribute), "a later spread must not override the explicit drag selection contract");
  for (const name of ["panOnDrag", "panOnScroll", "selectionOnDrag"]) {
    const property = attribute(flow, name);
    assert(property, `${name} remains enabled`);
    assert.equal(property.initializer, undefined, name);
  }
  assert(attribute(flow, "nodesDraggable"), "manual dragging remains wired");
  for (const name of ["onNodeClick", "onSelectionChange", "onSelectionStart", "onSelectionEnd"]) assert.equal(attribute(flow, name)?.initializer?.expression?.getText(ast), name);
  assert.equal(canvasElements.length, 1);
  for (const name of ["onPointerDownCapture", "onKeyDownCapture", "onKeyDown"]) {
    const callback = name === "onPointerDownCapture" ? "onCanvasPointerDownCapture" : name === "onKeyDownCapture" ? "onCanvasKeyDownCapture" : "onCanvasKeyDown";
    assert.equal(attribute(canvasElements[0], name)?.initializer?.expression?.getText(ast), callback);
  }
});

test("pressing and dragging an unselected node with no resulting selection cannot change focus or URL intent", () => {
  const h = harness();
  h.pointer(h.targetElement);
  assert.equal(h.context.userSelectionIntentRef.current.expectedTargetSelected, true);
  assert.equal(h.focusChanges.length, 0, "pointer-down must not commit a new focus");
  h.selection();
  h.handlers.onNodeClick({}, h.nodes[1]);
  assert.deepEqual(h.selectedIds, []);
  assert.deepEqual(h.focusChanges, []);
  assert.equal(h.state.focus.id, "course:1");
  assert.equal(h.state.detailsOpen, false);
});

test("a native click selection commits once and the subsequent click callback cannot duplicate focus history", () => {
  const h = harness();
  h.pointer(h.targetElement);
  h.selection();
  assert.deepEqual(h.focusChanges, []);
  h.nodes[1].selected = true;
  h.selection([h.nodes[1]]);
  h.handlers.onNodeClick({}, h.nodes[1]);
  h.selection([h.nodes[1]]);
  assert.deepEqual(h.selectedIds, ["challenge:2"]);
  assert.deepEqual(h.focusChanges, [{ type: "challenge", id: "2" }]);
  assert.equal(h.state.detailsOpen, true);
  assert.equal(h.context.userSelectionIntentRef.current, null);
});

test("clicking an already selected focus node reopens details without another focus transition", () => {
  const h = harness({ selected: true, focusTarget: true });
  h.pointer(h.targetElement);
  assert.equal(h.context.userSelectionIntentRef.current.expectedTargetSelected, null);
  h.selection([h.nodes[1]]);
  h.handlers.onNodeClick({}, h.nodes[1]);
  h.handlers.onNodeClick({}, h.nodes[1]);
  assert.deepEqual(h.selectedIds, ["challenge:2"]);
  assert.deepEqual(h.focusChanges, []);
  assert.equal(h.state.detailsOpen, true);
});

test("Enter and Space preserve keyboard selection and consume their focus intent once", () => {
  for (const key of ["Enter", " "]) {
    const h = harness();
    h.keyCapture(key);
    h.nodes[1].selected = true;
    h.selection([h.nodes[1]]);
    h.handlers.onCanvasKeyDown({ key });
    assert.deepEqual(h.selectedIds, ["challenge:2"], key);
    assert.deepEqual(h.focusChanges, [{ type: "challenge", id: "2" }], key);
    assert.equal(h.state.detailsOpen, true, key);
  }
});

test("keyboard reopening and Escape retain the existing selected-node behavior", () => {
  const h = harness({ selected: true, focusTarget: true });
  h.keyCapture("Enter");
  h.handlers.onCanvasKeyDown({ key: "Enter" });
  assert.equal(h.state.detailsOpen, true);
  assert.deepEqual(h.focusChanges, []);
  h.keyCapture("Escape");
  h.nodes[1].selected = false;
  h.selection();
  h.handlers.onCanvasKeyDown({ key: "Escape" });
  assert.equal(h.state.detailsOpen, false);
  assert.equal(h.state.selectedNodeId, null);
  assert.deepEqual(h.focusChanges, []);
});

test("an intent from an old graph request, route focus, or graph focus cannot select the new graph", () => {
  for (const change of [
    (h) => { h.context.graphRequestRef.current.id++; },
    (h) => { h.state.initialFocusKey = "course:other"; },
    (h) => { h.state.graph = { ...h.state.graph, focus: { ...h.first, id: "course:other" } }; },
  ]) {
    const h = harness();
    h.pointer(h.targetElement);
    change(h); h.rebind();
    h.selection([h.nodes[1]]);
    h.handlers.onNodeClick({}, h.nodes[1]);
    assert.deepEqual(h.selectedIds, []);
    assert.deepEqual(h.focusChanges, []);
    assert.equal(h.context.userSelectionIntentRef.current, null);
  }
});

test("dragging a selected group does not refocus it and rectangle selection keeps its existing commit path", () => {
  const h = harness({ selected: true });
  h.nodes[0].selected = true;
  h.pointer(h.targetElement);
  h.selection(h.nodes);
  assert.deepEqual(h.selectedIds, []);
  assert.deepEqual(h.focusChanges, []);
  h.handlers.onSelectionStart();
  h.selection(h.nodes);
  h.handlers.onSelectionEnd();
  assert.deepEqual(h.selectedIds, ["challenge:2"]);
  assert.deepEqual(h.focusChanges, [{ type: "challenge", id: "2" }]);
  assert.equal(h.context.userSelectionIntentRef.current, null);
});
