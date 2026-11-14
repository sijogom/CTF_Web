"use client";

import { useCallback, useEffect, useId, useRef } from "react";
import { useTranslations } from "next-intl";
import { Button } from "./Button";

interface ConfirmModalProps {
  open: boolean;
  title: string;
  description: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: "danger" | "default";
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmModal({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel,
  variant = "default",
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  const tc = useTranslations("common");
  const resolvedConfirm = confirmLabel ?? tc("confirm");
  const resolvedCancel = cancelLabel ?? tc("cancel");
  const dialogRef = useRef<HTMLDialogElement>(null);
  const uid = useId();
  const titleId = `${uid}-title`;
  const descId = `${uid}-desc`;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  }, [open]);

  // focus trap: Tab 키를 모달 내부에서 순환
  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    if (!dialog) return;

    const focusable = dialog.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    );
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    first?.focus();

    const handleTab = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last?.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first?.focus();
      }
    };

    dialog.addEventListener("keydown", handleTab);
    return () => dialog.removeEventListener("keydown", handleTab);
  }, [open]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    },
    [onCancel]
  );

  if (!open) return null;

  return (
    <dialog
      ref={dialogRef}
      onKeyDown={handleKeyDown}
      aria-labelledby={titleId}
      aria-describedby={descId}
      className="fixed inset-0 z-[100] m-auto w-[calc(100vw-24px)] max-w-md rounded-xl border border-border bg-bg-surface p-0 shadow-2xl backdrop:bg-black/50"
    >
      <div className="p-[12px] sm:p-6">
        <h3 id={titleId} className="break-words text-lg font-semibold text-text-primary">{title}</h3>
        <p id={descId} className="mt-2 break-words text-sm text-text-secondary">{description}</p>
      </div>
      <div className="flex flex-wrap justify-end gap-2 border-t border-border p-[12px] sm:px-6 sm:py-4">
        <Button variant="secondary" onClick={onCancel}>
          {resolvedCancel}
        </Button>
        <Button
          variant="primary"
          onClick={onConfirm}
          className={variant === "danger" ? "bg-accent-error hover:bg-accent-error/80 border-accent-error" : ""}
        >
          {resolvedConfirm}
        </Button>
      </div>
    </dialog>
  );
}
