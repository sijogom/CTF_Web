"use client";

import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { KnowledgeTagMappingEditor } from "@/components/admin/knowledge/KnowledgeTagMappingEditor";
import { formatFileSize } from "@/lib/utils";
import type { AttachmentInfo } from "@/types/course";
import type { MappingEditorController } from "@/lib/use-knowledge-tag-mapping-draft";

interface Props {
  lessonId: string;
  editTitle: string;
  editContent: string;
  editMinutes: number;
  mappingController: MappingEditorController;
  editSaving: boolean;
  editLoading: boolean;
  editError: string | null;
  attachments: AttachmentInfo[];
  attachUploading: boolean;
  onTitleChange: (v: string) => void;
  onContentChange: (v: string) => void;
  onMinutesChange: (v: number) => void;
  onSave: () => void;
  onCancel: () => void;
  onAddAttachment: (file: File) => void;
  onRemoveAttachment: (filename: string) => void;
}

export function LessonEditForm({
  lessonId,
  editTitle,
  editContent,
  editMinutes,
  mappingController,
  editSaving,
  editLoading,
  editError,
  attachments,
  attachUploading,
  onTitleChange,
  onContentChange,
  onMinutesChange,
  onSave,
  onCancel,
  onAddAttachment,
  onRemoveAttachment,
}: Props) {
  const t = useTranslations("admin");
  const tl = useTranslations("learn");
  const tc = useTranslations("common");

  return (
    <div className="space-y-4 rounded-xl border border-accent-warning/30 bg-bg-surface p-[12px] sm:p-5" aria-busy={editLoading || editSaving}>
      {editLoading && <p className="text-sm text-text-muted" role="status">{t("lessonLoading")}</p>}
      {editError && <p className="rounded-lg bg-accent-error/10 px-3 py-2 text-sm text-accent-error" role="alert">{editError}</p>}
      <div
        inert={editLoading || editSaving ? true : undefined}
        className={`space-y-4 ${editLoading || editSaving ? "pointer-events-none opacity-60" : ""}`}
      >
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">{tc("edit")}</h3>
        <button type="button" onClick={onCancel} className="text-sm text-text-muted hover:text-text-primary">
          {tc("cancel")}
        </button>
      </div>
      <div>
        <label className="mb-1.5 block text-sm font-medium">{t("lessonTitleLabel")}</label>
        <input
          value={editTitle}
          onChange={(e) => onTitleChange(e.target.value)}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
        />
      </div>
      <div>
        <label className="mb-1.5 block text-sm font-medium">{t("lessonContentLabel")}</label>
        <RichTextEditor
          value={editContent}
          onChange={onContentChange}
          enableImageUpload
        />
      </div>
      <div>
        <label className="mb-1.5 block text-sm font-medium">{t("lessonMinutesLabel")}</label>
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="number"
            value={editMinutes}
            onChange={(e) => onMinutesChange(parseInt(e.target.value) || 10)}
            className="w-full min-w-0 max-w-32 rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm"
          />
          <span className="text-sm text-text-muted">{t("minuteUnit")}</span>
        </div>
      </div>

      <KnowledgeTagMappingEditor
        entityType="lesson"
        entityId={lessonId}
        controller={mappingController}
        disabled={editSaving}
      />

      <div>
        <label className="mb-1.5 block text-sm font-medium">{tl("attachments")}</label>
        {attachments.map((att) => (
          <div key={att.filename} className="mb-1 flex min-w-0 flex-col items-stretch gap-2 rounded-lg border border-border bg-bg-primary px-3 py-2 sm:flex-row sm:items-center">
            <div className="grid min-w-0 flex-1 grid-cols-[auto_minmax(0,1fr)] items-center gap-x-2 gap-y-1 text-sm">
              <svg className="h-4 w-4 shrink-0 text-text-muted" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
              </svg>
              <span className="min-w-0 break-all">{att.original_name}</span>
              <span className="col-start-2 text-text-muted">({formatFileSize(att.size)})</span>
            </div>
            <button
              onClick={() => onRemoveAttachment(att.filename)}
              className="self-start text-xs text-accent-error hover:underline sm:self-auto"
            >
              {tl("removeAttachment")}
            </button>
          </div>
        ))}
        <label className="mt-2 inline-flex max-w-full cursor-pointer flex-wrap items-center gap-1.5 rounded-lg border border-dashed border-border px-3 py-2 text-sm text-text-muted hover:border-accent-primary hover:text-accent-primary">
          {attachUploading ? (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
          ) : (
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
          )}
          {tl("addAttachment")}
          <input
            type="file"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) onAddAttachment(file);
              e.target.value = "";
            }}
          />
        </label>
        <p className="mt-2 text-xs text-text-muted">{t("lessonAttachmentsImmediate")}</p>
      </div>

      <div className="flex justify-end">
        <Button onClick={onSave} disabled={editLoading || editSaving || mappingController.draft?.status !== "ready" || !(editTitle ?? "").trim()}>
          {editSaving ? tc("processing") : tc("save")}
        </Button>
      </div>
      </div>
    </div>
  );
}
