"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { apiFetch, apiFormFetch, ApiError } from "@/lib/api-client";
import { isRichContentEmpty, legacyRichContentToHtml } from "@/lib/rich-content.mjs";
import { requiresRichContentEditorReview } from "@/lib/rich-content-editor.mjs";
import { createLessonEditSnapshot, isLessonEditDirty } from "@/lib/lesson-edit-state.mjs";
import { useUnsavedChangesGuard } from "@/lib/use-unsaved-changes-guard";
import { useKnowledgeTagMappingDraft } from "@/lib/use-knowledge-tag-mapping-draft";
import { useToast } from "@/providers/ToastProvider";
import type { Lesson, AttachmentInfo } from "@/types/course";
import { LessonForm } from "./LessonForm";
import { LessonList } from "./LessonList";

interface Props {
  courseId: string;
  lessons: Lesson[];
  onRefresh: () => void;
  onAuthoringStateChange?: (active: boolean) => void;
}

export function CourseLessonManager({ courseId, lessons, onRefresh, onAuthoringStateChange }: Props) {
  const t = useTranslations("admin");
  const tl = useTranslations("learn");
  const tc = useTranslations("common");
  const { toast } = useToast();

  const [showLessonForm, setShowLessonForm] = useState(false);
  const [lessonSaving, setLessonSaving] = useState(false);
  const [attachUploading, setAttachUploading] = useState<string | null>(null);
  const [deleteLessonId, setDeleteLessonId] = useState<string | null>(null);
  const [removeAttachmentInfo, setRemoveAttachmentInfo] = useState<{ lessonId: string; filename: string } | null>(null);

  const [editingLessonId, setEditingLessonId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editContent, setEditContent] = useState("");
  const [editMinutes, setEditMinutes] = useState(10);
  const [editSaving, setEditSaving] = useState(false);
  const [editAttachments, setEditAttachments] = useState<AttachmentInfo[]>([]);
  const [editLoadingLessonId, setEditLoadingLessonId] = useState<string | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const editRequestGenerationRef = useRef(0);
  const editingLessonIdRef = useRef<string | null>(null);
  const initialEditSnapshotRef = useRef<string | null>(null);
  const pendingParentRefreshRef = useRef(false);
  const mappingController = useKnowledgeTagMappingDraft("lesson", editingLessonId);

  const editDraft = useMemo(() => ({
    title: editTitle,
    content: editContent,
    minutes: editMinutes,
    tagIds: [],
  }), [editContent, editMinutes, editTitle]);
  const editDirty = isLessonEditDirty(initialEditSnapshotRef.current, editDraft) || mappingController.dirty;
  const authoringActive = showLessonForm || editingLessonId !== null || editLoadingLessonId !== null;
  const {
    confirmDiscardOpen,
    requestDiscard,
    confirmDiscard,
    cancelDiscard,
  } = useUnsavedChangesGuard(editDirty, {
    busy: editSaving || attachUploading !== null || mappingController.busy,
  });

  useEffect(() => {
    onAuthoringStateChange?.(authoringActive);
    return () => onAuthoringStateChange?.(false);
  }, [authoringActive, onAuthoringStateChange]);

  useEffect(() => {
    editingLessonIdRef.current = editingLessonId;
  }, [editingLessonId]);

  const refreshParent = useCallback(() => {
    pendingParentRefreshRef.current = false;
    onRefresh();
  }, [onRefresh]);

  const handleAddLesson = async (title: string, content: string, minutes: number) => {
    if (!title.trim()) return;
    setLessonSaving(true);
    try {
      await apiFetch(`/api/learn/courses/${courseId}/lessons`, {
        method: "POST",
        body: {
          title,
          content: isRichContentEmpty(content) ? null : content,
          order_index: lessons.length,
          estimated_minutes: minutes,
        },
      });
      setShowLessonForm(false);
      refreshParent();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setLessonSaving(false);
    }
  };

  const handleDeleteLesson = async () => {
    if (!deleteLessonId) return;
    const lessonId = deleteLessonId;
    setDeleteLessonId(null);
    try {
      await apiFetch(`/api/learn/courses/${courseId}/lessons/${lessonId}`, { method: "DELETE" });
      refreshParent();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    }
  };

  const clearEditingLesson = useCallback(() => {
    editRequestGenerationRef.current += 1;
    editingLessonIdRef.current = null;
    initialEditSnapshotRef.current = null;
    setEditingLessonId(null);
    setEditLoadingLessonId(null);
    setEditError(null);
  }, []);

  const loadLessonForEdit = useCallback(async (lesson: Lesson) => {
    const generation = ++editRequestGenerationRef.current;
    setEditLoadingLessonId(lesson.id);
    setEditError(null);
    try {
      const res = await apiFetch<{ lesson: { id: string; title: string; content: string | null; estimated_minutes: number; attachments: AttachmentInfo[] }; siblings: unknown }>(
          `/api/learn/courses/${courseId}/lessons/${lesson.id}`,
        );
      if (generation !== editRequestGenerationRef.current) return;
      const detail = res.lesson;
      const persistedContent = detail.content || "";
      const content = requiresRichContentEditorReview(persistedContent)
        ? persistedContent
        : legacyRichContentToHtml(persistedContent);
      const draft = {
        title: detail.title,
        content,
        minutes: detail.estimated_minutes,
        tagIds: [],
      };
      initialEditSnapshotRef.current = createLessonEditSnapshot(draft);
      editingLessonIdRef.current = lesson.id;
      setEditingLessonId(lesson.id);
      setEditTitle(draft.title);
      setEditContent(draft.content);
      setEditMinutes(draft.minutes);
      setEditAttachments(detail.attachments ?? []);
    } catch (err) {
      if (generation !== editRequestGenerationRef.current) return;
      const message = err instanceof ApiError ? err.message : tc("error");
      setEditError(message);
      toast("error", message);
    } finally {
      if (generation === editRequestGenerationRef.current) setEditLoadingLessonId(null);
    }
  }, [courseId, tc, toast]);

  const startEditLesson = (lesson: Lesson) => {
    if (showLessonForm || editSaving || attachUploading !== null || lesson.id === editingLessonIdRef.current) return;
    requestDiscard(() => {
      clearEditingLesson();
      void loadLessonForEdit(lesson);
    });
  };

  const requestCancelEditing = () => {
    if (editSaving || attachUploading !== null) return;
    requestDiscard(() => {
      clearEditingLesson();
      if (pendingParentRefreshRef.current) refreshParent();
    });
  };

  const handleUpdateLesson = async () => {
    if (!editingLessonId || !editTitle.trim() || editSaving || attachUploading !== null || !mappingController.payload) return;
    const lessonId = editingLessonId;
    setEditSaving(true);
    setEditError(null);
    try {
      await apiFetch(`/api/learn/courses/${courseId}/lessons/${lessonId}`, {
        method: "PUT",
        body: {
          title: editTitle,
          content: isRichContentEmpty(editContent) ? null : editContent,
          estimated_minutes: editMinutes,
          ...(mappingController.dirty ? { knowledge_tag_mapping: mappingController.payload } : {}),
        },
      });
      if (editingLessonIdRef.current === lessonId) clearEditingLesson();
      refreshParent();
    } catch (err) {
      const message = err instanceof ApiError ? err.message : tc("error");
      setEditError(message);
      toast("error", message);
    } finally {
      setEditSaving(false);
    }
  };

  const handleAddAttachment = async (lessonId: string, file: File) => {
    setAttachUploading(lessonId);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const result = await apiFormFetch<AttachmentInfo>(
        `/api/learn/courses/${courseId}/lessons/${lessonId}/attachments`,
        formData,
      );
      // 편집 중일 때는 로컬 첨부 목록만 업데이트 (편집 내용 보존)
      if (editingLessonIdRef.current === lessonId) {
        setEditAttachments((prev) => [...prev, result]);
      } else if (editingLessonIdRef.current) {
        // The upload is already persisted. Defer the parent list refresh until
        // the active lesson draft is saved/cancelled so it cannot be unmounted.
        pendingParentRefreshRef.current = true;
      } else {
        refreshParent();
      }
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setAttachUploading((current) => current === lessonId ? null : current);
    }
  };

  const handleRemoveAttachment = async () => {
    if (!removeAttachmentInfo) return;
    const { lessonId, filename } = removeAttachmentInfo;
    setRemoveAttachmentInfo(null);
    try {
      await apiFetch(
        `/api/learn/courses/${courseId}/lessons/${lessonId}/attachments/${filename}`,
        { method: "DELETE" },
      );
      // 편집 중일 때는 로컬 첨부 목록만 업데이트 (편집 내용 보존)
      if (editingLessonIdRef.current === lessonId) {
        setEditAttachments((prev) => prev.filter((a) => a.filename !== filename));
      } else if (editingLessonIdRef.current) {
        pendingParentRefreshRef.current = true;
      } else {
        refreshParent();
      }
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">{t("lessonManagement")}</h2>
        <Button size="sm" onClick={() => setShowLessonForm(true)} disabled={showLessonForm || editingLessonId !== null || editLoadingLessonId !== null}>
          {t("addLesson")}
        </Button>
      </div>

      {showLessonForm && (
        <LessonForm
          saving={lessonSaving}
          onSubmit={handleAddLesson}
          onCancel={() => setShowLessonForm(false)}
        />
      )}

      <LessonList
        lessons={lessons}
        editingLessonId={editingLessonId}
        loadingLessonId={editLoadingLessonId}
        editDisabled={showLessonForm}
        deleteDisabled={showLessonForm || editingLessonId !== null || editLoadingLessonId !== null}
        editFormProps={editingLessonId ? {
          editTitle,
          editContent,
          editMinutes,
          mappingController,
          editSaving: editSaving || attachUploading === editingLessonId,
          editLoading: editLoadingLessonId !== null,
          editError,
          attachments: editAttachments,
          attachUploading: attachUploading === editingLessonId,
          onTitleChange: setEditTitle,
          onContentChange: setEditContent,
          onMinutesChange: setEditMinutes,
          onSave: handleUpdateLesson,
          onCancel: requestCancelEditing,
          onAddAttachment: (file: File) => handleAddAttachment(editingLessonId, file),
          onRemoveAttachment: (filename: string) => {
            if (editingLessonId) setRemoveAttachmentInfo({ lessonId: editingLessonId, filename });
          },
        } : undefined}
        onEdit={startEditLesson}
        onDelete={setDeleteLessonId}
      />

      <ConfirmModal
        open={!!deleteLessonId}
        title={tc("delete")}
        description={tc("confirm")}
        confirmLabel={tc("delete")}
        variant="danger"
        onConfirm={handleDeleteLesson}
        onCancel={() => setDeleteLessonId(null)}
      />

      <ConfirmModal
        open={!!removeAttachmentInfo}
        title={tl("removeAttachment")}
        description={tc("confirm")}
        confirmLabel={tc("delete")}
        variant="danger"
        onConfirm={handleRemoveAttachment}
        onCancel={() => setRemoveAttachmentInfo(null)}
      />

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
