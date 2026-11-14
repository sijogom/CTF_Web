import assert from "node:assert/strict";
import test from "node:test";
import { parsePurchaseStatus } from "../src/lib/marketplace-settlement.mjs";

test("an accepted but unconfirmed payment never grants download access", () => {
  const result = parsePurchaseStatus({ status: "processing", operation_id: "op-1", retryable: true, purchased: true });
  assert.equal(result.purchased, false);
  assert.equal(result.retryable, true);
  assert.equal(result.operationId, "op-1");
});

test("legacy purchases and confirmed operations both grant download access", () => {
  for (const operation_id of [null, "op-1"]) {
    assert.equal(parsePurchaseStatus({ status: "completed", operation_id, price_paid: 100 }).purchased, true);
  }
});

test("rejected and not-started states grant no purchase access", () => {
  for (const status of ["not_started", "rejected"]) {
    assert.equal(parsePurchaseStatus({ status, purchased: true }).purchased, false);
  }
});

test("blocked settlement cannot offer a new charge retry", () => {
  assert.equal(parsePurchaseStatus({ status: "processing", operation_id: "op-1", retryable: false }).retryable, false);
});

test("missing, malformed, and incomplete replies cannot be mistaken for success", () => {
  for (const payload of [null, {}, { status: "success" }, { status: "processing" }, { status: "completed" }, { status: "completed", price_paid: 0 }]) {
    assert.throws(() => parsePurchaseStatus(payload));
  }
});
