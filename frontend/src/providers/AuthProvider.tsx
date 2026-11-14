"use client";

import React, {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
  useMemo,
} from "react";
import { useTranslations } from "next-intl";
import { apiFetch, ApiError } from "@/lib/api-client";
import {
  authRequestCoordinator,
  verifySessionAfterActiveLogin,
} from "@/lib/auth-request-coordinator.mjs";
import { isAdminRole, isSuperAdminRole, type UserRole } from "@/lib/roles";
import { useToast } from "@/providers/ToastProvider";

export interface AuthUser {
  id: string;
  username: string;
  email: string;
  role: UserRole;
  level: number;
  xp: number;
  points: number;
  streak_days: number;
  avatar_url: string | null;
  bio: string | null;
  email_verified: boolean;
  totp_enabled: boolean;
}

export type AuthSessionStatus = "authenticated" | "anonymous" | "unknown";
export type AuthCheckResult = AuthSessionStatus;
export type LogoutResult = "success" | "failed" | "unknown";

type FetchUserOptions = {
  background?: boolean;
  expectedEpoch?: number;
};

function sameAuthUser(current: AuthUser | null, next: AuthUser): boolean {
  return current !== null
    && current.id === next.id
    && current.username === next.username
    && current.email === next.email
    && current.role === next.role
    && current.level === next.level
    && current.xp === next.xp
    && current.points === next.points
    && current.streak_days === next.streak_days
    && current.avatar_url === next.avatar_url
    && current.bio === next.bio
    && current.email_verified === next.email_verified
    && current.totp_enabled === next.totp_enabled;
}

interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  /** 서버가 마지막으로 확정한 세션 상태. unknown은 네트워크/서버 장애로 확인하지 못한 상태다. */
  sessionStatus: AuthSessionStatus;
  /** 기존 세션 변경과 직렬화한 뒤 로그인하고 최종 사용자 상태를 확인한다. */
  login: (email: string, password: string) => Promise<AuthCheckResult>;
  /** 로그아웃 — 서버에서 쿠키 삭제 */
  logout: () => Promise<LogoutResult>;
  /** 사용자 정보 새로고침 */
  refresh: () => Promise<AuthCheckResult>;
  /** 관리자 여부 (admin 또는 moderator) */
  isAdmin: boolean;
  /** 최고 관리자 여부 (admin만) */
  isSuperAdmin: boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function isMissingSession(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used within AuthProvider");
  return context;
}

/** 인증 상태가 있으면 사용자 정보 반환, 없으면 null — SSR 안전 */
export function useOptionalAuth(): AuthContextValue | null {
  return useContext(AuthContext);
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [sessionStatus, setSessionStatus] = useState<AuthSessionStatus>("unknown");
  const { toast } = useToast();
  const t = useTranslations("common");
  const wasLoggedInRef = React.useRef(false);
  const userRef = React.useRef<AuthUser | null>(null);
  const sessionStatusRef = React.useRef<AuthSessionStatus>("unknown");
  const sessionEpochRef = React.useRef(0);
  const requestIdRef = React.useRef(0);
  const checkPromiseRef = React.useRef<Promise<AuthCheckResult> | null>(null);
  const refreshPromiseRef = React.useRef<Promise<void> | null>(null);
  const loginPromiseRef = React.useRef<Promise<AuthCheckResult> | null>(null);
  const logoutPromiseRef = React.useRef<Promise<LogoutResult> | null>(null);
  const logoutInFlightRef = React.useRef(false);

  const rotateRefreshToken = useCallback((): Promise<void> => {
    if (refreshPromiseRef.current) return refreshPromiseRef.current;

    const promise = apiFetch("/api/auth/refresh", {
      method: "POST",
      sessionRecovery: "bypass",
    }).then(() => undefined);
    refreshPromiseRef.current = promise;
    const clear = () => {
      if (refreshPromiseRef.current === promise) refreshPromiseRef.current = null;
    };
    void promise.then(clear, clear);
    return promise;
  }, []);

  const fetchUser = useCallback(async (
    options: FetchUserOptions = {},
  ): Promise<AuthCheckResult> => {
    const expectedEpoch = options.expectedEpoch;
    const epochChanged = () => (
      expectedEpoch !== undefined && expectedEpoch !== sessionEpochRef.current
    );
    if (logoutInFlightRef.current || epochChanged()) return "unknown";
    if (checkPromiseRef.current) {
      const result = await checkPromiseRef.current;
      return epochChanged() ? "unknown" : result;
    }

    const requestId = ++requestIdRef.current;
    if (!options.background) setLoading(true);

    const applyAuthenticated = (data: AuthUser): AuthCheckResult => {
      if (requestId !== requestIdRef.current || epochChanged()) return "unknown";
      const previous = userRef.current;
      if (previous !== null && previous.id !== data.id) sessionEpochRef.current += 1;
      userRef.current = data;
      sessionStatusRef.current = "authenticated";
      setUser((current) => sameAuthUser(current, data) ? current : data);
      setSessionStatus("authenticated");
      wasLoggedInRef.current = true;
      return "authenticated";
    };

    const applyAnonymous = (): AuthCheckResult => {
      if (requestId !== requestIdRef.current || epochChanged()) return "unknown";
      const shouldNotifyExpiry = wasLoggedInRef.current;
      if (userRef.current !== null) sessionEpochRef.current += 1;
      userRef.current = null;
      sessionStatusRef.current = "anonymous";
      setUser(null);
      setSessionStatus("anonymous");
      wasLoggedInRef.current = false;
      if (shouldNotifyExpiry) toast("info", t("sessionExpired"));
      return "anonymous";
    };

    const applyUnknown = (): AuthCheckResult => {
      if (requestId !== requestIdRef.current || epochChanged()) return "unknown";
      sessionStatusRef.current = "unknown";
      // 열린 편집 화면의 배경 확인 실패는 내부 요청만 막고 기존 사용자 UI는 유지한다.
      if (!options.background || userRef.current === null) setSessionStatus("unknown");
      toast("warning", t("authStatusUnavailable"));
      return "unknown";
    };

    const task = (async (): Promise<AuthCheckResult> => {
      try {
        // httpOnly 쿠키가 자동으로 전송됨 — 토큰이 없으면 401 반환
        const data = await apiFetch<AuthUser>("/api/auth/me", { sessionRecovery: "bypass" });
        return applyAuthenticated(data);
      } catch (err) {
        // 401이면 토큰 만료 — 현재 세션 검사만 refresh rotation을 수행한다.
        if (isMissingSession(err)) {
          if (requestId !== requestIdRef.current || logoutInFlightRef.current) return "unknown";
          try {
            await rotateRefreshToken();
            if (requestId !== requestIdRef.current || logoutInFlightRef.current) return "unknown";
            // refresh 성공 시 새 access_token 쿠키가 자동 설정됨
            const data = await apiFetch<AuthUser>("/api/auth/me", { sessionRecovery: "bypass" });
            return applyAuthenticated(data);
          } catch (refreshErr) {
            return isMissingSession(refreshErr) ? applyAnonymous() : applyUnknown();
          }
        }
        // 403은 밴 등 "인증된 세션의 접근 거절"일 수 있으므로 세션 만료로 단정하지 않는다.
        return applyUnknown();
      } finally {
        if (!options.background && requestId === requestIdRef.current) setLoading(false);
      }
    })();

    checkPromiseRef.current = task;
    try {
      return await task;
    } finally {
      if (checkPromiseRef.current === task) checkPromiseRef.current = null;
    }
  }, [rotateRefreshToken, toast, t]);

  const requestSessionSnapshot = useCallback(() => ({
    status: sessionStatusRef.current,
    epoch: sessionEpochRef.current,
    authenticated: userRef.current !== null,
    ownerId: userRef.current?.id ?? null,
  }), []);

  const verifyRequestSession = useCallback((expectedEpoch: number) => (
    verifySessionAfterActiveLogin({
      activeLogin: loginPromiseRef.current,
      expectedEpoch,
      currentEpoch: () => sessionEpochRef.current,
      verify: (epoch: number) => fetchUser({ background: true, expectedEpoch: epoch }),
    })
  ), [fetchUser]);

  useEffect(() => authRequestCoordinator.register({
    snapshot: requestSessionSnapshot,
    verify: verifyRequestSession,
  }), [requestSessionSnapshot, verifyRequestSession]);

  useEffect(() => {
    void fetchUser();
  }, [fetchUser]);

  // wasLoggedInRef 동기화
  useEffect(() => {
    wasLoggedInRef.current = user !== null;
  }, [user]);

  const login = useCallback((email: string, password: string): Promise<AuthCheckResult> => {
    if (loginPromiseRef.current) return loginPromiseRef.current;

    const task = (async (): Promise<AuthCheckResult> => {
      // 앞선 logout이 쿠키를 지운 다음 새 계정 쿠키를 발급해야 최종 순서가 안정적이다.
      const activeLogout = logoutPromiseRef.current;
      if (activeLogout) await activeLogout;

      // 이 시점보다 먼저 시작한 요청은 새 계정 쿠키로 전송하거나 재전송하지 않는다.
      sessionEpochRef.current += 1;
      // 아직 refresh를 시작하지 않은 이전 /me 검사를 먼저 무효화한다.
      const loginRequestId = ++requestIdRef.current;
      checkPromiseRef.current = null;
      setLoading(true);

      // 이미 시작된 refresh 응답의 Set-Cookie가 새 로그인 응답을 뒤늦게 덮지 않게 한다.
      const activeRefresh = refreshPromiseRef.current;
      if (activeRefresh) {
        try {
          await activeRefresh;
        } catch {
          // 로그인 요청이 새 세션의 최종 기준이므로 이전 refresh 실패는 그대로 진행한다.
        }
      }

      try {
        await apiFetch("/api/auth/login", {
          method: "POST",
          body: { email, password },
          sessionRecovery: "bypass",
        });
        return fetchUser();
      } catch (error) {
        if (loginRequestId === requestIdRef.current) setLoading(false);
        throw error;
      }
    })();

    loginPromiseRef.current = task;
    const clear = () => {
      if (loginPromiseRef.current === task) loginPromiseRef.current = null;
    };
    void task.then(clear, clear);
    return task;
  }, [fetchUser]);

  const logout = useCallback((): Promise<LogoutResult> => {
    if (logoutPromiseRef.current) return logoutPromiseRef.current;

    // logout 직전에 시작한 요청이 쿠키 폐기 뒤 재개되지 않게 먼저 세대를 바꾼다.
    sessionEpochRef.current += 1;
    logoutInFlightRef.current = true;
    const requestId = ++requestIdRef.current;
    checkPromiseRef.current = null;
    setLoading(true);

    const applyLoggedOut = (): LogoutResult => {
      if (requestId !== requestIdRef.current) return "unknown";
      userRef.current = null;
      sessionStatusRef.current = "anonymous";
      setUser(null);
      setSessionStatus("anonymous");
      wasLoggedInRef.current = false;
      return "success";
    };

    const probeAfterUncertainLogout = async (): Promise<LogoutResult> => {
      try {
        const current = await apiFetch<AuthUser>("/api/auth/me", { sessionRecovery: "bypass" });
        if (requestId !== requestIdRef.current) return "unknown";
        userRef.current = current;
        sessionStatusRef.current = "authenticated";
        setUser((previous) => sameAuthUser(previous, current) ? previous : current);
        setSessionStatus("authenticated");
        wasLoggedInRef.current = true;
        toast("error", t("logoutFailed"));
        return "failed";
      } catch (meError) {
        if (!isMissingSession(meError)) {
          if (requestId === requestIdRef.current) {
            sessionStatusRef.current = "unknown";
            setSessionStatus("unknown");
            toast("warning", t("logoutUnknown"));
          }
          return "unknown";
        }

        // access cookie의 401만으로는 살아 있는 refresh session을 배제할 수 없다.
        try {
          await rotateRefreshToken();
          const current = await apiFetch<AuthUser>("/api/auth/me", { sessionRecovery: "bypass" });
          if (requestId !== requestIdRef.current) return "unknown";
          userRef.current = current;
          sessionStatusRef.current = "authenticated";
          setUser((previous) => sameAuthUser(previous, current) ? previous : current);
          setSessionStatus("authenticated");
          wasLoggedInRef.current = true;
          toast("error", t("logoutFailed"));
          return "failed";
        } catch (refreshError) {
          // refresh 403은 밴 등 접근 거절일 수 있다. refresh 세션 부재를 뜻하는 401만 확정한다.
          if (isMissingSession(refreshError)) return applyLoggedOut();
          if (requestId === requestIdRef.current) {
            sessionStatusRef.current = "unknown";
            setSessionStatus("unknown");
            toast("warning", t("logoutUnknown"));
          }
          return "unknown";
        }
      }
    };

    const task = (async (): Promise<LogoutResult> => {
      try {
        // 이미 시작된 로그인과 최종 /me 확인이 끝난 뒤 그 세션을 폐기한다.
        const activeLogin = loginPromiseRef.current;
        if (activeLogin) {
          try {
            await activeLogin;
          } catch {
            // 실패한 로그인 뒤에도 기존 쿠키 폐기를 시도한다.
          }
        }
        // 이미 시작된 1회용 refresh rotation이 끝난 뒤 최신 쿠키를 폐기한다.
        const activeRefresh = refreshPromiseRef.current;
        if (activeRefresh) {
          try {
            await activeRefresh;
          } catch {
            // 실패한 rotation이어도 logout 요청으로 쿠키 삭제와 서버 폐기를 시도한다.
          }
        }
        await apiFetch("/api/auth/logout", {
          method: "POST",
          sessionRecovery: "bypass",
        });
        return applyLoggedOut();
      } catch {
        return await probeAfterUncertainLogout();
      } finally {
        logoutInFlightRef.current = false;
        if (requestId === requestIdRef.current) setLoading(false);
      }
    })();

    logoutPromiseRef.current = task;
    const clear = () => {
      if (logoutPromiseRef.current === task) logoutPromiseRef.current = null;
    };
    void task.then(clear, clear);
    return task;
  }, [rotateRefreshToken, t, toast]);

  const refresh = useCallback(
    (): Promise<AuthCheckResult> => loginPromiseRef.current ?? fetchUser(),
    [fetchUser],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      loading,
      sessionStatus,
      login,
      logout,
      refresh,
      isAdmin: isAdminRole(user?.role),
      isSuperAdmin: isSuperAdminRole(user?.role),
    }),
    [user, loading, sessionStatus, login, logout, refresh]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
