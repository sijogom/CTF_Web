import assert from "node:assert/strict";
import test from "node:test";
import { createKnowledgeMapPositions, fitKnowledgeMapViewport } from "../src/lib/knowledge-map-layout.mjs";

function graph(count, overrides = {}) {
  const nodes = Array.from({ length: count }, (_, index) => ({
    id: index === 0 ? "course:focus" : `lesson:${String(index).padStart(3, "0")}`,
    type: index === 0 ? "course" : "lesson",
    distance: index === 0 ? 0 : 1,
  }));
  return { focus: nodes[0], nodes, layout_hints: { distance_by_node: {} }, ...overrides };
}

function flowNodes(map, compact = false) {
  return [...map].map(([id, position]) => ({ id, position, measured: { width: compact ? 148 : 224, height: 116 } }));
}

function assertSeparated(nodes) {
  for (let left = 0; left < nodes.length; left += 1) {
    for (let right = left + 1; right < nodes.length; right += 1) {
      const a = nodes[left];
      const b = nodes[right];
      assert.ok(Math.abs(a.position.x - b.position.x) >= (a.measured.width + b.measured.width) / 2 + 16
        || Math.abs(a.position.y - b.position.y) >= (a.measured.height + b.measured.height) / 2 + 16,
      `${a.id} overlaps ${b.id}`);
    }
  }
}

for (const compact of [false, true]) {
  for (const count of [1, 2, 6, 8, 25, 120]) {
    test(`${compact ? "compact" : "desktop"} ${count}-node layout keeps every card separated`, () => {
      const positions = createKnowledgeMapPositions(graph(count), { compact });
      assert.equal(positions.size, count);
      assertSeparated(flowNodes(positions, compact));
    });
  }
}

test("layout is independent of API node order and retains focus", () => {
  const input = graph(25);
  const initial = createKnowledgeMapPositions(input);
  const reversed = createKnowledgeMapPositions({ ...input, nodes: [...input.nodes].reverse() });
  assert.deepEqual(initial, reversed);
  assert.deepEqual(initial.get(input.focus.id), { x: 620, y: 410 });
});

test("farther nodes occupy outer rings without replacing direct neighbors", () => {
  const input = graph(25);
  input.nodes.slice(9).forEach((node) => { input.layout_hints.distance_by_node[node.id] = 2; });
  const positions = createKnowledgeMapPositions(input);
  for (const node of input.nodes.slice(1, 9)) {
    const point = positions.get(node.id);
    assert.ok(Math.abs(point.x - 620) <= 296 && Math.abs(point.y - 410) <= 168);
  }
  assertSeparated(flowNodes(positions));
});

for (const width of [360, 390, 768, 1280, 1920]) {
  test(`initial six-node fit at ${width}px shows complete readable cards`, () => {
    const compact = width < 1024;
    const canvasWidth = width < 640 ? width - 42 : width - 96;
    const bounds = { left: compact ? 12 : 72, right: canvasWidth - (compact ? 12 : 24), top: compact ? 72 : 24, bottom: 436 };
    const nodes = flowNodes(createKnowledgeMapPositions(graph(6), { compact }), compact);
    const viewport = fitKnowledgeMapViewport(nodes, bounds, { compact });
    assert.ok(viewport.zoom >= (compact ? 0.86 : 0.9), "representative map must not shrink titles to a few pixels");
    for (const node of nodes) {
      const left = viewport.x + (node.position.x - node.measured.width / 2) * viewport.zoom;
      const right = viewport.x + (node.position.x + node.measured.width / 2) * viewport.zoom;
      const top = viewport.y + (node.position.y - node.measured.height / 2) * viewport.zoom;
      const bottom = viewport.y + (node.position.y + node.measured.height / 2) * viewport.zoom;
      assert.ok(left >= bounds.left - 0.001 && right <= bounds.right + 0.001 && top >= bounds.top - 0.001 && bottom <= bounds.bottom + 0.001, node.id);
    }
  });
}

test("fit uses measured card dimensions and bounded zoom without mutating drag coordinates", () => {
  const nodes = [{ position: { x: -120, y: 80 }, measured: { width: 400, height: 200 } }];
  const before = structuredClone(nodes);
  const viewport = fitKnowledgeMapViewport(nodes, { left: 10, right: 210, top: 10, bottom: 210 });
  assert.deepEqual(viewport, { x: 170, y: 70, zoom: 0.5 });
  assert.deepEqual(nodes, before);
  assert.equal(fitKnowledgeMapViewport(nodes, { left: 10, right: 10, top: 0, bottom: 100 }), null);
  assert.equal(fitKnowledgeMapViewport([], { left: 0, right: 100, top: 0, bottom: 100 }), null);
  assert.equal(fitKnowledgeMapViewport([{ position: { x: NaN, y: 0 } }], { left: 0, right: 100, top: 0, bottom: 100 }), null);
});

test("120-node phone overview retains every boundary below the old minimum zoom", () => {
  const nodes = flowNodes(createKnowledgeMapPositions(graph(120), { compact: true }), true);
  const bounds = { left: 12, right: 306, top: 72, bottom: 436 };
  const viewport = fitKnowledgeMapViewport(nodes, bounds, { compact: true });
  assert.ok(viewport.zoom < 0.2 && viewport.zoom >= 0.05);
  for (const node of nodes) {
    assert.ok(viewport.x + (node.position.x - 74) * viewport.zoom >= bounds.left - 0.001);
    assert.ok(viewport.x + (node.position.x + 74) * viewport.zoom <= bounds.right + 0.001);
    assert.ok(viewport.y + (node.position.y - 58) * viewport.zoom >= bounds.top - 0.001);
    assert.ok(viewport.y + (node.position.y + 58) * viewport.zoom <= bounds.bottom + 0.001);
  }
});
