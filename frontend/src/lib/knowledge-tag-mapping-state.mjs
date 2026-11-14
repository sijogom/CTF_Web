export function mappingSnapshot(mappings) {
  return JSON.stringify([...mappings].sort((a, b) => a.tag_id - b.tag_id).map((mapping) => ({
    tag_id: mapping.tag_id,
    direction: mapping.direction,
    strength: mapping.strength,
    confidence: mapping.confidence,
    evidence_summary: mapping.evidence_summary,
    origin: mapping.origin,
  })));
}

export function isEditableMapping(mapping) {
  return mapping.origin === "manual" || mapping.origin === "import";
}

export function selectMappingTags(mappings, tagIds) {
  const locked = mappings.filter((mapping) => !isEditableMapping(mapping));
  const ids = [...new Set([...locked.map((mapping) => mapping.tag_id), ...tagIds])];
  return ids.map((tagId) => mappings.find((mapping) => mapping.tag_id === tagId) ?? {
    tag_id: tagId,
    direction: "content_to_knowledge_tag",
    strength: 1,
    confidence: 1,
    evidence_summary: "수동 지식 태그 매핑",
    origin: "manual",
    created_at: null,
    approved_at: null,
  });
}

export function patchMapping(mappings, tagId, patch) {
  return mappings.map((mapping) => mapping.tag_id === tagId && isEditableMapping(mapping)
    ? { ...mapping, ...patch, tag_id: mapping.tag_id, origin: mapping.origin }
    : mapping);
}

export function mappingSavePayload(draft) {
  if (draft.status !== "ready" || !draft.revision) return null;
  return {
    expected_revision: draft.revision,
    mappings: draft.mappings.map((mapping) => ({
      tag_id: mapping.tag_id,
      direction: mapping.direction,
      strength: mapping.strength,
      confidence: mapping.confidence,
      evidence_summary: mapping.evidence_summary,
    })),
  };
}
