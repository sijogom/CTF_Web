import test from "node:test";
import assert from "node:assert/strict";
import { mappingSnapshot, mappingSavePayload, patchMapping, selectMappingTags } from "../src/lib/knowledge-tag-mapping-state.mjs";

const manual = { tag_id: 1, origin: "manual", direction: "content_to_knowledge_tag", strength: 0.4,
  confidence: 0.8, evidence_summary: "검증 근거" };
const rule = { ...manual, tag_id: 2, origin: "rule" };
const candidate = { ...manual, tag_id: 3, origin: "model_candidate", approved_at: null };

test("failed or unfinished mapping loads never create a replacement payload", () => {
  for (const status of ["error", "loading"]) {
    assert.equal(mappingSavePayload({ status, revision: "a".repeat(64), mappings: [] }), null);
  }
  assert.equal(mappingSavePayload({ status: "ready", revision: null, mappings: [] }), null);
});

test("a successful empty mapping load can explicitly clear editable mappings with its revision", () => {
  assert.deepEqual(mappingSavePayload({ status: "ready", revision: "a".repeat(64), mappings: [] }),
    { expected_revision: "a".repeat(64), mappings: [] });
});

test("quality-only changes mark the mapping draft dirty", () => {
  assert.notEqual(mappingSnapshot([manual]), mappingSnapshot(patchMapping([manual], 1, { strength: 0.7 })));
  assert.notEqual(mappingSnapshot([manual]), mappingSnapshot(patchMapping([manual], 1, { evidence_summary: "다른 근거" })));
});

test("selection preserves rule and candidate mappings and existing manual quality", () => {
  assert.deepEqual(selectMappingTags([manual, rule, candidate], [1]), [rule, candidate, manual]);
  assert.deepEqual(selectMappingTags([manual, rule, candidate], []), [rule, candidate]);
});

test("quality edits cannot change automatic mappings or mapping identity", () => {
  assert.deepEqual(patchMapping([rule, candidate], 2, { strength: 0.1 }), [rule, candidate]);
  assert.deepEqual(patchMapping([rule, candidate], 3, { strength: 0.1 }), [rule, candidate]);
  assert.equal(patchMapping([manual], 1, { tag_id: 8, origin: "rule" })[0].tag_id, 1);
  assert.equal(patchMapping([manual], 1, { tag_id: 8, origin: "rule" })[0].origin, "manual");
});

test("mapping order and duplicate selection do not create false dirty changes", () => {
  assert.equal(mappingSnapshot([manual, rule]), mappingSnapshot(selectMappingTags([manual, rule], [1, 1, 2])));
});

test("save payload carries quality and optimistic revision without rewriting provenance", () => {
  const payload = mappingSavePayload({ status: "ready", revision: "b".repeat(64), mappings: [candidate] });
  assert.equal(payload.expected_revision, "b".repeat(64));
  assert.deepEqual(payload.mappings[0], { tag_id: 3, direction: manual.direction,
    strength: 0.4, confidence: 0.8, evidence_summary: "검증 근거" });
  assert.equal("approved_at" in payload.mappings[0], false);
});
