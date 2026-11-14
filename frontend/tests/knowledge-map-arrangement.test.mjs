import assert from "node:assert/strict";
import test from "node:test";
import {
  KNOWLEDGE_MAP_POSITION_STORAGE_KEY as POSITION_KEY,
  KNOWLEDGE_MAP_POSITION_LAYOUT_VERSION as POSITION_VERSION,
  KNOWLEDGE_MAP_VIEWPORT_STORAGE_KEY as VIEWPORT_KEY,
  KNOWLEDGE_MAP_VIEWPORT_LAYOUT_VERSION as VIEWPORT_VERSION,
  knowledgeMapPositionKey,
  knowledgeMapViewportKey,
  persistKnowledgeMapLayout,
  recoverKnowledgeMapLayout,
} from "../src/lib/knowledge-map-arrangement.mjs";

const graph = { graph_revision: "r1", focus: { id: "course:1" }, nodes: [{ id: "course:1" }, { id: "lesson:1" }] };
const oldViewport = { x: -1220, y: 83, zoom: 0.35 };
const newViewport = { x: -38, y: -72, zoom: 0.9 };
const positionKey = (nodeId) => knowledgeMapPositionKey(graph, nodeId);
const viewportKey = knowledgeMapViewportKey(graph);
const oldPositions = new Map([["course:1", { x: 1800, y: -60 }], ["lesson:1", { x: 2900, y: 410 }]]);
const compactPositions = new Map([["course:1", { x: 620, y: 410 }], ["lesson:1", { x: 800, y: 410 }]]);

function storageFixture() {
  const data = new Map([
    [POSITION_KEY, JSON.stringify({ version: POSITION_VERSION, metadata: "preserved", entries: {
      [positionKey("course:1")]: oldPositions.get("course:1"),
      [positionKey("lesson:1")]: oldPositions.get("lesson:1"),
      "r1:course:2:lesson:1": { x: 8800, y: 9900 },
      "r1:course:1:lesson:outside-current-page": { x: 7300, y: 1200 },
    } })],
    [VIEWPORT_KEY, JSON.stringify({ version: VIEWPORT_VERSION, entries: {
      [viewportKey]: oldViewport,
      "r1:course:2": { x: -99, y: 19, zoom: 1.6 },
    } })],
    ["unrelated:key", "do not touch"],
  ]);
  const calls = [];
  return {
    data,
    calls,
    getItem(key) { return data.get(key) ?? null; },
    setItem(key, raw) { calls.push(["set", key]); data.set(key, raw); },
    removeItem(key) { calls.push(["remove", key]); data.delete(key); },
  };
}

test("explicit layout replaces only current node entries and the current camera, with no global truncation", () => {
  const storage = storageFixture();
  const before = new Map(storage.data);
  const positionEnvelope = JSON.parse(storage.getItem(POSITION_KEY));
  const viewportEnvelope = JSON.parse(storage.getItem(VIEWPORT_KEY));
  for (let index = 0; index < 400; index += 1) positionEnvelope.entries[`other:${index}`] = { x: index, y: index };
  for (let index = 0; index < 80; index += 1) viewportEnvelope.entries[`other:${index}`] = { x: index, y: index, zoom: 1 };
  storage.data.set(POSITION_KEY, JSON.stringify(positionEnvelope));
  storage.data.set(VIEWPORT_KEY, JSON.stringify(viewportEnvelope));
  const result = persistKnowledgeMapLayout(storage, graph, compactPositions, newViewport);
  assert.equal(result.ok, true);
  const nextPositions = JSON.parse(storage.getItem(POSITION_KEY));
  const nextViewports = JSON.parse(storage.getItem(VIEWPORT_KEY));
  assert.equal(Object.keys(nextPositions.entries).length, Object.keys(positionEnvelope.entries).length);
  assert.equal(Object.keys(nextViewports.entries).length, Object.keys(viewportEnvelope.entries).length);
  assert.equal(nextPositions.metadata, "preserved");
  for (const [key, value] of Object.entries(positionEnvelope.entries)) {
    if (![positionKey("course:1"), positionKey("lesson:1")].includes(key)) assert.deepEqual(nextPositions.entries[key], value);
  }
  for (const [key, value] of Object.entries(viewportEnvelope.entries)) {
    if (key !== viewportKey) assert.deepEqual(nextViewports.entries[key], value);
  }
  assert.deepEqual(nextPositions.entries[positionKey("course:1")], compactPositions.get("course:1"));
  assert.deepEqual(nextViewports.entries[viewportKey], newViewport);
  assert.equal(storage.getItem("unrelated:key"), before.get("unrelated:key"));
  assert.ok(storage.calls.every(([, key]) => [POSITION_KEY, VIEWPORT_KEY].includes(key)));
});

test("undo restores exact previous mixed coordinates and camera for a reload", () => {
  const storage = storageFixture();
  const beforePositions = JSON.parse(storage.getItem(POSITION_KEY));
  const beforeViewports = JSON.parse(storage.getItem(VIEWPORT_KEY));
  assert.equal(persistKnowledgeMapLayout(storage, graph, compactPositions, newViewport).ok, true);
  const result = persistKnowledgeMapLayout(storage, graph, oldPositions, oldViewport);
  assert.equal(result.ok, true);
  assert.deepEqual(JSON.parse(storage.getItem(POSITION_KEY)), beforePositions);
  assert.deepEqual(JSON.parse(storage.getItem(VIEWPORT_KEY)), beforeViewports);
});

test("full existing capacity rejects additions without evicting another graph", () => {
  for (const key of [POSITION_KEY, VIEWPORT_KEY]) {
    const storage = storageFixture();
    const envelope = JSON.parse(storage.getItem(key));
    const limit = key === POSITION_KEY ? 360 : 60;
    if (key === POSITION_KEY) delete envelope.entries[positionKey("lesson:1")];
    else delete envelope.entries[viewportKey];
    for (let index = Object.keys(envelope.entries).length; index < limit; index += 1) {
      envelope.entries[`other:${index}`] = key === POSITION_KEY ? { x: index, y: index } : { x: index, y: index, zoom: 1 };
    }
    storage.data.set(key, JSON.stringify(envelope));
    const before = new Map(storage.data);
    const result = persistKnowledgeMapLayout(storage, graph, compactPositions, newViewport);
    assert.equal(result.ok, false);
    assert.deepEqual(result.recovery, []);
    assert.deepEqual(storage.data, before);
    assert.deepEqual(storage.calls, []);
  }
});

test("undo can restore absent saved entries without deleting other graph entries", () => {
  const storage = storageFixture();
  assert.equal(persistKnowledgeMapLayout(storage, graph, new Map([["lesson:1", null]]), oldViewport).ok, true);
  const entries = JSON.parse(storage.getItem(POSITION_KEY)).entries;
  assert.equal(Object.hasOwn(entries, positionKey("lesson:1")), false);
  assert.deepEqual(entries["r1:course:2:lesson:1"], { x: 8800, y: 9900 });
  assert.deepEqual(entries[positionKey("course:1")], oldPositions.get("course:1"));
});

test("read errors, malformed or unsupported storage, and invalid changes never mutate storage", () => {
  const invalid = ["{", "null", "[]", "{}", JSON.stringify({ version: "future", entries: {} }), JSON.stringify({ version: POSITION_VERSION, entries: [] })];
  for (const raw of invalid) {
    const storage = storageFixture();
    storage.data.set(POSITION_KEY, raw);
    const before = new Map(storage.data);
    const result = persistKnowledgeMapLayout(storage, graph, compactPositions, newViewport);
    assert.equal(result.ok, false);
    assert.deepEqual(result.recovery, []);
    assert.deepEqual(storage.data, before);
    assert.deepEqual(storage.calls, []);
  }
  for (const changes of [new Map([["other:1", { x: 1, y: 1 }]]), new Map([["lesson:1", { x: NaN, y: 1 }]])]) {
    const storage = storageFixture();
    assert.equal(persistKnowledgeMapLayout(storage, graph, changes, newViewport).ok, false);
    assert.deepEqual(storage.calls, []);
  }
  const storage = storageFixture();
  storage.getItem = () => { throw new Error("blocked"); };
  assert.equal(persistKnowledgeMapLayout(storage, graph, compactPositions, newViewport).ok, false);
  assert.deepEqual(storage.calls, []);
});

test("blocked first write leaves the previous data intact without reporting success", () => {
  const storage = storageFixture();
  const before = new Map(storage.data);
  storage.setItem = () => { throw new Error("quota"); };
  const result = persistKnowledgeMapLayout(storage, graph, compactPositions, newViewport);
  assert.equal(result.ok, false);
  assert.deepEqual(result.recovery, []);
  assert.deepEqual(storage.data, before);
});

test("second write failure restores the exact first raw value", () => {
  const storage = storageFixture();
  const before = new Map(storage.data);
  const setItem = storage.setItem.bind(storage);
  storage.setItem = (key, value) => {
    if (key === VIEWPORT_KEY) throw new Error("quota");
    setItem(key, value);
  };
  const result = persistKnowledgeMapLayout(storage, graph, compactPositions, newViewport);
  assert.equal(result.ok, false);
  assert.deepEqual(result.recovery, []);
  assert.deepEqual(storage.data, before);
});

test("rollback failure retains the original raw for explicit recovery and never reports success", () => {
  const storage = storageFixture();
  const before = new Map(storage.data);
  const setItem = storage.setItem.bind(storage);
  let writes = 0;
  storage.setItem = (key, value) => {
    writes += 1;
    if (writes > 1) throw new Error("blocked");
    setItem(key, value);
  };
  const result = persistKnowledgeMapLayout(storage, graph, compactPositions, newViewport);
  assert.equal(result.ok, false);
  assert.equal(result.recovery.length, 1);
  assert.equal(result.recovery[0].previousRaw, before.get(POSITION_KEY));
  assert.notEqual(storage.getItem(POSITION_KEY), before.get(POSITION_KEY));
  storage.setItem = setItem;
  assert.deepEqual(recoverKnowledgeMapLayout(storage, result.recovery), { ok: true, recovery: [] });
  assert.deepEqual(storage.data, before);
});

test("recovery does not overwrite a later storage write from another graph or tab", () => {
  const storage = storageFixture();
  const recovery = [{ key: POSITION_KEY, previousRaw: "previous", writtenRaw: "written" }];
  const latest = storage.getItem(POSITION_KEY);
  assert.deepEqual(recoverKnowledgeMapLayout(storage, recovery), { ok: false, recovery });
  assert.equal(storage.getItem(POSITION_KEY), latest);
  assert.deepEqual(storage.calls, []);
});

test("failed second write removes only a new task-created first key during rollback", () => {
  const storage = storageFixture();
  storage.data.delete(POSITION_KEY);
  const before = new Map(storage.data);
  const setItem = storage.setItem.bind(storage);
  storage.setItem = (key, raw) => {
    if (key === VIEWPORT_KEY) throw new Error("blocked");
    setItem(key, raw);
  };
  const result = persistKnowledgeMapLayout(storage, graph, compactPositions, newViewport);
  assert.equal(result.ok, false);
  assert.deepEqual(result.recovery, []);
  assert.deepEqual(storage.data, before);
  assert.deepEqual(storage.calls.at(-1), ["remove", POSITION_KEY]);
});
