// The backend baseline is verified before the extension UI is activated.
export const KNOWLEDGE_RELEASE_STAGE = "extended";

export function knowledgeExtensionsEnabled(stage = KNOWLEDGE_RELEASE_STAGE) {
  return stage === "extended";
}
