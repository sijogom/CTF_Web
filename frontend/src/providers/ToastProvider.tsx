"use client";

import * as ToastPrimitive from "@radix-ui/react-toast";
import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import { createContext, useContext, useCallback, useState } from "react";

type ToastType = "success" | "error" | "warning" | "info";

interface ToastItem {
  id: string;
  type: ToastType;
  title: string;
  description?: string;
}

const TOAST_STYLES: Record<ToastType, string> = {
  success: "border-emerald-500",
  error: "border-red-500",
  warning: "border-amber-500",
  info: "border-blue-500",
};

interface ToastContextValue {
  toast: (type: ToastType, title: string, description?: string) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) throw new Error("useToast must be used within ToastProvider");
  return context;
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const tc = useTranslations("common");
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const MAX_TOASTS = 5;
  const toast = useCallback(
    (type: ToastType, title: string, description?: string) => {
      const id = crypto.randomUUID();
      setToasts((prev) => [...prev.slice(-(MAX_TOASTS - 1)), { id, type, title, description }]);
    },
    []
  );

  const removeToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const handleEscapeKeyDown = useCallback((event: KeyboardEvent) => {
    if (event.key !== "Escape" || event.defaultPrevented || !(event.target instanceof Element)) return;
    const focusedId = event.target.closest("[data-toast-id]")?.getAttribute("data-toast-id");
    if (!focusedId || !toasts.some((item) => item.id === focusedId)) return;
    // Radix's document capture runs before the focused toast's keydown.
    event.preventDefault();
    removeToast(focusedId);
  }, [removeToast, toasts]);

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      <ToastPrimitive.Provider key={toasts.length === 0 ? "idle" : "active"} swipeDirection="right">
        {toasts.map((t) => (
          <ToastPrimitive.Root
            key={t.id}
            data-toast-id={t.id}
            className={`flex min-w-0 shrink-0 items-start gap-3 rounded-lg border bg-bg-surface p-4 ${TOAST_STYLES[t.type]}`}
            onEscapeKeyDown={handleEscapeKeyDown}
            onOpenChange={(open) => {
              if (!open) removeToast(t.id);
            }}
          >
            <div className="min-w-0 flex-1">
              <ToastPrimitive.Title className="whitespace-normal break-words font-medium text-text-primary">
                {t.title}
              </ToastPrimitive.Title>
              {t.description && (
                <ToastPrimitive.Description className="mt-1 whitespace-normal break-words text-sm text-text-muted">
                  {t.description}
                </ToastPrimitive.Description>
              )}
            </div>
            <ToastPrimitive.Close
              type="button"
              aria-label={tc("close")}
              title={tc("close")}
              className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-text-muted transition-colors hover:bg-bg-elevated hover:text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </ToastPrimitive.Close>
          </ToastPrimitive.Root>
        ))}
        <ToastPrimitive.Viewport className="fixed bottom-4 left-4 right-4 z-50 flex max-h-[calc(100%-2rem)] w-auto max-w-[calc(100vw-2rem)] flex-col gap-2 overflow-y-auto sm:left-auto sm:w-96" />
      </ToastPrimitive.Provider>
    </ToastContext.Provider>
  );
}
