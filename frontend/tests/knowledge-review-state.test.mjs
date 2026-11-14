import assert from "node:assert/strict";
import test from "node:test";
import { readBulkReviewResults } from "../src/lib/knowledge-review-state.mjs";
test("bulk review reports each requested result separately", () => {
  assert.deepEqual(readBulkReviewResults(["a", "b"], { items: [{ proposal_id: "b", status: "failed", error: "403" }, { proposal_id: "a", status: "succeeded" }] }), [{ proposal_id: "a", status: "succeeded", error: undefined }, { proposal_id: "b", status: "failed", error: "403" }]);
});
test("missing or malformed bulk results never claim a failed or successful write", () => {
  assert.equal(readBulkReviewResults(["a"], null)[0].status, "unknown");
  assert.deepEqual(readBulkReviewResults(["a"], { items: [{ proposal_id: "other", status: "succeeded" }, { proposal_id: "a", status: "invalid", error: { private: "value" } }] }), [{ proposal_id: "a", status: "unknown", error: undefined }]);
});
