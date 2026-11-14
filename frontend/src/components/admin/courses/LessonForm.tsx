"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { isRichContentEmpty } from "@/lib/rich-content.mjs";
import { useUnsavedChangesGuard } from "@/lib/use-unsaved-changes-guard";

interface Props {
  saving: boolean;
  onSubmit: (title: string, content: string, minutes: number) => void;
  onCancel: () => void;
}

export function LessonForm({ saving, onSubmit, onCancel }: Props) {
  const t = useTranslations("admin");
  const tc = useTranslations("common");

  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [minutes, setMinutes] = useState(10);
  const dirty = Boolean(title.trim()) || !isRichContentEmpty(content) || minutes !== 10;
  const { confirmDiscardOpen, requestDiscard, confirmDiscard, cancelDiscard } = useUnsavedChangesGuard(dirty, { busy: saving });

  const handleCancel = () => {
    if (saving) return;
    requestDiscard(onCancel);
  };

  return (
    <div
      className="space-y-4 rounded-xl border border-accent-primary/30 bg-bg-surface p-[12px] sm:p-5"
      aria-busy={saving}
      inert={saving ? true : undefined}
    >
      <div>
        <label className="mb-1.5 block text-sm font-medium">{t("lessonTitleLabel")}</label>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder={t("lessonTitlePlaceholder")}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
        />
      </div>
      <div>
        <label className="mb-1.5 block text-sm font-medium">{t("lessonContentLabel")}</label>
        <RichTextEditor
          value={content}
          onChange={setContent}
          placeholder={t("lessonContentPlaceholder")}
          enableImageUpload
        />
      </div>
      <div>
        <label className="mb-1.5 block text-sm font-medium">{t("lessonMinutesLabel")}</label>
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="number"
            value={minutes}
            onChange={(e) => setMinutes(parseInt(e.target.value) || 10)}
            className="w-full min-w-0 max-w-32 rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm"
          />
          <span className="text-sm text-text-muted">{t("minuteUnit")}</span>
        </div>
      </div>
      <div className="flex justify-end gap-3">
        <Button variant="ghost" onClick={handleCancel} disabled={saving}>
          {tc("cancel")}
        </Button>
        <Button onClick={() => onSubmit(title, content, minutes)} disabled={saving || !title.trim()}>
          {saving ? tc("processing") : t("addLesson")}
        </Button>
      </div>
      <ConfirmModal
        open={confirmDiscardOpen}
        title={t("lessonUnsavedTitle")}
        description={t("lessonUnsavedDescription")}
        confirmLabel={t("discardLessonChanges")}
        variant="danger"
        onConfirm={confirmDiscard}
        onCancel={cancelDiscard}
      />
    </div>
  );
}
