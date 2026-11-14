/** Collect a stable page range while de-duplicating records by id. */
export async function collectEducationPages(fetchPage, pageSize = 100) {
  const first = await fetchPage(1, pageSize);
  const itemsById = new Map(first.items.map((item) => [item.id, item]));
  const size = Math.max(1, first.size || pageSize);
  let expectedTotal = Math.max(first.total, first.items.length);

  for (let page = first.page + 1; page <= Math.ceil(expectedTotal / size); page += 1) {
    const response = await fetchPage(page, size);
    expectedTotal = Math.max(expectedTotal, response.total);
    for (const item of response.items) itemsById.set(item.id, item);
    if (response.items.length === 0) break;
  }
  return [...itemsById.values()];
}

/** Mirrors the server's read visibility and write-window boundaries. */
export function educationSubmissionAvailability(classItem, assignment, now) {
  if (classItem.is_education_manager) return "manager";
  if (classItem.status !== "published") return "classUnavailable";
  if (assignment.status !== "published") return "assignmentUnavailable";

  const opensAt = assignment.available_from_at ? Date.parse(assignment.available_from_at) : Number.NaN;
  const dueAt = assignment.due_at ? Date.parse(assignment.due_at) : Number.NaN;
  if (Number.isFinite(opensAt) && now < opensAt) return "notOpen";
  if (Number.isFinite(dueAt) && now > dueAt) return "closed";
  return "open";
}
