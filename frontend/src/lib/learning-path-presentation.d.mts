import type { LearningPathProjection } from "@/types/knowledge";

export function learningPathAction(projection: LearningPathProjection):
  | { kind: "sign_in"; href: string }
  | { kind: "contact_admin"; href: string }
  | { kind: "open_content"; href: string; name: string }
  | null;
export function hasLearningPathContent(projection: LearningPathProjection): boolean;
