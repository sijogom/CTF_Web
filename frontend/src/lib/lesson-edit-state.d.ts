export interface LessonEditDraft {
  title: string;
  content: string;
  minutes: number;
  tagIds: number[];
}

export function createLessonEditSnapshot(draft: LessonEditDraft): string;
export function isLessonEditDirty(initialSnapshot: string | null, draft: LessonEditDraft): boolean;
