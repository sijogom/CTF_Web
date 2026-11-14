"use client";

import { useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ArrowDown, ArrowUp, File, FolderUp, LoaderCircle, RefreshCw, RotateCcw, Trash2 } from "lucide-react";
import type { AttachmentController, AttachmentSnapshot } from "@/lib/challenge-attachments.mjs";
import type { ChallengeAttachmentDescriptor } from "@/types/challenge-attachment";

interface Props {
  attachments: AttachmentSnapshot & { controller: AttachmentController };
  legacy: ChallengeAttachmentDescriptor[];
  disabled: boolean;
  onRemoveLegacy(index: number): void;
  onReload(): Promise<void>;
}

export function StepFiles({ attachments, legacy, disabled, onRemoveLegacy, onReload }: Props) {
  const t = useTranslations("challenge"), tc = useTranslations("common");
  const input = useRef<HTMLInputElement>(null), [dragging, setDragging] = useState(false);
  const { controller, items, error, conflict } = attachments;
  const errorKey = error === "name" ? "attachmentNameError" : error === "size" ? "attachmentSizeError" : error === "restore" ? "attachmentRestoreError"
    : error === "conflict" ? "attachmentConflict" : error === "pending" ? "attachmentPendingSave" : error === "fileUnavailable" ? "attachmentFileUnavailable" : "attachmentFailed";
  const iconButton = "inline-flex h-10 w-10 shrink-0 items-center justify-center rounded border border-border hover:bg-bg-primary focus-visible:outline-2 focus-visible:outline-accent-primary disabled:opacity-40";
  const status = (state: string, ready: boolean) => ready && state === "ready" ? t("attachmentReady") : state === "uploading" || state === "queued" ? t("attachmentUploading") : state === "missing" ? t("attachmentFailed") : t("attachmentPending");
  return <div className="space-y-5">
    <h2 className="text-lg font-semibold">{t("attachmentFiles")}</h2>
    <label role="button" tabIndex={disabled ? -1 : 0} aria-disabled={disabled} aria-label={t("attachmentFiles")}
      className={`flex min-h-32 cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-6 focus-visible:outline-2 focus-visible:outline-accent-primary ${dragging ? "border-accent-primary bg-accent-primary/10" : "border-border bg-bg-primary"} ${disabled ? "pointer-events-none opacity-50" : ""}`}
      onKeyDown={event => { if (!disabled && ["Enter", " "].includes(event.key)) { event.preventDefault(); input.current?.click(); } }}
      onDragOver={event => event.preventDefault()} onDragEnter={event => { event.preventDefault(); if (!disabled) setDragging(true); }}
      onDragLeave={event => { event.preventDefault(); setDragging(false); }}
      onDrop={event => { event.preventDefault(); setDragging(false); if (!disabled) void controller.add(Array.from(event.dataTransfer.files)); }}>
      <FolderUp size={28} aria-hidden="true" /><span className="text-sm text-text-secondary">{t("dragHere")}</span>
      <input ref={input} type="file" multiple disabled={disabled} className="sr-only" tabIndex={-1} onChange={event => {
        const selected = Array.from(event.currentTarget.files ?? []); event.currentTarget.value = "";
        if (!disabled && selected.length) void controller.add(selected);
      }} />
    </label>
    {error && <div role="alert" className="flex flex-wrap items-center justify-between gap-2 text-sm text-accent-error"><span>{t(errorKey)}</span>
      {conflict && <button type="button" disabled={disabled} onClick={() => void onReload()} className="inline-flex items-center gap-2 underline"><RefreshCw size={16} />{t("attachmentReload")}</button>}
    </div>}
    <ul className="space-y-2">
      {legacy.map(item => <li key={`legacy-${item.legacy_index}`} className="flex min-w-0 items-center gap-3 border-b border-border py-3">
        <File size={18} className="shrink-0" aria-hidden="true" /><div className="min-w-0 flex-1"><p className="break-all text-sm font-medium">{item.name}</p>
          <p className="text-xs text-text-secondary">{item.state === "ready" ? t("attachmentReady") : t("attachmentUnavailable")}</p></div>
        <button type="button" disabled={disabled || item.legacy_index === null} className={iconButton} title={tc("delete")} aria-label={`${tc("delete")}: ${item.name}`} onClick={() => item.legacy_index !== null && onRemoveLegacy(item.legacy_index)}><Trash2 size={17} /></button>
      </li>)}
      {items.map((item, index) => <li key={item.id} className="flex min-w-0 flex-wrap items-center gap-2 border-b border-border py-3">
        {item.state === "uploading" ? <LoaderCircle size={18} className="shrink-0 animate-spin" aria-hidden="true" /> : <File size={18} className="shrink-0" aria-hidden="true" />}
        <div className="min-w-0 flex-1 basis-32"><p className="break-all text-sm font-medium">{item.name}</p><p role="status" className="text-xs text-text-secondary">
          {item.existing && item.state === "unavailable" ? t("attachmentUnavailable") : item.error ? t("attachmentFailed") : status(item.state, item.ready_for_attach)}{item.byte_size !== null ? ` · ${item.byte_size.toLocaleString()} B` : ""}</p></div>
        <div className="flex flex-wrap gap-1">
          {!item.existing && <button type="button" className={iconButton} disabled={disabled} title={t("attachmentCheckStatus")} aria-label={`${t("attachmentCheckStatus")}: ${item.name}`} onClick={() => void controller.check(item.id)}><RefreshCw size={17} /></button>}
          {!item.existing && item.reconcile_required && <button type="button" className={iconButton} disabled={disabled} title={t("attachmentReconcile")} aria-label={`${t("attachmentReconcile")}: ${item.name}`} onClick={() => void controller.check(item.id, true)}><RotateCcw size={17} /></button>}
          {item.canRetry && <button type="button" className={iconButton} disabled={disabled} title={tc("retry")} aria-label={`${tc("retry")}: ${item.name}`} onClick={() => void controller.retry(item.id)}><RotateCcw size={17} /></button>}
          <button type="button" className={iconButton} disabled={disabled || index === 0} title={t("attachmentMoveUp")} aria-label={`${t("attachmentMoveUp")}: ${item.name}`} onClick={() => void controller.move(item.id, -1)}><ArrowUp size={17} /></button>
          <button type="button" className={iconButton} disabled={disabled || index === items.length - 1} title={t("attachmentMoveDown")} aria-label={`${t("attachmentMoveDown")}: ${item.name}`} onClick={() => void controller.move(item.id, 1)}><ArrowDown size={17} /></button>
          <button type="button" className={iconButton} disabled={disabled} title={tc("delete")} aria-label={`${tc("delete")}: ${item.name}`} onClick={() => void controller.remove(item.id)}><Trash2 size={17} /></button>
        </div>
      </li>)}
    </ul>
  </div>;
}
