// Enable management only after the compatible Learn image is verified and archived.
export const EVENT_CATEGORY_RELEASE_STAGE = "enabled";

export function eventCategoryManagementEnabled(stage = EVENT_CATEGORY_RELEASE_STAGE) {
  return stage === "enabled";
}
