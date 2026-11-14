export function readBulkReviewResults(ids: string[], value: unknown): Array<{ proposal_id: string; status: "succeeded" | "failed" | "unknown"; error?: string }>;
