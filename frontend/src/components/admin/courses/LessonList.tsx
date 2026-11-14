"use client";

import { useTranslations } from "next-intl";
import type { Lesson, AttachmentInfo } from "@/types/course";
import { LessonEditForm } from "./LessonEditForm";
import type { MappingEditorController } from "@/lib/use-knowledge-tag-mapping-draft";

interface EditFormProps {
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

interface Props {
  lessons: Lesson[];
  editingLessonId: string | null;
  loadingLessonId: string | null;
  editDisabled?: boolean;
  deleteDisabled?: boolean;
  editFormProps?: EditFormProps;
  onEdit: (lesson: Lesson) => void;
  onDelete: (lessonId: string) => void;
}

export function LessonList({ lessons, editingLessonId, loadingLessonId, editDisabled = false, deleteDisabled = false, editFormProps, onEdit, onDelete }: Props) {
  const t = useTranslations("admin");
  const tc = useTranslations("common");

  const sortedLessons = [...lessons].sort((a, b) => a.order_index - b.order_index);

  if (sortedLessons.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-border p-8 text-center text-sm text-text-muted">
        {t("noLessons")}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {sortedLessons.map((lesson, i) => {
        if (lesson.id === editingLessonId && editFormProps) {
          return (
            <LessonEditForm key={lesson.id} lessonId={lesson.id} {...editFormProps} />
          );
        }

        return (
          <div
            key={lesson.id}
            className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-bg-surface px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
          >
            <div className="flex min-w-0 items-center gap-3">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-bg-primary text-xs font-medium text-text-muted">
                {i + 1}
              </span>
              <div className="min-w-0">
                <span className="break-words text-sm font-medium [overflow-wrap:anywhere]">{lesson.title}</span>
                <span className="ml-2 text-xs text-text-muted">{t("minuteDisplay", { n: lesson.estimated_minutes })}</span>
                {lesson.attachments && lesson.attachments.length > 0 && (
                  <span className="ml-2 text-xs text-text-muted">
                    {t("attachmentCount", { count: lesson.attachments.length })}
                  </span>
                )}
              </div>
            </div>
            <div className="flex shrink-0 flex-wrap gap-2 self-end sm:self-auto">
              <button
                onClick={() => onEdit(lesson)}
                disabled={editDisabled || loadingLessonId === lesson.id}
                className="rounded-lg px-2 py-1 text-xs text-accent-primary hover:bg-accent-primary/10"
              >
                {loadingLessonId === lesson.id ? t("lessonLoading") : tc("edit")}
              </button>
              <button
                onClick={() => onDelete(lesson.id)}
                disabled={deleteDisabled}
                className="rounded-lg px-2 py-1 text-xs text-accent-error hover:bg-accent-error/10 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {tc("delete")}
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
