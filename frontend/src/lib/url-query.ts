export type UrlHistoryMode = "push" | "replace";

export type UrlQueryValue = string | number | boolean | null | undefined;

/** URL에서 유효한 1 이상 정수만 읽는다. */
export function readPositiveInt(value: string | null, fallback = 1): number {
  if (!value || !/^\d+$/.test(value)) return fallback;
  const parsed = Number.parseInt(value, 10);
  return Number.isSafeInteger(parsed) && parsed >= 1 ? parsed : fallback;
}

/** 허용 목록 밖의 URL 값은 안전한 기본값으로 되돌린다. */
export function readEnum<T extends string>(
  value: string | null,
  allowed: readonly T[],
  fallback: T,
): T {
  return value && allowed.includes(value as T) ? (value as T) : fallback;
}

/**
 * 현재 경로와 다른 query를 보존하면서 지정한 값만 갱신한다.
 * null, undefined, 빈 문자열, false는 기본값으로 보고 URL에서 제거한다.
 */
export function writeUrlQuery(
  updates: Record<string, UrlQueryValue>,
  mode: UrlHistoryMode = "replace",
): void {
  if (typeof window === "undefined") return;

  const url = new URL(window.location.href);
  for (const [key, value] of Object.entries(updates)) {
    if (value === null || value === undefined || value === "" || value === false) {
      url.searchParams.delete(key);
    } else {
      url.searchParams.set(key, value === true ? "1" : String(value));
    }
  }

  const next = `${url.pathname}${url.search}${url.hash}`;
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (next === current) return;
  window.history[mode === "push" ? "pushState" : "replaceState"](
    null,
    "",
    next,
  );
}

