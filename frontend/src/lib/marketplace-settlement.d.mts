export interface PurchaseStatus {
  status: "not_started" | "processing" | "completed" | "rejected";
  operationId: string | null;
  purchased: boolean;
  retryable: boolean;
  failureCode: string | null;
}
export function parsePurchaseStatus(value: unknown): PurchaseStatus;
