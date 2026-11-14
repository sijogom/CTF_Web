const PURCHASE_STATES = new Set(["not_started", "processing", "completed", "rejected"]);

export function parsePurchaseStatus(value) {
  if (!value || typeof value !== "object" || !PURCHASE_STATES.has(value.status)) {
    throw new Error("Invalid purchase status response");
  }
  if (value.status === "processing" && (typeof value.operation_id !== "string" || !value.operation_id)) {
    throw new Error("Processing purchase requires an operation ID");
  }
  if (value.status === "completed" && (!Number.isInteger(value.price_paid) || value.price_paid < 1)) {
    throw new Error("Completed purchase requires a price snapshot");
  }
  return {
    status: value.status,
    operationId: typeof value.operation_id === "string" ? value.operation_id : null,
    purchased: value.status === "completed",
    retryable: value.status === "processing" && value.retryable === true,
    failureCode: typeof value.failure_code === "string" ? value.failure_code : null,
  };
}
