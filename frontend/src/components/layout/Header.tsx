"use client";

import { useState, useEffect, useCallback, useRef, type RefObject } from "react";
import { useTranslations } from "next-intl";
import { Link, usePathname } from "@/i18n/navigation";
import { cn } from "@/lib/utils";
import { AccessibilityPanel } from "./AccessibilityPanel";
import { NotificationBell } from "./NotificationBell";
import { useAuth, type LogoutResult } from "@/providers/AuthProvider";
import { useSiteSettings } from "@/providers/SiteSettingsProvider";

/** 네비게이션 경로 키 */
// 학생용 교육 화면은 직접 URL과 교육 전용 흐름에서만 접근한다.
// 일반 전역 메뉴에 임의로 노출하면 기존 학습/CTF 탐색과 권한 경계를 섞게 된다.
const NAV_KEYS = ["learn", "wargame", "ctf", "events", "community", "market", "ranking"] as const;

/* ─────────────────────────────────────────────
 * 로고 아이콘 — 방패 심볼 (단순화)
 * ───────────────────────────────────────────── */
function LogoIcon({ className }: { className?: string }) {
  return (
    <svg className={cn("h-7 w-7", className)} viewBox="0 0 32 32" fill="none">
      <path
        d="M16 2L4 7v8c0 7.73 5.12 14.96 12 17 6.88-2.04 12-9.27 12-17V7L16 2z"
        stroke="currentColor"
        strokeWidth="1.8"
        fill="none"
        className="text-accent-primary"
      />
      <path
        d="M16 10v4m0 4v4m-4-8h8"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
        className="text-accent-primary"
        opacity="0.5"
      />
      <circle cx="16" cy="16" r="2" className="fill-accent-primary" />
    </svg>
  );
}

/* ─────────────────────────────────────────────
 * 모바일 메뉴 슬라이드 패널
 * ───────────────────────────────────────────── */
function MobileMenu({
  open,
  onClose,
  navItems,
  pathname,
  user,
  isAdmin,
  onLogout,
  authUnknown,
  onRetryAuth,
  triggerRef,
  siteName,
}: {
  open: boolean;
  onClose: () => void;
  navItems: { key: string; href: string; label: string }[];
  pathname: string;
  user: { username: string; email: string; avatar_url?: string | null } | null;
  isAdmin: boolean;
  onLogout: () => Promise<LogoutResult>;
  authUnknown: boolean;
  onRetryAuth: () => Promise<unknown>;
  triggerRef: RefObject<HTMLButtonElement | null>;
  siteName: string;
}) {
  const t = useTranslations("nav");
  const ta = useTranslations("a11y");
  const tc = useTranslations("common");
  const [closing, setClosing] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const restoreFocusRef = useRef(true);

  const handleClose = useCallback((restoreFocus = true) => {
    if (closeTimerRef.current) return;
    restoreFocusRef.current = restoreFocus;
    setClosing(true);
    closeTimerRef.current = setTimeout(() => {
      closeTimerRef.current = null;
      setClosing(false);
      onClose();
      if (restoreFocusRef.current) triggerRef.current?.focus();
    }, 250);
  }, [onClose, triggerRef]);

  useEffect(() => () => {
    if (closeTimerRef.current) clearTimeout(closeTimerRef.current);
  }, []);

  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        handleClose();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, handleClose]);

  useEffect(() => {
    if (open) document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = ""; };
  }, [open]);

  if (!open) return null;

  const trapKeyboardFocus = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab" || !dialogRef.current) return;
    const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )).filter((element) => !element.hasAttribute("hidden"));
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <>
      <div
        className={cn(
          "fixed inset-0 z-[60] bg-bg-primary/60 backdrop-blur-sm",
          closing ? "animate-fade-in opacity-0" : "animate-fade-in"
        )}
        onClick={() => handleClose()}
        aria-hidden="true"
      />

      <div
        id="mobile-navigation-dialog"
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="mobile-navigation-title"
        onKeyDown={trapKeyboardFocus}
        className={cn(
          "fixed right-0 top-0 z-[70] flex h-full w-[min(85vw,340px)] flex-col bg-bg-surface shadow-2xl",
          closing ? "animate-slide-out-right" : "animate-slide-in-right"
        )}
      >
        {/* 패널 헤더 */}
        <div className="flex items-center justify-between border-b border-border px-5 py-4">
          <span id="mobile-navigation-title" className="min-w-0 truncate whitespace-nowrap text-sm font-semibold text-text-primary" title={siteName}>{siteName}</span>
          <button
            ref={closeButtonRef}
            type="button"
            onClick={() => handleClose()}
            className="rounded-lg p-2 text-text-secondary transition-colors hover:bg-bg-elevated hover:text-text-primary"
            aria-label={ta("closeMenu")}
          >
            <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* 네비게이션 */}
        <nav className="flex-1 overflow-y-auto px-3 py-3">
          <div className="space-y-0.5">
            {navItems.map(({ key, href, label }) => {
              const active = pathname.startsWith(href);
              return (
                <Link
                  key={key}
                  href={href}
                  onClick={() => handleClose(false)}
                  className={cn(
                    "block rounded-lg px-4 py-3 text-sm font-medium transition-colors",
                    active
                      ? "bg-accent-primary/10 text-accent-primary"
                      : "text-text-secondary hover:bg-bg-elevated hover:text-text-primary"
                  )}
                >
                  {label}
                </Link>
              );
            })}
          </div>
        </nav>

        {/* 하단 — 인증 상태 */}
        <div className="border-t border-border p-4">
          {user ? (
            <div className="space-y-3">
              <div className="flex items-center gap-3 px-1">
                {user.avatar_url ? (
                  <img src={user.avatar_url} alt={user.username} className="h-8 w-8 rounded-full object-cover" />
                ) : (
                  <span className="flex h-8 w-8 items-center justify-center rounded-full bg-accent-primary/15 text-xs font-bold text-accent-primary">
                    {user.username.charAt(0).toUpperCase()}
                  </span>
                )}
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-text-primary">{user.username}</p>
                  <p className="truncate text-xs text-text-muted">{user.email}</p>
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Link
                  href="/profile"
                   onClick={() => handleClose(false)}
                  className="flex flex-1 items-center justify-center rounded-lg border border-border px-3 py-2.5 text-sm font-medium text-text-secondary transition-colors hover:text-text-primary"
                >
                  {t("profile")}
                </Link>
                {isAdmin && (
                  <Link
                    href="/admin"
                     onClick={() => handleClose(false)}
                    className="flex items-center justify-center rounded-lg border border-accent-primary/30 bg-accent-primary/10 px-3 py-2.5 text-sm font-medium text-accent-primary"
                  >
                    {t("admin")}
                  </Link>
                )}
                <button
                  type="button"
                  onClick={async () => {
                    const result = await onLogout();
                    if (result === "success") handleClose();
                  }}
                  className="flex items-center justify-center rounded-lg border border-accent-error/30 px-3 py-2.5 text-accent-error"
                  aria-label={t("logout")}
                >
                  <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                    <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9" />
                  </svg>
                </button>
              </div>
            </div>
          ) : authUnknown ? (
            <button
              type="button"
              onClick={() => void onRetryAuth()}
              title={tc("authStatusUnavailable")}
              className="flex w-full items-center justify-center rounded-lg border border-border px-4 py-2.5 text-sm font-semibold text-text-secondary transition-colors hover:bg-bg-elevated"
            >
              {tc("retry")}
            </button>
          ) : (
            <Link
              href="/login"
              onClick={() => handleClose(false)}
              className="flex w-full items-center justify-center rounded-lg bg-accent-primary px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-accent-primary-hover"
            >
              {t("login")}
            </Link>
          )}
        </div>
      </div>
    </>
  );
}

/* ─────────────────────────────────────────────
 * Header — 1단 네비게이션, 텍스트 위주
 * 참고: HackTheBox, PicoCTF, Root-Me 스타일
 * ───────────────────────────────────────────── */
export function Header() {
  const pathname = usePathname();
  const t = useTranslations("nav");
  const ta = useTranslations("a11y");
  const tc = useTranslations("common");
  const { user, loading: authLoading, sessionStatus, logout, refresh, isAdmin } = useAuth();
  const { siteName } = useSiteSettings();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const profileRef = useRef<HTMLDivElement>(null);
  const profileTriggerRef = useRef<HTMLButtonElement>(null);
  const profileMenuRef = useRef<HTMLDivElement>(null);
  const profileInitialFocusRef = useRef<"first" | "last">("first");
  const mobileTriggerRef = useRef<HTMLButtonElement>(null);

  const closeProfileMenu = useCallback((restoreFocus = false) => {
    setProfileOpen(false);
    if (restoreFocus) profileTriggerRef.current?.focus();
  }, []);

  /* 프로필 드롭다운 외부 클릭 닫기 */
  useEffect(() => {
    if (!profileOpen) return;
    const handler = (e: MouseEvent) => {
      if (profileRef.current && !profileRef.current.contains(e.target as Node)) {
        closeProfileMenu();
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [profileOpen, closeProfileMenu]);

  useEffect(() => {
    if (!profileOpen) return;
    const frame = window.requestAnimationFrame(() => {
      const items = profileMenuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]');
      if (!items?.length) return;
      items[profileInitialFocusRef.current === "last" ? items.length - 1 : 0]?.focus();
    });
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeProfileMenu(true);
      }
    };
    document.addEventListener("keydown", handleEscape);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleEscape);
    };
  }, [profileOpen, closeProfileMenu]);

  const handleProfileMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!profileMenuRef.current) return;
    const items = Array.from(profileMenuRef.current.querySelectorAll<HTMLElement>('[role="menuitem"]'));
    if (items.length === 0) return;
    const currentIndex = Math.max(0, items.indexOf(document.activeElement as HTMLElement));
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown") nextIndex = (currentIndex + 1) % items.length;
    else if (event.key === "ArrowUp") nextIndex = (currentIndex - 1 + items.length) % items.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = items.length - 1;
    if (nextIndex !== null) {
      event.preventDefault();
      items[nextIndex].focus();
    }
  };

  /* 스크롤 감지 — 배경 강화 */
  useEffect(() => {
    const handleScroll = () => setScrolled(window.scrollY > 8);
    window.addEventListener("scroll", handleScroll, { passive: true });
    return () => window.removeEventListener("scroll", handleScroll);
  }, []);

  const navItems = NAV_KEYS.map((key) => ({
    key,
    href: `/${key}`,
    label: t(key),
  }));

  return (
    <>
      <header
        className={cn(
          "header-always-dark sticky top-0 z-50 border-b border-border/60 transition-colors duration-200",
          scrolled
            ? "bg-bg-primary backdrop-blur-xl"
            : "bg-bg-primary/95 backdrop-blur-md"
        )}
      >
        <div className="mx-auto flex h-14 max-w-7xl items-center px-[8px] sm:px-[12px] 2xl:px-6">
          {/* 로고 */}
          <Link href="/" className="mr-[4px] flex min-w-0 flex-1 items-center gap-[4px] 2xl:mr-8 2xl:flex-none 2xl:gap-2" title={siteName}>
            <LogoIcon className="h-[28px] w-[28px] shrink-0" />
            <span className="min-w-0 max-w-28 truncate whitespace-nowrap text-[15px] font-bold tracking-tight text-text-primary sm:max-w-40 2xl:max-w-56">
              {siteName}
            </span>
          </Link>

          {/* 데스크톱 네비게이션 — 텍스트만 */}
          <nav className="hidden h-full items-center gap-1 2xl:flex">
            {navItems.map(({ key, href, label }) => {
              const active = pathname.startsWith(href);
              return (
                <Link
                  key={key}
                  href={href}
                  className={cn(
                    "relative flex h-full items-center px-3 text-[13px] font-medium transition-colors",
                    active
                      ? "text-text-primary"
                      : "text-text-muted hover:text-text-primary"
                  )}
                >
                  {label}
                  {/* 액티브 밑줄 — 단순 2px 라인 */}
                  {active && (
                    <span className="absolute bottom-0 left-2 right-2 h-[2px] rounded-full bg-accent-primary" />
                  )}
                </Link>
              );
            })}
          </nav>

          {/* 우측 영역 */}
          <div className="ml-auto flex shrink-0 items-center gap-[2px] 2xl:gap-1.5">
            {/* 검색 아이콘 */}
            <Link
              href="/search"
              className="flex h-[44px] w-[44px] shrink-0 items-center justify-center rounded-lg p-0 text-text-secondary transition-colors hover:bg-bg-surface hover:text-accent-primary 2xl:h-auto 2xl:w-auto 2xl:p-2"
              aria-label={t("search")}
            >
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <circle cx="11" cy="11" r="8" />
                <path d="M21 21l-4.35-4.35" />
              </svg>
            </Link>

            {/* 알림 벨 — 로그인 사용자만 */}
            {user && <NotificationBell />}

            <AccessibilityPanel />

            {/* 데스크톱 인증 */}
            {authLoading && !user ? (
              <div className="hidden h-8 w-16 animate-pulse rounded-md bg-bg-surface 2xl:block" />
            ) : user ? (
              <div ref={profileRef} className="relative hidden 2xl:block">
                <button
                  ref={profileTriggerRef}
                  type="button"
                  id="profile-menu-trigger"
                  aria-label={`${user.username} · ${t("profile")}`}
                  aria-haspopup="menu"
                  aria-controls="profile-menu"
                  aria-expanded={profileOpen}
                  onClick={() => {
                    profileInitialFocusRef.current = "first";
                    setProfileOpen((current) => !current);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                      event.preventDefault();
                      profileInitialFocusRef.current = event.key === "ArrowUp" ? "last" : "first";
                      setProfileOpen(true);
                    }
                  }}
                  className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] text-text-secondary transition-colors hover:bg-bg-surface hover:text-text-primary"
                >
                  {user.avatar_url ? (
                    <img src={user.avatar_url} alt={user.username} className="h-6 w-6 rounded-full object-cover" />
                  ) : (
                    <span className="flex h-6 w-6 items-center justify-center rounded-full bg-accent-primary/15 text-[10px] font-bold text-accent-primary">
                      {user.username.charAt(0).toUpperCase()}
                    </span>
                  )}
                  <span className="max-w-[100px] truncate">{user.username}</span>
                  <svg className={cn("h-3 w-3 transition-transform", profileOpen && "rotate-180")} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                    <path d="M6 9l6 6 6-6" />
                  </svg>
                </button>

                {profileOpen && (
                  <div
                    id="profile-menu"
                    ref={profileMenuRef}
                    role="menu"
                    aria-labelledby="profile-menu-trigger"
                    onKeyDown={handleProfileMenuKeyDown}
                    className="absolute right-0 top-full mt-1.5 w-48 rounded-lg border border-border bg-bg-surface py-1 shadow-xl"
                  >
                    <div className="border-b border-border px-3 py-2">
                      <p className="text-sm font-medium text-text-primary">{user.username}</p>
                      <p className="text-xs text-text-muted">{user.email}</p>
                      {"points" in user && (
                        <p className="mt-1 text-xs font-medium text-accent-warning">
                          {(user as { points: number }).points.toLocaleString()} P
                        </p>
                      )}
                    </div>
                    <Link
                      href="/profile"
                      role="menuitem"
                      onClick={() => closeProfileMenu()}
                      className="block px-3 py-2 text-sm text-text-secondary hover:bg-bg-elevated hover:text-text-primary"
                    >
                      {t("profile")}
                    </Link>
                    {isAdmin && (
                      <Link
                        href="/admin"
                        role="menuitem"
                        onClick={() => closeProfileMenu()}
                        className="block px-3 py-2 text-sm text-accent-primary hover:bg-bg-elevated"
                      >
                        {t("admin")}
                      </Link>
                    )}
                    <button
                      type="button"
                      role="menuitem"
                      onClick={async () => {
                        const result = await logout();
                        if (result === "success") closeProfileMenu(true);
                      }}
                      className="block w-full px-3 py-2 text-left text-sm text-accent-error hover:bg-bg-elevated"
                    >
                      {t("logout")}
                    </button>
                  </div>
                )}
              </div>
            ) : sessionStatus === "unknown" ? (
              <button
                type="button"
                onClick={() => void refresh()}
                title={tc("authStatusUnavailable")}
                className="hidden rounded-md border border-border px-3.5 py-1.5 text-[13px] font-semibold text-text-secondary transition-colors hover:bg-bg-surface 2xl:block"
              >
                {tc("retry")}
              </button>
            ) : (
              <Link
                href="/login"
                className="hidden rounded-md bg-accent-primary px-3.5 py-1.5 text-[13px] font-semibold text-white transition-colors hover:bg-accent-primary-hover 2xl:block"
              >
                {t("login")}
              </Link>
            )}

            {/* 모바일 햄버거 */}
            <button
              ref={mobileTriggerRef}
              type="button"
              className={cn(
                "flex h-[44px] w-[44px] shrink-0 items-center justify-center rounded-md p-0 text-text-secondary transition-colors hover:bg-bg-surface hover:text-text-primary 2xl:hidden",
                mobileOpen && "bg-bg-surface text-text-primary"
              )}
              onClick={() => setMobileOpen(!mobileOpen)}
              aria-label={ta("toggleNavMenu")}
              aria-expanded={mobileOpen}
              aria-controls="mobile-navigation-dialog"
            >
              <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                {mobileOpen ? (
                  <path d="M18 6L6 18M6 6l12 12" />
                ) : (
                  <>
                    <path d="M4 6h16" />
                    <path d="M4 12h16" />
                    <path d="M4 18h16" />
                  </>
                )}
              </svg>
            </button>
          </div>
        </div>
      </header>

      <MobileMenu
        open={mobileOpen}
        onClose={() => setMobileOpen(false)}
        navItems={navItems}
        pathname={pathname}
        user={user}
        isAdmin={isAdmin}
        onLogout={logout}
        authUnknown={sessionStatus === "unknown"}
        onRetryAuth={refresh}
        triggerRef={mobileTriggerRef}
        siteName={siteName}
      />
    </>
  );
}
