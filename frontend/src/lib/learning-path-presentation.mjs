import { internalContentHref } from "./map-ui-state.mjs";

export function learningPathAction(projection) {
  const action = projection.next_action;
  const href = internalContentHref(action?.href);
  if (!href || href === internalContentHref(projection.target.href)) return null;
  if (action.kind === "sign_in") return { kind: "sign_in", href };
  if (action.kind === "contact_admin") return { kind: "contact_admin", href };
  if (action.kind !== "open_content") return null;
  const step = projection.required_steps.find((item) => item.status !== "completed"
    && item.status !== "unavailable" && internalContentHref(item.href) === href);
  return step ? { kind: "open_content", href, name: step.name } : null;
}

export function hasLearningPathContent(projection) {
  return projection.required_steps.length > 0
    || projection.recommended_steps.length > 0
    || projection.helpful_content.length > 0
    || learningPathAction(projection) !== null;
}
