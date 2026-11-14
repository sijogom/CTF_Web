"use client";

import { createContext, useContext, useState, useEffect, useRef, type ReactNode } from "react";
import { type AccessibilityState, DEFAULT_A11Y } from "./types";

interface A11yContextType {
  state: AccessibilityState;
  update: (partial: Partial<AccessibilityState>) => void;
  reset: () => void;
}

const A11yContext = createContext<A11yContextType | null>(null);

const STORAGE_KEY = "ctf-a11y-settings";

export function AccessibilityProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AccessibilityState>(DEFAULT_A11Y);
  const initialized = useRef(false);

  // localStorage + OS 설정 복원 (마운트 1회)
  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;

    let restored = { ...DEFAULT_A11Y };
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) restored = { ...DEFAULT_A11Y, ...JSON.parse(saved) };
    } catch {
      // localStorage 접근 불가 시 기본값 유지
    }

    // OS 설정 감지
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      restored.reducedMotion = true;
    }
    if (window.matchMedia("(prefers-contrast: more)").matches) {
      restored.colorVisionMode = "high-contrast";
    }

    // 한 번의 setState로 일괄 반영 (마운트 시 localStorage 복원은 의도적 패턴)
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setState(restored);
  }, []);

  // 상태 변경 시 저장 + CSS 반영
  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    const root = document.documentElement;

    // 테마 모드 적용
    const resolvedTheme = state.themeMode === "system"
      ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
      : state.themeMode;
    root.classList.remove("dark", "light");
    root.classList.add(resolvedTheme);
    root.style.colorScheme = resolvedTheme;

    // 글자 크기
    const fontSizeMap = { small: "14px", default: "16px", large: "20px" };
    root.style.fontSize = fontSizeMap[state.fontSize];

    // 색각 모드 CSS 클래스
    root.dataset.colorVision = state.colorVisionMode;

    // 모션 감소
    root.dataset.reducedMotion = state.reducedMotion ? "true" : "false";

    // 키보드 탐색 강조
    root.dataset.keyboardNav = state.keyboardNav ? "true" : "false";

    // 쉬운 읽기
    root.dataset.easyRead = state.easyRead ? "true" : "false";
  }, [state]);

  const update = (partial: Partial<AccessibilityState>) => {
    setState((prev) => ({ ...prev, ...partial }));
  };

  const reset = () => setState(DEFAULT_A11Y);

  return (
    <A11yContext.Provider value={{ state, update, reset }}>
      {children}
    </A11yContext.Provider>
  );
}

export function useAccessibility() {
  const ctx = useContext(A11yContext);
  if (!ctx) throw new Error("AccessibilityProvider가 필요합니다.");
  return ctx;
}
