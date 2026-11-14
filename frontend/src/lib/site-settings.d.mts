export const DEFAULT_SITE_NAME: string;

export function normalizeSiteName(value: unknown): string;

export function titleWithSiteName(
  currentTitle: unknown,
  siteName: unknown,
  previousSiteName?: unknown,
): string;

export interface TrackedRequest {
  id: number;
  controller: AbortController;
}

export interface LatestRequestTracker {
  begin(): TrackedRequest;
  isCurrent(request: TrackedRequest): boolean;
  clear(request: TrackedRequest): void;
  dispose(): void;
}

export function createLatestRequestTracker(): LatestRequestTracker;
