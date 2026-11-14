"use client";

import { useEffect, useId, useRef, type RefObject } from "react";
import { AlertTriangle } from "lucide-react";
import { useTranslations } from "next-intl";
import type { TagDeletionPreview } from "@/lib/knowledge-tag-deletion.mjs";

interface Props {
  name: string;
  preview: TagDeletionPreview | null;
  loading: boolean;
  deleting: boolean;
  error: string | null;
  onRetry: () => void;
  onConfirm: () => void;
  onCancel: () => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
}

export function KnowledgeTagDeleteDialog({ name, preview, loading, deleting, error, onRetry, onConfirm, onCancel, returnFocusRef }: Props) {
  const t = useTranslations("knowledgeAdmin.tagDeletion");
  const common = useTranslations("common");
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const previous = document.activeElement;
    const element = dialog.current;
    const returnTarget = returnFocusRef;
    element?.showModal();
    return () => {
      element?.close();
      const target = returnTarget?.current ?? previous;
      if (target instanceof HTMLElement && target.isConnected) target.focus();
    };
  }, [returnFocusRef]);

  return (
    <dialog ref={dialog} aria-labelledby={titleId}
      onCancel={(event) => { event.preventDefault(); if (!deleting) onCancel(); }}
      className="fixed inset-0 m-auto max-h-[calc(100dvh-24px)] w-[calc(100vw-24px)] max-w-lg overflow-y-auto rounded-lg border border-border bg-bg-surface p-5 text-text-primary backdrop:bg-black/40">
      <h2 id={titleId} className="flex items-center gap-2 text-lg font-semibold"><AlertTriangle className="h-5 w-5 shrink-0 text-accent-error" aria-hidden />{t("title")}</h2>
      <p className="mt-3 break-words text-sm">{t("description", { name })}</p>
      {loading && <p className="mt-3 text-sm text-text-secondary" role="status">{t("loading")}</p>}
      {preview && <div className="mt-3 space-y-2 text-sm">
        <p>{t("impacts", preview.counts)}</p>
        {preview.counts.children > 0 && <p>{t("children", { count: preview.counts.children })}</p>}
        {(preview.counts.relations > 0 || preview.counts.proposals > 0) && <p>{t("relations", { count: preview.counts.relations, proposals: preview.counts.proposals })}</p>}
        <p className="text-text-secondary">{t("history")}</p>
      </div>}
      {error && <p role="alert" className="mt-3 break-words text-sm text-accent-error">{error}</p>}
      <div className="mt-5 flex flex-wrap justify-end gap-2">
        {error && <button type="button" onClick={onRetry} disabled={loading || deleting} className="rounded-lg border border-border px-3 py-2 text-sm disabled:opacity-50">{t("retry")}</button>}
        <button type="button" autoFocus onClick={onCancel} disabled={deleting} className="rounded-lg border border-border px-3 py-2 text-sm disabled:opacity-50">{common("cancel")}</button>
        <button type="button" onClick={onConfirm} disabled={!preview || loading || deleting} className="rounded-lg bg-accent-error px-3 py-2 text-sm font-medium text-white disabled:opacity-50">{t(deleting ? "deleting" : "confirm")}</button>
      </div>
    </dialog>
  );
}
