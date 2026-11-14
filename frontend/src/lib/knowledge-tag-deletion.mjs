const IMPACT_KEYS = ["challenge", "course", "lesson", "children", "relations", "proposals", "audit_events"];

export function readTagDeletionPreview(value, expectedId) {
  if (!value || value.tag?.id !== expectedId || typeof value.tag.name !== "string" || !value.tag.name.trim()
    || typeof value.confirmation !== "string" || !/^[a-f0-9]{64}$/.test(value.confirmation)
    || !IMPACT_KEYS.every((key) => Number.isSafeInteger(value.counts?.[key]) && value.counts[key] >= 0)) {
    throw new Error("Invalid tag deletion preview");
  }
  return {
    tag: { id: value.tag.id, name: value.tag.name },
    counts: Object.fromEntries(IMPACT_KEYS.map((key) => [key, value.counts[key]])),
    confirmation: value.confirmation,
  };
}
