"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";
import { apiFetch } from "@/lib/api-client";
import { richContentToPlainText } from "@/lib/rich-content.mjs";
import { Link } from "@/i18n/navigation";
import { useAuth } from "@/providers/AuthProvider";

interface NotificationItem {
  id: string;
  title: string;
  content: string;
  type: "toast" | "popup" | "alert";
  created_at: string;
  source?: "global" | "personal";
  path?: string | null;
}

interface NotificationListResponse {
  items: NotificationItem[];
  total: number;
}

const POLL_INTERVAL = 60_000; // 60초
const LAST_SEEN_KEY = "notification_last_seen";
const DISMISSED_KEY = "notification_dismissed";

function readStorage(key: string) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function writeStorage(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* 저장이 차단되어도 알림 조회는 유지한다. */ }
}

function readDismissed(key: string): Set<string> {
  try {
    const parsed: unknown = JSON.parse(readStorage(key) ?? "[]");
    return new Set(Array.isArray(parsed) ? parsed.filter((value): value is string => typeof value === "string") : []);
  } catch { return new Set(); }
}

export function NotificationBell() {
  const t = useTranslations("notification");
  const { user } = useAuth();
  const userId = user?.id;
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const [dismissedIds, setDismissedIds] = useState<Set<string>>(new Set());
  const [unreadCount, setUnreadCount] = useState(0);
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const requestGeneration = useRef(0);
  const lastSeenKey = `${LAST_SEEN_KEY}:${userId ?? "anonymous"}`;
  const dismissedKey = `${DISMISSED_KEY}:${userId ?? "anonymous"}`;

  const closeNotifications = useCallback((restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) {
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    }
  }, []);

  const fetchNotifications = useCallback(async () => {
    if (!userId) return;
    const request = requestGeneration.current;
    try {
      const data = await apiFetch<NotificationListResponse>(
        "/api/auth/notifications?size=10",
      );
      if (request !== requestGeneration.current) return;
      setFetchedAt(Date.now());
      setItems(data.items);
      setLoadedFor(userId);

      const lastSeen = readStorage(lastSeenKey);
      const dismissed = readDismissed(dismissedKey);
      setDismissedIds(dismissed);
      const visible = data.items.filter((n) => !dismissed.has(n.id));
      if (lastSeen) {
        const count = visible.filter(
          (n) => new Date(n.created_at) > new Date(lastSeen),
        ).length;
        setUnreadCount(count);
      } else {
        setUnreadCount(visible.length);
      }
    } catch {
      // 비로그인 또는 네트워크 에러 — 무시
    }
  }, [dismissedKey, lastSeenKey, userId]);

  useEffect(() => {
    requestGeneration.current += 1;
    const initialFetch = window.setTimeout(() => {
      void fetchNotifications();
    }, 0);
    const interval = window.setInterval(() => {
      void fetchNotifications();
    }, POLL_INTERVAL);
    return () => {
      requestGeneration.current += 1;
      window.clearTimeout(initialFetch);
      window.clearInterval(interval);
    };
  }, [fetchNotifications]);

  /* 외부 클릭 닫기 */
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        closeNotifications();
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [closeNotifications, open]);

  useEffect(() => {
    if (!open) return;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeNotifications(true);
    };
    document.addEventListener("keydown", handleEscape);
    return () => document.removeEventListener("keydown", handleEscape);
  }, [closeNotifications, open]);

  const handleToggle = () => {
    const next = !open;
    setOpen(next);
    if (next) {
      writeStorage(lastSeenKey, new Date().toISOString());
      setUnreadCount(0);
    }
  };

  const formatTime = (iso: string) => {
    const notificationTime = new Date(iso).getTime();
    const diff = Math.max(0, (fetchedAt ?? notificationTime) - notificationTime);
    const minutes = Math.floor(diff / 60_000);
    if (minutes < 1) return t("justNow");
    if (minutes < 60) return t("minutesAgo", { n: minutes });
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return t("hoursAgo", { n: hours });
    const days = Math.floor(hours / 24);
    return t("daysAgo", { n: days });
  };

  const handleDismiss = (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    const next = new Set(dismissedIds);
    next.add(id);
    setDismissedIds(next);
    writeStorage(dismissedKey, JSON.stringify([...next]));
    // 즉시 unread 재계산
    const lastSeen = readStorage(lastSeenKey);
    const visible = items.filter((n) => !next.has(n.id));
    if (lastSeen) {
      setUnreadCount(
        visible.filter((n) => new Date(n.created_at) > new Date(lastSeen)).length,
      );
    } else {
      setUnreadCount(visible.length);
    }
  };

  const visibleItems = loadedFor === userId ? items.filter((n) => !dismissedIds.has(n.id)) : [];

  const typeStyle: Record<string, string> = {
    alert: "bg-accent-error/15 text-accent-error",
    popup: "bg-accent-warning/15 text-accent-warning",
    toast: "bg-accent-primary/15 text-accent-primary",
  };

  return (
    <div ref={ref} className="relative">
      <button
        ref={triggerRef}
        type="button"
        onClick={handleToggle}
        className="relative flex h-[44px] w-[44px] shrink-0 items-center justify-center rounded-lg p-0 text-text-secondary transition-colors hover:bg-bg-surface hover:text-accent-primary 2xl:h-auto 2xl:w-auto 2xl:p-2"
        aria-label={t("bell")}
        aria-controls="notification-panel"
        aria-expanded={open}
      >
        <svg
          className="h-4 w-4"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
        >
          <path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.73 21a2 2 0 01-3.46 0" />
        </svg>
        {loadedFor === userId && unreadCount > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-accent-error px-1 text-[10px] font-bold text-white">
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        )}
      </button>

      {open && (
        <div id="notification-panel" className="fixed inset-x-[8px] top-14 z-50 mt-1.5 flex max-h-[calc(100dvh-4rem)] w-auto flex-col overflow-hidden rounded-lg border border-border bg-bg-surface shadow-xl sm:left-auto sm:right-6 sm:w-[min(20rem,calc(100vw-3rem))] 2xl:absolute 2xl:inset-x-auto 2xl:right-0 2xl:top-full 2xl:w-80">
          <div className="border-b border-border px-4 py-3">
            <h3 className="text-sm font-semibold text-text-primary">
              {t("title")}
            </h3>
          </div>

          <div className="min-h-0 max-h-80 flex-1 overflow-x-hidden overflow-y-auto">
            {visibleItems.length === 0 ? (
              <div className="px-4 py-8 text-center text-sm text-text-muted">
                {t("noNotifications")}
              </div>
            ) : (
              visibleItems.map((n) => (
                <div
                  key={n.id}
                  className="group relative min-w-0 border-b border-border/50 px-4 py-3 transition-colors hover:bg-bg-elevated"
                >
                  <div className="flex min-w-0 items-start justify-between gap-2">
                    <p className="min-w-0 flex-1 [overflow-wrap:anywhere] text-sm font-medium text-text-primary">
                      {n.path ? <Link href={n.path} onClick={() => closeNotifications()} className="hover:underline">{n.title}</Link> : n.title}
                    </p>
                    <div className="flex shrink-0 items-center gap-1">
                      <span
                        className={cn(
                          "rounded px-1.5 py-0.5 text-[10px] font-medium",
                          typeStyle[n.type] ?? typeStyle.toast,
                        )}
                      >
                        {n.type}
                      </span>
                      <button
                        onClick={(e) => handleDismiss(n.id, e)}
                        className="rounded p-0.5 text-text-muted opacity-0 transition-opacity hover:bg-bg-surface hover:text-text-primary focus:opacity-100 group-hover:opacity-100 group-focus-within:opacity-100"
                        aria-label={t("dismiss")}
                        title={t("dismiss")}
                      >
                        <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                          <path d="M18 6L6 18" />
                          <path d="M6 6l12 12" />
                        </svg>
                      </button>
                    </div>
                  </div>
                  {n.content && (
                    <p className="mt-1 line-clamp-2 [overflow-wrap:anywhere] text-xs text-text-secondary">
                      {richContentToPlainText(n.content)}
                    </p>
                  )}
                  <p className="mt-1 text-[11px] text-text-muted">
                    {formatTime(n.created_at)}
                  </p>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
