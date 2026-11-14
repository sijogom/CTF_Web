export interface TagDeletionPreview {
  tag: { id: number; name: string };
  counts: Record<"challenge" | "course" | "lesson" | "children" | "relations" | "proposals" | "audit_events", number>;
  confirmation: string;
}

export function readTagDeletionPreview(value: unknown, expectedId: number): TagDeletionPreview;
