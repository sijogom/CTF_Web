export function createLessonEditSnapshot({ title, content, minutes, tagIds }) {
  return JSON.stringify({
    title,
    content,
    minutes,
    tagIds: [...new Set(tagIds)].sort((left, right) => left - right),
  });
}

export function isLessonEditDirty(initialSnapshot, draft) {
  if (!initialSnapshot) return false;
  return initialSnapshot !== createLessonEditSnapshot(draft);
}
