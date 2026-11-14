import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { createKnowledgeMapPositions, fitKnowledgeMapViewport } from "../src/lib/knowledge-map-layout.mjs";
import {
  KNOWLEDGE_MAP_POSITION_STORAGE_KEY as POSITION_KEY,
  KNOWLEDGE_MAP_POSITION_LAYOUT_VERSION as POSITION_VERSION,
  KNOWLEDGE_MAP_VIEWPORT_STORAGE_KEY as VIEWPORT_KEY,
  KNOWLEDGE_MAP_VIEWPORT_LAYOUT_VERSION as VIEWPORT_VERSION,
  knowledgeMapPositionKey as positionMemoryKey,
  knowledgeMapViewportKey as viewportMemoryKey,
  persistKnowledgeMapLayout,
  recoverKnowledgeMapLayout,
} from "../src/lib/knowledge-map-arrangement.mjs";

const source = readFileSync(new URL("../src/components/learn/KnowledgeGraphMap.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("KnowledgeGraphMap.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
assert.equal(ast.parseDiagnostics.length, 0);
function declaration(name, unwrapCallback = true) {
  const matches = [];
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(ast) === name) matches.push(node.initializer);
    ts.forEachChild(node, visit);
  }
  visit(ast);
  assert.equal(matches.length, 1, name);
  const value = matches[0];
  return (unwrapCallback && ts.isCallExpression(value) && value.expression.getText(ast) === "useCallback" ? value.arguments[0] : value).getText(ast);
}
const plain = (value) => JSON.parse(JSON.stringify(value));
const tick = () => new Promise((resolve) => setImmediate(resolve));

function harness({ compact = false, saved = true } = {}) {
  const graph = {
    graph_revision: "r1", focus: { id: "course:1", type: "course", entity_id: "1" },
    nodes: [{ id: "course:1", type: "course" }, { id: "lesson:1", type: "lesson", distance: 1 }],
    edges: [{ id: "edge:1", source: "course:1", target: "lesson:1" }],
    applied_filters: { depth: 1, types: ["course", "lesson"], relation_types: ["contains"] },
  };
  const state = {
    graph, focus: graph.focus, graphLoading: false, layoutBusy: false, layoutUndo: null, layoutSaveError: null,
    nodes: graph.nodes.map((node, index) => ({ id: node.id, position: { x: 1800 + 1100 * index, y: -60 + 470 * index }, measured: { width: compact ? 148 : 224, height: 116 }, selected: index === 1 })),
    edges: [{ ...graph.edges[0], selected: true }], viewport: { x: -1220, y: 83, zoom: 0.35 },
    selectedNodeId: "lesson:1", detailsOpen: true, url: "?focus_type=course&focus_id=1&map_q=SQL",
  };
  const positionEntries = { "r1:course:2:lesson:1": { x: 8800, y: 9900 } };
  if (saved) for (const node of state.nodes) positionEntries[positionMemoryKey(graph, node.id)] = { ...node.position };
  const data = new Map([
    [POSITION_KEY, JSON.stringify({ version: POSITION_VERSION, entries: positionEntries })],
    [VIEWPORT_KEY, JSON.stringify({ version: VIEWPORT_VERSION, entries: { [viewportMemoryKey(graph)]: state.viewport, "r1:course:2": { x: 91, y: -88, zoom: 1.2 } } })],
  ]);
  const storage = {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, raw) => data.set(key, raw),
    removeItem: (key) => data.delete(key),
  };
  const frames = new Map();
  let frameId = 0;
  const writes = [], requests = [];
  const context = {
    state, Map, Set, Boolean, Number, AbortController, URLSearchParams, DOMException,
    window: { innerWidth: compact ? 390 : 1280, localStorage: storage,
      requestAnimationFrame(callback) { const id = ++frameId; frames.set(id, callback); return id; },
      cancelAnimationFrame(id) { frames.delete(id); } },
    graphRequestRef: { current: { id: 1, controller: null } }, graphDataRequestIdRef: { current: 1 },
    graphRef: { current: graph }, layoutRecoveryRef: { current: [] }, layoutBusyRef: { current: false },
    layoutUndoRef: { current: null }, layoutCameraLockRef: { current: 0 }, layoutCameraFrameRef: { current: null },
    automaticViewportRef: { current: false }, restoredViewportKeyRef: { current: viewportMemoryKey(graph) },
    failedLayoutCommandRef: { current: null }, userSelectionIntentRef: { current: null },
    positionMemoryRef: { current: new Map(Object.entries(positionEntries)) },
    viewportMemoryRef: { current: new Map(Object.entries(JSON.parse(data.get(VIEWPORT_KEY)).entries)) },
    selectedNodeIdRef: { current: "lesson:1" }, compactLayoutRef: { current: compact },
    depth: 1, requestedContentTypes: ["course", "lesson"], requestedRelationTypes: ["contains"],
    graphNodesMeasured: true, flowReady: true, initialFocusKey: "course:1",
    currentGraphMatchesRequest: (value, focus) => value.focus.id === focus.id,
    positionMemoryKey, viewportMemoryKey, persistKnowledgeMapLayout, recoverKnowledgeMapLayout, fitKnowledgeMapViewport,
    createPositions: (value) => createKnowledgeMapPositions(value, { compact }),
    safeCanvasBounds: () => ({ left: compact ? 12 : 72, right: compact ? 306 : 1000, top: compact ? 72 : 24, bottom: 436 }),
    graphToFlowEdges: (value) => value.edges.map((edge) => ({ ...edge })),
    graphToFlowNodes: (value) => value.nodes.map((node) => ({ id: node.id, position: { x: 0, y: 0 } })),
    resolveCanvasPositions: () => new Map(), describeEdge: () => "", relationLabel: () => "",
    nodeLabel: () => "", describeDistance: () => "", describeConnections: () => "", selectNode: () => {},
    writePositionMemory: () => writes.push("drag"), writeViewportMemory: () => writes.push("viewport"),
    onNodesChange: (changes) => writes.push(changes.map((change) => change.type)),
    setGraphError: () => {}, setPageError: () => {},
    ApiError: class ApiError extends Error {}, mergeMapPage: (_, page) => page,
    apiFetch: () => new Promise((resolve) => requests.push(resolve)),
  };
  for (const key of ["graph", "graphLoading", "layoutBusy", "layoutUndo", "layoutSaveError", "nodes", "edges"]) {
    context[`set${key[0].toUpperCase()}${key.slice(1)}`] = (value) => {
      state[key] = typeof value === "function" ? value(state[key]) : value;
    };
  }
  context.flowInstanceRef = { current: {
    getNodes: () => state.nodes, getViewport: () => state.viewport,
    setViewport: (viewport) => {
      state.viewport = { ...viewport };
      context.callbacks.handleViewportMoveEnd(null, viewport);
      return Promise.resolve(true);
    },
  } };
  const names = ["applyLayout", "persistLayout", "handleAutoArrange", "handleUndoArrange", "retryLayoutCommand", "handleNodesChange", "handleViewportMoveEnd", "ensureSelectedNodeVisible", "loadFocus"];
  const program = `globalThis.bind = (graph, focus, graphLoading, layoutBusy, layoutUndo) => {
    const layoutAvailable = ${declaration("layoutAvailable", false)};
    const undoAvailable = ${declaration("undoAvailable", false)};
    ${names.map((name) => `const ${name} = ${declaration(name)};`).join("\n")}
    return { ${names.join(", ")} };
  };`;
  vm.runInNewContext(ts.transpileModule(program, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText, context, { timeout: 1000 });
  const render = () => (context.callbacks = context.bind(state.graph, state.focus, state.graphLoading, state.layoutBusy, state.layoutUndo));
  render();
  return {
    state, context, data, storage, writes, requests, render, frames,
    async settle() {
      await tick();
      while (frames.size) {
        const entries = [...frames]; frames.clear();
        for (const [, callback] of entries) callback();
      }
      render();
    },
  };
}

for (const compact of [false, true]) {
  test(`${compact ? "phone" : "desktop"} product commands arrange and undo the exact prior coordinates/camera once`, async () => {
    const h = harness({ compact });
    const beforeNodes = plain(h.state.nodes), beforeViewport = plain(h.state.viewport);
    const beforeData = new Map(h.data);
    h.render().handleAutoArrange();
    assert.deepEqual(h.state.nodes.map((node) => node.position), [...createKnowledgeMapPositions(h.state.graph, { compact }).values()]);
    assert.equal(h.state.layoutUndo.graph, h.state.graph);
    assert.equal(h.context.layoutBusyRef.current, true);
    assert.deepEqual(h.writes, [], "programmatic camera move does not re-save the viewport");
    h.render().ensureSelectedNodeVisible();
    h.render().handleNodesChange([{ type: "position", id: "lesson:1", position: { x: 1, y: 2 } }, { type: "select", id: "lesson:1", selected: true }]);
    assert.deepEqual(h.writes.at(-1), ["select"], "camera transaction rejects stray drags while keeping selection events");
    await h.settle();
    const oldUndoCallback = h.render().handleUndoArrange;
    oldUndoCallback();
    assert.deepEqual(plain(h.state.nodes), beforeNodes);
    assert.deepEqual(plain(h.state.viewport), beforeViewport);
    assert.equal(h.state.layoutUndo, null);
    assert.equal(h.state.selectedNodeId, "lesson:1");
    assert.equal(h.state.detailsOpen, true);
    assert.equal(h.state.url, "?focus_type=course&focus_id=1&map_q=SQL");
    await h.settle();
    const afterUndo = new Map(h.data);
    oldUndoCallback();
    assert.deepEqual(h.data, afterUndo, "an old callback cannot consume the same undo twice");
    for (const key of [POSITION_KEY, VIEWPORT_KEY]) assert.deepEqual(JSON.parse(h.data.get(key)), JSON.parse(beforeData.get(key)));
  });
}

test("a graph request invalidates prior undo immediately, including old render callbacks", async () => {
  const h = harness();
  h.render().handleAutoArrange(); await h.settle();
  const old = h.render();
  const beforeData = new Map(h.data);
  const pending = old.loadFocus({ id: "course:2", type: "course", entity_id: "2" });
  assert.equal(h.state.layoutUndo, null);
  assert.equal(h.context.layoutUndoRef.current, null);
  old.handleUndoArrange(); old.handleAutoArrange();
  assert.deepEqual(h.data, beforeData);
  const next = { ...h.state.graph, focus: { id: "course:2", type: "course", entity_id: "2" } };
  h.requests[0](next); await pending;
  old.handleUndoArrange(); old.handleAutoArrange();
  assert.deepEqual(h.data, beforeData);
});

test("a changed route focus cannot arrange or undo the old graph", async () => {
  const h = harness();
  h.render().handleAutoArrange(); await h.settle();
  const before = new Map(h.data);
  h.context.initialFocusKey = "course:other";
  h.render().handleUndoArrange(); h.render().handleAutoArrange();
  assert.deepEqual(h.data, before);
});

test("failed persistence leaves the rendered graph and undo unchanged, then explicit retry succeeds", async () => {
  const h = harness();
  const beforeNodes = plain(h.state.nodes), beforeViewport = plain(h.state.viewport), beforeData = new Map(h.data);
  const write = h.storage.setItem;
  h.storage.setItem = () => { throw new Error("quota"); };
  h.render().handleAutoArrange();
  assert.deepEqual(plain(h.state.nodes), beforeNodes);
  assert.deepEqual(plain(h.state.viewport), beforeViewport);
  assert.deepEqual(h.data, beforeData);
  assert.equal(h.state.layoutUndo, null);
  assert.equal(h.state.layoutSaveError, "save");
  h.storage.setItem = write;
  h.render().retryLayoutCommand(); await h.settle();
  assert.equal(h.state.layoutSaveError, null);
  assert.ok(h.state.layoutUndo);
  assert.notDeepEqual(plain(h.state.nodes), beforeNodes);
});

test("rollback failure keeps recovery data, prevents misleading UI success, and supports an explicit retry", async () => {
  const h = harness();
  const beforeNodes = plain(h.state.nodes), beforeViewport = plain(h.state.viewport);
  const write = h.storage.setItem;
  let count = 0;
  h.storage.setItem = (key, value) => { if (++count > 1) throw new Error("blocked"); write(key, value); };
  h.render().handleAutoArrange();
  assert.equal(h.state.layoutSaveError, "recovery");
  assert.equal(h.context.layoutRecoveryRef.current.length, 1);
  assert.deepEqual(plain(h.state.nodes), beforeNodes);
  assert.deepEqual(plain(h.state.viewport), beforeViewport);
  assert.equal(h.state.layoutUndo, null);
  h.storage.setItem = write;
  h.render().retryLayoutCommand(); await h.settle();
  assert.equal(h.context.layoutRecoveryRef.current.length, 0);
  assert.equal(h.state.layoutSaveError, null);
  assert.ok(h.state.layoutUndo);
});

test("failed undo keeps its snapshot and arranged screen until an explicit retry saves the restoration", async () => {
  const h = harness();
  const originalNodes = plain(h.state.nodes), originalViewport = plain(h.state.viewport);
  h.render().handleAutoArrange(); await h.settle();
  const undo = h.state.layoutUndo;
  const arrangedNodes = plain(h.state.nodes), arrangedViewport = plain(h.state.viewport), arrangedData = new Map(h.data);
  const write = h.storage.setItem;
  h.storage.setItem = () => { throw new Error("blocked"); };
  h.render().handleUndoArrange();
  assert.equal(h.state.layoutUndo, undo);
  assert.equal(h.context.layoutUndoRef.current, undo);
  assert.equal(h.state.layoutSaveError, "save");
  assert.deepEqual(plain(h.state.nodes), arrangedNodes);
  assert.deepEqual(plain(h.state.viewport), arrangedViewport);
  assert.deepEqual(h.data, arrangedData);
  h.storage.setItem = write;
  h.render().retryLayoutCommand(); await h.settle();
  assert.equal(h.state.layoutUndo, null);
  assert.deepEqual(plain(h.state.nodes), originalNodes);
  assert.deepEqual(plain(h.state.viewport), originalViewport);
});

test("recovery after graph navigation restores storage without replaying the old graph command", async () => {
  const h = harness();
  const originalNodes = plain(h.state.nodes), originalViewport = plain(h.state.viewport), originalData = new Map(h.data);
  const write = h.storage.setItem;
  let count = 0;
  h.storage.setItem = (key, value) => { if (++count > 1) throw new Error("blocked"); write(key, value); };
  h.render().handleAutoArrange();
  assert.equal(h.state.layoutSaveError, "recovery");
  const pending = h.render().loadFocus({ id: "course:2", type: "course", entity_id: "2" });
  h.storage.setItem = write;
  h.render().retryLayoutCommand();
  assert.equal(h.context.layoutRecoveryRef.current.length, 0);
  assert.equal(h.state.layoutUndo, null);
  assert.deepEqual(h.data, originalData);
  assert.deepEqual(plain(h.state.nodes), originalNodes);
  assert.deepEqual(plain(h.state.viewport), originalViewport);
  h.requests[0]({ ...h.state.graph, focus: { id: "course:2", type: "course", entity_id: "2" } });
  await pending;
});

test("unsaved default positions remain unsaved through arrange and undo", async () => {
  const h = harness({ saved: false });
  const before = JSON.parse(h.data.get(POSITION_KEY));
  h.render().handleAutoArrange(); await h.settle();
  assert.deepEqual(JSON.parse(h.data.get(POSITION_KEY)), before);
  h.render().handleUndoArrange(); await h.settle();
  assert.deepEqual(JSON.parse(h.data.get(POSITION_KEY)), before);
});

function effectDeclaration(tree, marker) {
  const matches = [];
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === "useEffect"
      && node.arguments[0]?.getText(tree).includes(marker)) matches.push(node.arguments[0].getText(tree));
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.equal(matches.length, 1, marker); return matches[0];
}

function responsiveHarness({ compact = true, saved = false, tree = ast } = {}) {
  const h = harness({ compact, saved }), observers = [], fits = [], ensureCalls = [];
  const context = h.context;
  context.detailsOpen = h.state.detailsOpen;
  context.canvasRef = { current: {} }; context.selectionPanelRef = { current: null }; context.resizeFrameRef = { current: null };
  context.restoredViewportKeyRef.current = null; context.viewportMemoryRef.current.clear();
  context.ensureSelectedNodeVisible = () => ensureCalls.push(true);
  context.safeCanvasBounds = () => ({ left: 0, top: 0, right: context.window.innerWidth - 48, bottom: 620 });
  context.flowInstanceRef.current.getNode = id => h.state.nodes.find(node => node.id === id);
  context.ResizeObserver = class {
    constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this); }
    observe(target) { assert.equal(target, context.canvasRef.current); }
    disconnect() { this.disconnected = true; }
  };
  const functions = [];
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && ["createPositions", "resolveCanvasPositions"].includes(node.name?.getText(ast))) functions.push(node.getText(ast));
    ts.forEachChild(node, visit);
  }
  visit(ast); assert.equal(functions.length, 2);
  context.createKnowledgeMapPositions = createKnowledgeMapPositions;
  const program = `${functions.join("\n")}
    globalThis.resolveCanvasPositions = resolveCanvasPositions;
    globalThis.actualFit = ${declaration("fitAllNodesInSafeArea")};
    globalThis.bindResponsiveEffects = (graph, graphNodesMeasured, flowReady) => ({
      restore: ${effectDeclaration(tree, "const key = viewportMemoryKey(graph)")},
      resize: ${effectDeclaration(tree, "new ResizeObserver")}
    });`;
  vm.runInNewContext(ts.transpileModule(program, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText, context, { timeout: 1000 });
  context.fitAllNodesInSafeArea = (...args) => { fits.push(plain(h.state.nodes)); return context.actualFit(...args); };
  const initial = context.resolveCanvasPositions(h.state.graph, context.positionMemoryRef.current);
  h.state.nodes = h.state.nodes.map(node => ({ ...node, position: initial.get(node.id) }));
  return {
    ...h, fits, observers, ensureCalls,
    effects: (measured = true, ready = true) => context.bindResponsiveEffects(h.state.graph, measured, ready),
    width(value) { context.window.innerWidth = value; h.state.nodes = h.state.nodes.map(node => ({ ...node, measured: { width: value < 1024 ? 148 : 224, height: 116 } })); },
    notify() { const observer = observers.at(-1); assert(!observer.disconnected); observer.callback(); },
    frame() { const pending = [...h.frames]; h.frames.clear(); for (const [, callback] of pending) callback(); },
  };
}

function assertDefaultPositions(h) {
  const positions = createKnowledgeMapPositions(h.state.graph, { compact: h.context.window.innerWidth < 1024 });
  for (const node of h.state.nodes) {
    const expected = h.context.positionMemoryRef.current.get(positionMemoryKey(h.state.graph, node.id)) ?? positions.get(node.id);
    assert.deepEqual(plain(node.position), expected);
  }
}

function cardsOverlap(nodes) {
  return nodes.some((a, index) => nodes.slice(index + 1).some(b => Math.abs(a.position.x - b.position.x) < (a.measured.width + b.measured.width) / 2
    && Math.abs(a.position.y - b.position.y) < (a.measured.height + b.measured.height) / 2));
}

test("the original restore guard loses one early resize; current effects synchronize before restore without a second notification", () => {
  const normalizedSource = source.replaceAll("\r\n", "\n"), oldGuard = "if (!graph || restoredViewportKeyRef.current !== viewportMemoryKey(graph)) return;";
  for (const marker of ["if (!graph) return;\n        const compact", "        if (restoredViewportKeyRef.current !== viewportMemoryKey(graph)) return;\n        // Card measurements"]) assert.equal(normalizedSource.split(marker).length, 2, "each responsive guard has exactly one inverse target");
  const before = normalizedSource.replace("if (!graph) return;\n        const compact", oldGuard + "\n        const compact")
    .replace("        if (restoredViewportKeyRef.current !== viewportMemoryKey(graph)) return;\n        // Card measurements", "        // Card measurements");
  const oldTree = ts.createSourceFile("KnowledgeGraphMap.tsx", before, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  for (const original of [true, false]) {
    const h = responsiveHarness({ tree: original ? oldTree : ast }), priorData = new Map(h.data), cleanup = h.effects(false, false).resize();
    h.width(1264); h.notify(); h.frame(); assert.equal(h.fits.length, 0, "camera waits for viewport restoration");
    h.effects().restore(); h.frame(); assert.equal(h.context.restoredViewportKeyRef.current, viewportMemoryKey(h.state.graph));
    assert.equal(h.fits.length, 1); assert.equal(h.frames.size, 0, "there is no second observer notification");
    if (original) { assert.throws(() => assertDefaultPositions(h)); assert.equal(cardsOverlap(h.state.nodes), true); }
    else { assertDefaultPositions(h); assert.equal(cardsOverlap(h.state.nodes), false); }
    assert.deepEqual(h.data, priorData, "responsive correction does not save coordinates or a camera"); cleanup();
  }
});

for (const compact of [true, false]) {
  test(`${compact ? "compact to desktop" : "desktop to compact"} early and restored resize retain manual positions, selection and storage`, () => {
    const h = responsiveHarness({ compact }), manual = h.state.nodes[0], remembered = { x: -440, y: 870 };
    h.context.positionMemoryRef.current.set(positionMemoryKey(h.state.graph, manual.id), remembered);
    const stored = JSON.parse(h.data.get(POSITION_KEY)); stored.entries[positionMemoryKey(h.state.graph, manual.id)] = remembered; h.data.set(POSITION_KEY, JSON.stringify(stored));
    h.state.nodes[0] = { ...manual, position: remembered }; const priorData = new Map(h.data), selected = h.state.nodes.map(node => node.selected), edgeSelected = h.state.edges.map(edge => edge.selected);
    const cleanup = h.effects(false, false).resize(); h.width(compact ? 1264 : 632); h.notify(); h.frame(); assertDefaultPositions(h);
    h.effects().restore(); h.frame(); assertDefaultPositions(h);
    h.width(compact ? 632 : 1264); h.notify(); h.frame(); h.frame(); assertDefaultPositions(h);
    assert.deepEqual(plain(h.state.nodes[0].position), remembered); assert.deepEqual(h.state.nodes.map(node => node.selected), selected); assert.deepEqual(h.state.edges.map(edge => edge.selected), edgeSelected);
    assert.deepEqual(h.data, priorData); assert.equal(h.writes.length, 0); cleanup();
  });
}

test("resize cleanup cancels its pending frame without touching nodes, selection or persisted state", () => {
  const h = responsiveHarness(), cleanup = h.effects().resize(), before = plain(h.state.nodes), data = new Map(h.data);
  h.width(1264); const afterWidth = plain(h.state.nodes); h.notify(); assert.equal(h.frames.size, 1); cleanup(); h.frame();
  assert.equal(h.observers[0].disconnected, true); assert.equal(h.frames.size, 0); assert.deepEqual(plain(h.state.nodes), afterWidth); assert.deepEqual(h.state.nodes.map(node => node.position), before.map(node => node.position)); assert.deepEqual(h.data, data); assert.equal(h.fits.length, 0);
});
