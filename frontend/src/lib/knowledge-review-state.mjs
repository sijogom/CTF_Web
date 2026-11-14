export function readBulkReviewResults(ids, value) {
  const items = Array.isArray(value?.items) ? value.items : [];
  return ids.map((id) => {
    const item = items.find((entry) => entry?.proposal_id === id);
    const status = item?.status === "succeeded" || item?.status === "failed" ? item.status : "unknown";
    return { proposal_id: id, status, error: typeof item?.error === "string" ? item.error : undefined };
  });
}
