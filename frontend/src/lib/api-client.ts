/**
 * API 클라이언트 — CSRF 토큰 자동 첨부 + httpOnly 쿠키 인증
 *
 * access_token은 httpOnly 쿠키로 관리되므로 브라우저가 자동 전송합니다.
 * JavaScript에서 토큰에 접근할 수 없어 XSS 토큰 탈취가 불가능합니다.
 */

/** basePath — next.config.ts의 basePath와 동일 */
const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH || "/ctf";

import { normalizeApiErrorPayload } from "@/lib/api-error.mjs";
import {
  AuthRequestBlockedError,
  authRequestCoordinator,
} from "@/lib/auth-request-coordinator.mjs";

/** 쿠키에서 CSRF 토큰 추출 (CSRF 쿠키는 httpOnly가 아님) */
function getCsrfToken(): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(/ctf_csrf_token=([^;]+)/);
  return match ? match[1] : null;
}

interface ApiOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  /** AuthProvider 자체의 세션 API만 재진입 방지를 위해 사용한다. */
  sessionRecovery?: "auto" | "bypass";
  requestOwner?: ApiRequestOwner;
}

type ApiFormOptions = Omit<RequestInit, "body" | "method"> & {
  method?: "POST" | "PUT";
  sessionRecovery?: "auto" | "bypass";
  requestOwner?: ApiRequestOwner;
};

export type ApiRequestOwner = ReturnType<typeof authRequestCoordinator.captureOwner>;

const AUTH_SESSION_REQUESTS = new Set([
  "GET /api/auth/me",
  "POST /api/auth/refresh",
  "POST /api/auth/login",
  "POST /api/auth/logout",
]);

function apiPathname(path: string): string | null {
  if (typeof window !== "undefined") {
    try {
      const url = new URL(path, window.location.origin);
      if (url.origin !== window.location.origin) return null;
      return url.pathname.startsWith(BASE_PATH + "/api/")
        ? url.pathname.slice(BASE_PATH.length)
        : url.pathname;
    } catch {
      return null;
    }
  }
  const pathname = path.split(/[?#]/, 1)[0];
  return pathname.startsWith(BASE_PATH + "/api/")
    ? pathname.slice(BASE_PATH.length)
    : pathname;
}

function usesSessionRecovery(path: string, method: string, mode: "auto" | "bypass"): boolean {
  if (mode === "bypass") return false;
  const pathname = apiPathname(path);
  return pathname !== null && pathname.startsWith("/api/") && !AUTH_SESSION_REQUESTS.has(`${method} ${pathname}`);
}

function sessionBlockedApiError(error: AuthRequestBlockedError): ApiError {
  if (error.reason === "anonymous") {
    return new ApiError(401, "인증이 필요합니다.", undefined, "auth_session_missing");
  }
  if (error.reason === "changed") {
    return new ApiError(409, "세션이 변경되어 요청을 중단했습니다. 다시 시도해 주세요.", undefined, "auth_session_changed");
  }
  return new ApiError(503, "로그인 상태를 확인하지 못했습니다. 다시 시도해 주세요.", undefined, "auth_status_unavailable");
}

export function captureApiRequestOwner(expectedUserId: string): ApiRequestOwner {
  try {
    return authRequestCoordinator.captureOwner(expectedUserId);
  } catch (error) {
    if (error instanceof AuthRequestBlockedError) throw sessionBlockedApiError(error);
    throw error;
  }
}

export function isCurrentApiRequestOwner(owner: ApiRequestOwner): boolean {
  return authRequestCoordinator.isCurrentOwner(owner);
}

async function consumeApiResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const normalized = normalizeApiErrorPayload(
      await response.json().catch(() => ({ detail: "Request failed" })),
    );
    throw new ApiError(response.status, normalized.message, normalized.detail, normalized.code, normalized.log);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

async function withSessionRecovery<T>(
  path: string,
  method: string,
  signal: AbortSignal | null | undefined,
  mode: "auto" | "bypass",
  requestOwner: ApiRequestOwner | undefined,
  execute: () => Promise<Response>,
): Promise<T> {
  try {
    return await authRequestCoordinator.run({
      execute,
      signal,
      requestOwner,
      bypass: !usesSessionRecovery(path, method, mode),
      consume: consumeApiResponse<T>,
    });
  } catch (error) {
    if (error instanceof AuthRequestBlockedError) throw sessionBlockedApiError(error);
    throw error;
  }
}

/**
 * API 요청 래퍼 — CSRF 헤더 자동 첨부, 인증은 httpOnly 쿠키가 자동 처리
 */
export async function apiFetch<T = unknown>(
  path: string,
  options: ApiOptions = {},
): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const { sessionRecovery = "auto", requestOwner, body, ...requestOptions } = options;
  const fullPath = path.startsWith("/") ? `${BASE_PATH}${path}` : path;
  return withSessionRecovery<T>(path, method, options.signal, sessionRecovery, requestOwner, () => {
    const headers: Record<string, string> = {
      ...(["GET", "HEAD", "OPTIONS"].includes(method) ? {} : { "Content-Type": "application/json" }),
      ...(options.headers as Record<string, string>),
    };
    if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
      const csrfToken = getCsrfToken();
      if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
    }
    return fetch(fullPath, {
      ...requestOptions,
      method,
      headers,
      credentials: "include",
      body: body ? JSON.stringify(body) : undefined,
    });
  });

}

/**
 * FormData 전송용 API 래퍼 — 파일 업로드 시 사용
 */
export async function apiFormFetch<T = unknown>(
  path: string,
  formData: FormData,
  options: ApiFormOptions = {},
): Promise<T> {
  const { sessionRecovery = "auto", requestOwner, method = "POST", ...requestOptions } = options;
  const fullPath = path.startsWith("/") ? `${BASE_PATH}${path}` : path;
  return withSessionRecovery<T>(path, method, options.signal, sessionRecovery, requestOwner, () => {
    const headers: Record<string, string> = {
      ...(options.headers as Record<string, string>),
    };
    const csrfToken = getCsrfToken();
    if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
    return fetch(fullPath, {
      ...requestOptions,
      method,
      headers,
      credentials: "include",
      // Content-Type은 설정하지 않음 (브라우저가 multipart boundary를 생성)
      body: formData,
    });
  });

}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    /** 구조화된 서버 오류 계약이 필요한 화면에서만 사용한다. */
    public detail?: unknown,
    /** 서버가 공개한 안정적인 오류 코드. 원문 오류를 파싱하지 않는다. */
    public code?: string,
    /** 관리자 전용 작업 화면에서만 표시할 수 있는 안전한 작업 로그. */
    public log?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
