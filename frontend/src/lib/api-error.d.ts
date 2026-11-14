export interface NormalizedApiError {
  detail: unknown;
  message: string;
  code?: string;
  log?: string;
}

export function normalizeApiErrorPayload(payload: unknown): NormalizedApiError;

