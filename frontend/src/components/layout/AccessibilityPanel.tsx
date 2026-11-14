"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter, usePathname } from "@/i18n/navigation";
import { useLocale, useTranslations } from "next-intl";
import { useAccessibility } from "@/lib/accessibility/context";
import type { ColorVisionMode, FontSizeLevel, ThemeMode } from "@/lib/accessibility/types";
import { routing } from "@/i18n/routing";
import { runGuardedNavigation } from "@/lib/use-unsaved-changes-guard";

const LANGUAGES = [
  { code: "ko", label: "한국어" },
  { code: "en", label: "English" },
  { code: "ja", label: "日本語" },
  { code: "zh", label: "中文" },
] as const;

export function AccessibilityPanel() {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const { state, update, reset } = useAccessibility();
  const locale = useLocale();
  const router = useRouter();
  const pathname = usePathname();
  const t = useTranslations("a11y");

  const COLOR_MODES: { value: ColorVisionMode; label: string }[] = [
    { value: "normal", label: t("colorNormal") },
    { value: "protanopia", label: t("colorProtanopia") },
    { value: "deuteranopia", label: t("colorDeuteranopia") },
    { value: "tritanopia", label: t("colorTritanopia") },
    { value: "high-contrast", label: t("colorHighContrast") },
  ];

  const FONT_SIZES: { value: FontSizeLevel; label: string }[] = [
    { value: "small", label: t("fontSmall") },
    { value: "default", label: t("fontDefault") },
    { value: "large", label: t("fontLarge") },
  ];

  const TOGGLE_OPTIONS = [
    { key: "keyboardNav" as const, label: t("keyboardNav") },
    { key: "screenReader" as const, label: t("screenReader") },
    { key: "reducedMotion" as const, label: t("reducedMotion") },
    { key: "easyRead" as const, label: t("easyRead") },
  ];

  const closePanel = useCallback((restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) {
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => {
      panelRef.current?.querySelector<HTMLElement>(
        'button:not([disabled]), select:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )?.focus();
    });
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closePanel(true);
    };
    document.addEventListener("keydown", handleEscape);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleEscape);
    };
  }, [closePanel, open]);

  const trapKeyboardFocus = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab" || !panelRef.current) return;
    const focusable = Array.from(panelRef.current.querySelectorAll<HTMLElement>(
      'button:not([disabled]), select:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )).filter((element) => !element.hasAttribute("hidden"));
    if (focusable.length === 0) {
      event.preventDefault();
      panelRef.current.focus();
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
    <div className="relative">
      <button
        ref={triggerRef}
        onClick={() => (open ? closePanel() : setOpen(true))}
        aria-label={t("title")}
        aria-expanded={open}
        className="flex h-[44px] w-[44px] shrink-0 items-center justify-center gap-0 rounded-lg p-0 text-sm font-medium text-text-secondary transition-colors hover:bg-bg-surface hover:text-accent-primary 2xl:h-auto 2xl:w-auto 2xl:gap-1.5 2xl:px-3 2xl:py-2"
      >
        <svg aria-hidden="true" className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
          <path fillRule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clipRule="evenodd" />
        </svg>
        <span className="hidden 2xl:inline">{t("title").split(" ")[0]}</span>
      </button>

      {open && (
        <>
          {/* 배경 오버레이 */}
          <div className="fixed inset-0 z-40" onClick={() => closePanel(true)} />

          {/* 패널 */}
          <div
            ref={panelRef}
            role="dialog"
            aria-modal="true"
            aria-label={t("title")}
            tabIndex={-1}
            onKeyDown={trapKeyboardFocus}
            className="a11y-panel-theme-aware fixed inset-x-3 top-16 z-50 max-h-[calc(100dvh-5rem)] w-auto overflow-y-auto overscroll-contain rounded-xl border border-border bg-bg-surface p-5 shadow-2xl sm:left-auto sm:right-6 sm:w-80"
          >
            <h3 className="mb-4 text-lg font-bold text-text-primary">{t("title")}</h3>

            {/* 언어 선택 */}
            <div className="mb-4">
              <label htmlFor="lang-select" className="mb-2 block text-sm font-medium text-text-primary">
                {t("language")}
              </label>
              <select
                id="lang-select"
                value={locale}
                onChange={(e) => {
                  const newLocale = e.target.value;
                  if (routing.locales.includes(newLocale as typeof routing.locales[number])) {
                    const result = runGuardedNavigation(() => {
                      router.replace(pathname, { locale: newLocale });
                    }, { onCancel: () => setOpen(true) });
                    if (result === "pending") closePanel();
                  }
                }}
                className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm text-text-primary"
              >
                {LANGUAGES.map(({ code, label }) => (
                  <option key={code} value={code}>{label}</option>
                ))}
              </select>
            </div>

            {/* 테마 모드 */}
            <div className="mb-4">
              <label className="mb-2 block text-sm font-medium text-text-primary">{t("theme")}</label>
              <div className="flex gap-2">
                {(["dark", "light", "system"] as ThemeMode[]).map((mode) => (
                  <button
                    key={mode}
                    onClick={() => update({ themeMode: mode })}
                    aria-pressed={state.themeMode === mode}
                    className={`flex-1 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                      state.themeMode === mode
                        ? "bg-accent-primary text-white"
                        : "bg-bg-primary text-text-secondary hover:text-text-primary"
                    }`}
                  >
                    {t(`theme${mode.charAt(0).toUpperCase() + mode.slice(1)}`)}
                  </button>
                ))}
              </div>
            </div>

            {/* 글자 크기 */}
            <div className="mb-4">
              <label className="mb-2 block text-sm font-medium text-text-primary">{t("fontSize")}</label>
              <div className="flex gap-2">
                {FONT_SIZES.map(({ value, label }) => (
                  <button
                    key={value}
                    onClick={() => update({ fontSize: value })}
                    aria-pressed={state.fontSize === value}
                    className={`flex-1 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                      state.fontSize === value
                        ? "bg-accent-primary text-white"
                        : "bg-bg-primary text-text-secondary hover:text-text-primary"
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>

            {/* 색각 모드 */}
            <div className="mb-4">
              <label htmlFor="color-vision-select" className="mb-2 block text-sm font-medium text-text-primary">
                {t("colorVision")}
              </label>
              <select
                id="color-vision-select"
                value={state.colorVisionMode}
                onChange={(e) => update({ colorVisionMode: e.target.value as ColorVisionMode })}
                className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm text-text-primary"
              >
                {COLOR_MODES.map(({ value, label }) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </div>

            {/* 토글 스위치들 */}
            {TOGGLE_OPTIONS.map(({ key, label }) => (
              <label key={key} className="mb-3 flex cursor-pointer items-center justify-between">
                <span className="text-sm text-text-primary">{label}</span>
                <button
                  role="switch"
                  aria-checked={state[key]}
                  onClick={() => update({ [key]: !state[key] })}
                  className={`relative h-6 w-11 rounded-full transition-colors ${
                    state[key] ? "bg-accent-primary" : "bg-bg-primary"
                  }`}
                >
                  <span
                    className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                      state[key] ? "translate-x-5" : ""
                    }`}
                  />
                </button>
              </label>
            ))}

            {/* 초기화 버튼 */}
            <button
              onClick={reset}
              className="mt-2 w-full rounded-lg border border-border py-2 text-sm text-text-secondary hover:text-text-primary"
            >
              {t("reset")}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
