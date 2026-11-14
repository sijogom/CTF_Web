"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { apiFetch } from "@/lib/api-client";
import {
  DEFAULT_SITE_NAME,
  createLatestRequestTracker,
  normalizeSiteName,
  titleWithSiteName,
  type LatestRequestTracker,
} from "@/lib/site-settings.mjs";

export { DEFAULT_SITE_NAME } from "@/lib/site-settings.mjs";
export const SITE_SETTINGS_UPDATED_EVENT = "ctf:site-settings-updated";

interface PublicSettingsResponse {
  site_name: string;
}

interface SiteSettingsContextValue {
  siteName: string;
  refreshSiteSettings: () => Promise<void>;
}

const SiteSettingsContext = createContext<SiteSettingsContextValue | null>(null);

export function SiteSettingsProvider({ children }: { children: ReactNode }) {
  const [siteName, setSiteName] = useState(DEFAULT_SITE_NAME);
  const requestTrackerRef = useRef<LatestRequestTracker | null>(null);
  const previousSiteNameRef = useRef(DEFAULT_SITE_NAME);
  if (!requestTrackerRef.current) requestTrackerRef.current = createLatestRequestTracker();

  const refreshSiteSettings = useCallback(async () => {
    const tracker = requestTrackerRef.current;
    if (!tracker) return;
    const request = tracker.begin();
    try {
      const data = await apiFetch<PublicSettingsResponse>("/api/auth/settings/public", {
        signal: request.controller.signal,
      });
      if (tracker.isCurrent(request)) {
        setSiteName(normalizeSiteName(data.site_name));
      }
    } catch {
      if (!tracker.isCurrent(request)) return;
      // 공개 설정 장애가 페이지 자체를 막지 않도록 안정적인 제품 기본명을 유지한다.
      setSiteName((current) => current || DEFAULT_SITE_NAME);
    } finally {
      tracker.clear(request);
    }
  }, []);

  useEffect(() => {
    void refreshSiteSettings();
    const handleSettingsUpdated = () => void refreshSiteSettings();
    window.addEventListener(SITE_SETTINGS_UPDATED_EVENT, handleSettingsUpdated);
    return () => {
      window.removeEventListener(SITE_SETTINGS_UPDATED_EVENT, handleSettingsUpdated);
      requestTrackerRef.current?.dispose();
    };
  }, [refreshSiteSettings]);

  useEffect(() => {
    const previousSiteName = previousSiteNameRef.current;
    previousSiteNameRef.current = siteName;
    const syncTitle = () => {
      const nextTitle = titleWithSiteName(document.title, siteName, previousSiteName);
      if (document.title !== nextTitle) document.title = nextTitle;
    };
    syncTitle();
    const observer = new MutationObserver(syncTitle);
    observer.observe(document.head, { childList: true, subtree: true, characterData: true });
    return () => observer.disconnect();
  }, [siteName]);

  const value = useMemo(
    () => ({ siteName, refreshSiteSettings }),
    [siteName, refreshSiteSettings],
  );

  return <SiteSettingsContext.Provider value={value}>{children}</SiteSettingsContext.Provider>;
}

export function useSiteSettings(): SiteSettingsContextValue {
  const context = useContext(SiteSettingsContext);
  if (!context) throw new Error("useSiteSettings must be used within SiteSettingsProvider");
  return context;
}
