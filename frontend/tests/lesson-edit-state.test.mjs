import assert from "node:assert/strict";
import test from "node:test";

import { createLessonEditSnapshot, isLessonEditDirty } from "../src/lib/lesson-edit-state.mjs";

const draft = {
  title: "Lesson",
  content: "<p>Body</p>",
  minutes: 10,
  tagIds: [3, 1],
};

test("lesson dirty state covers title, body, duration, and tags", () => {
  const snapshot = createLessonEditSnapshot(draft);
  assert.equal(isLessonEditDirty(snapshot, { ...draft, tagIds: [1, 3] }), false);
  assert.equal(isLessonEditDirty(snapshot, { ...draft, title: "Changed" }), true);
  assert.equal(isLessonEditDirty(snapshot, { ...draft, content: "<p>Changed</p>" }), true);
  assert.equal(isLessonEditDirty(snapshot, { ...draft, minutes: 11 }), true);
  assert.equal(isLessonEditDirty(snapshot, { ...draft, tagIds: [1, 4] }), true);
});
