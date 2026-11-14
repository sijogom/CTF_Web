import assert from "node:assert/strict";
import test from "node:test";

import { collectEducationPages, educationSubmissionAvailability } from "../src/lib/education-state.mjs";

test("education pagination reads the declared page range and de-duplicates overlap", async () => {
  const calls = [];
  const pages = {
    1: { items: [{ id: "a" }, { id: "b" }], total: 5, page: 1, size: 2 },
    2: { items: [{ id: "b" }, { id: "c" }], total: 5, page: 2, size: 2 },
    3: { items: [{ id: "d" }, { id: "e" }], total: 5, page: 3, size: 2 },
  };
  const items = await collectEducationPages(async (page, size) => {
    calls.push([page, size]);
    return pages[page];
  }, 2);
  assert.deepEqual(calls, [[1, 2], [2, 2], [3, 2]]);
  assert.deepEqual(items.map((item) => item.id), ["a", "b", "c", "d", "e"]);
});

test("submission reads remain available when the write window closes", () => {
  const classItem = { status: "published", is_education_manager: false };
  const assignment = {
    status: "published",
    available_from_at: "2026-09-08T01:00:00.000Z",
    due_at: "2026-09-08T02:00:00.000Z",
  };
  assert.equal(educationSubmissionAvailability(classItem, assignment, Date.parse("2026-09-08T00:59:59.999Z")), "notOpen");
  assert.equal(educationSubmissionAvailability(classItem, assignment, Date.parse("2026-09-08T02:00:00.000Z")), "open");
  assert.equal(educationSubmissionAvailability(classItem, assignment, Date.parse("2026-09-08T02:00:00.001Z")), "closed");
  assert.equal(educationSubmissionAvailability({ ...classItem, status: "archived" }, assignment, Date.now()), "classUnavailable");
  assert.equal(educationSubmissionAvailability(classItem, { ...assignment, status: "archived" }, Date.now()), "assignmentUnavailable");
  assert.equal(educationSubmissionAvailability({ ...classItem, is_education_manager: true }, assignment, Date.now()), "manager");
});
