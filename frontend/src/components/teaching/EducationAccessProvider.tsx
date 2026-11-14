"use client";

import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError } from "@/lib/api-client";
import { getEducationPermissions } from "@/components/teaching/education-api";
import { useAuth } from "@/providers/AuthProvider";
import type { EducationPermissions } from "@/types/education";

interface EducationAccessContextValue {
  permissions: EducationPermissions | null;
  loading: boolean;
  errorStatus: number | null;
  refresh: () => Promise<void>;
}

const EducationAccessContext = createContext<EducationAccessContextValue | null>(null);

export function EducationAccessProvider({ children }: { children: ReactNode }) {
  const { sessionStatus, user } = useAuth();
  const identity = `${sessionStatus}:${user?.id ?? "anonymous"}`;
  const identityRef = useRef(identity);
  useLayoutEffect(() => { identityRef.current = identity; }, [identity]);
  const [snapshot, setSnapshot] = useState<{
    identity: string;
    permissions: EducationPermissions | null;
    loading: boolean;
    errorStatus: number | null;
  }>({ identity, permissions: null, loading: true, errorStatus: null });
  const requestGenerationRef = useRef(0);
  const refresh = useCallback(async () => {
    if (identityRef.current !== identity) return;
    const generation = ++requestGenerationRef.current;
    const isCurrent = () => generation === requestGenerationRef.current && identityRef.current === identity;
    if (sessionStatus !== "authenticated") {
      setSnapshot({ identity, permissions: null, errorStatus: sessionStatus === "anonymous" ? 401 : null, loading: false });
      return;
    }
    setSnapshot({ identity, permissions: null, errorStatus: null, loading: true });
    try {
      const nextPermissions = await getEducationPermissions();
      if (isCurrent()) setSnapshot({ identity, permissions: nextPermissions, errorStatus: null, loading: false });
    } catch (error) {
      if (!isCurrent()) return;
      // Keep transport/unknown failures distinct from a confirmed 403.  A
      // missing status must still render a retry path rather than a false
      // "not authorized" decision.
      setSnapshot({ identity, permissions: null, errorStatus: error instanceof ApiError ? error.status : 0, loading: false });
    }
  }, [sessionStatus, identity]);
  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 0);
    return () => {
      window.clearTimeout(timer);
      requestGenerationRef.current += 1;
    };
  }, [refresh]);
  const value = useMemo(() => ({
    permissions: snapshot.identity === identity ? snapshot.permissions : null,
    loading: snapshot.identity === identity ? snapshot.loading : sessionStatus === "authenticated",
    errorStatus: snapshot.identity === identity ? snapshot.errorStatus : null,
    refresh,
  }), [snapshot, identity, sessionStatus, refresh]);
  return <EducationAccessContext.Provider value={value}>{children}</EducationAccessContext.Provider>;
}

export function useEducationAccess(): EducationAccessContextValue {
  const context = useContext(EducationAccessContext);
  if (!context) throw new Error("useEducationAccess must be used within EducationAccessProvider");
  return context;
}
