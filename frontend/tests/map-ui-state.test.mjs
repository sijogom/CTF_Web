import assert from "node:assert/strict";
import test from "node:test";
import { readMapToolsState, mergeMapPage, internalContentHref } from "../src/lib/map-ui-state.mjs";

function page(edges, nodes, overrides = {}) {
  return { graph_revision: "r1", focus: { id: "course:1" }, applied_filters: { depth: 1, types: ["course", "lesson"], relation_types: ["contains"] }, nodes: nodes.map((id) => ({ id })), edges, next_cursor: null, ...overrides };
}

test("map tools preserve supported filters and reject unknown or empty selections", () => {
  assert.deepEqual(readMapToolsState("map_q=SQL&map_types=lesson,invalid,lesson&map_relations=similar"), { query: "SQL", types: ["lesson"], relations: ["similar"] });
  assert.equal(readMapToolsState("map_types=").types.length, 4);
  assert.equal(readMapToolsState("map_q=" + "x".repeat(150)).query.length, 100);
});
test("map pages merge shared focus without losing edge endpoints", () => {
  const first = page([{ id: "a", source: "course:1", target: "lesson:1" }], ["course:1", "lesson:1"]);
  const next = page([{ id: "b", source: "course:1", target: "lesson:2" }], ["course:1", "lesson:2"], { next_cursor: "next" });
  const merged = mergeMapPage(first, next);
  assert.equal(merged.nodes.length, 3);
  assert.equal(merged.edges.length, 2);
  assert.equal(merged.next_cursor, "next");
});
test("map pages cannot join different revisions, centers, filters or orphan edges", () => {
  const initial = page([], ["course:1"]);
  for (const override of [{ graph_revision: "r2" }, { focus: { id: "course:2" } }, { applied_filters: { ...initial.applied_filters, depth: 2 } }]) {
    assert.throws(() => mergeMapPage(initial, page([], ["course:1"], override)), /mismatch/);
  }
  assert.throws(() => mergeMapPage(initial, page([{ id: "bad", source: "course:1", target: "missing" }], ["course:1"])), /endpoint/);
});
test("content links stay on internal paths", () => {
  assert.equal(internalContentHref("/learn/1"), "/learn/1");
  for (const href of ["//example.com", "/\\example.com", "javascript:alert(1)", "/\nexample.com"]) assert.equal(internalContentHref(href), null);
});
