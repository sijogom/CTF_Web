"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { KnowledgeTagMappingEditor } from "@/components/admin/knowledge/KnowledgeTagMappingEditor";
import type { MappingEditorController } from "@/lib/use-knowledge-tag-mapping-draft";
import { apiFetch, ApiError } from "@/lib/api-client";
import { isRichContentEmpty, legacyRichContentToHtml } from "@/lib/rich-content.mjs";
import { requiresRichContentEditorReview } from "@/lib/rich-content-editor.mjs";
import { useUnsavedChangesGuard } from "@/lib/use-unsaved-changes-guard";
import { useToast } from "@/providers/ToastProvider";

export interface CourseMetadata {
  id: string;
  title: string;
  description: string | null;
  metadata_revision: string;
}

interface CourseMetadataEditorProps {
  course: CourseMetadata;
  archived: boolean;
  lessonAuthoringActive: boolean;
  courseActionActive: boolean;
  mappingController: MappingEditorController;
  onEditingChange: (editing: boolean) => void;
  onSaved: (metadata: CourseMetadata) => void;
}

interface Draft {
  title: string;
  description: string;
}

function editableDescription(value: string | null): string {
  if (!value) return "";
  return requiresRichContentEditorReview(value) ? value : legacyRichContentToHtml(value);
}

function draftFromCourse(course: CourseMetadata): Draft {
  return {
    title: course.title,
    description: editableDescription(course.description),
  };
}

export function CourseMetadataEditor({
  course,
  archived,
  lessonAuthoringActive,
  courseActionActive,
  mappingController,
  onEditingChange,
  onSaved,
}: CourseMetadataEditorProps) {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const { toast } = useToast();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Draft>(() => draftFromCourse(course));
  const [saving, setSaving] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const initialDraftRef = useRef<Draft>(draftFromCourse(course));
  const revisionRef = useRef(course.metadata_revision);
  const requestGenerationRef = useRef(0);
  const titleInputRef = useRef<HTMLInputElement>(null);

  const metadataDirty = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(initialDraftRef.current),
    [draft],
  );
  const dirty = editing && (metadataDirty || mappingController.dirty);
  const busy = saving || reloading || mappingController.busy;
  const { confirmDiscardOpen, requestDiscard, confirmDiscard, cancelDiscard } = useUnsavedChangesGuard(dirty, { busy });

  const setEditingState = useCallback((next: boolean) => {
    setEditing(next);
    onEditingChange(next);
  }, [onEditingChange]);

  useEffect(() => () => {
    requestGenerationRef.current += 1;
    onEditingChange(false);
  }, [onEditingChange]);

  useEffect(() => {
    if (editing) return;
    const next = draftFromCourse(course);
    initialDraftRef.current = next;
    revisionRef.current = course.metadata_revision;
    setDraft(next);
    setError(null);
    setConflict(false);
  }, [course, editing]);

  useEffect(() => {
    if (editing) titleInputRef.current?.focus();
  }, [editing]);

  const beginEditing = () => {
    if (archived || lessonAuthoringActive || courseActionActive || mappingController.saving) return;
    const next = draftFromCourse(course);
    initialDraftRef.current = next;
    revisionRef.current = course.metadata_revision;
    setDraft(next);
    setError(null);
    setConflict(false);
    setEditingState(true);
  };

  const closeEditor = useCallback(() => {
    if (saving || reloading) return;
    const next = draftFromCourse(course);
    initialDraftRef.current = next;
    revisionRef.current = course.metadata_revision;
    setDraft(next);
    setError(null);
    setConflict(false);
    if (mappingController.dirty) mappingController.retry();
    setEditingState(false);
  }, [course, mappingController, reloading, saving, setEditingState]);

  const requestCancel = () => {
    if (saving || reloading) return;
    requestDiscard(closeEditor);
  };

  const loadLatest = useCallback(async () => {
    const generation = ++requestGenerationRef.current;
    const courseId = course.id;
    setReloading(true);
    setError(null);
    try {
      const latest = await apiFetch<CourseMetadata>(`/api/learn/courses/${courseId}`);
      if (generation !== requestGenerationRef.current || latest.id !== courseId) return;
      const next = draftFromCourse(latest);
      initialDraftRef.current = next;
      revisionRef.current = latest.metadata_revision;
      setDraft(next);
      setConflict(false);
      onSaved(latest);
      if (mappingController.dirty) mappingController.retry();
      toast("success", t("courseMetadataLatestLoaded"));
      window.setTimeout(() => titleInputRef.current?.focus(), 0);
    } catch (loadError) {
      if (generation !== requestGenerationRef.current) return;
      setError(loadError instanceof ApiError ? loadError.message : tc("error"));
    } finally {
      if (generation === requestGenerationRef.current) setReloading(false);
    }
  }, [course.id, mappingController, onSaved, t, tc, toast]);

  const requestLatest = () => {
    if (saving || reloading) return;
    requestDiscard(() => { void loadLatest(); });
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving || reloading || !draft.title.trim() || !dirty || !mappingController.payload) return;

    const generation = ++requestGenerationRef.current;
    const courseId = course.id;
    const initial = initialDraftRef.current;
    const payload: {
      expected_metadata_revision: string;
      title?: string;
      description?: string;
      knowledge_tag_mapping?: NonNullable<MappingEditorController["payload"]>;
    } = { expected_metadata_revision: revisionRef.current };
    if (draft.title !== initial.title) payload.title = draft.title;
    if (draft.description !== initial.description) {
      payload.description = isRichContentEmpty(draft.description) ? "" : draft.description;
    }
    if (mappingController.dirty) payload.knowledge_tag_mapping = mappingController.payload;

    setSaving(true);
    setError(null);
    setConflict(false);
    try {
      const saved = await apiFetch<CourseMetadata>(`/api/learn/courses/${courseId}`, {
        method: "PATCH",
        body: payload,
      });
      if (generation !== requestGenerationRef.current || saved.id !== courseId) return;
      const next = draftFromCourse(saved);
      initialDraftRef.current = next;
      revisionRef.current = saved.metadata_revision;
      setDraft(next);
      onSaved(saved);
      if (payload.knowledge_tag_mapping) mappingController.retry();
      setEditingState(false);
      toast("success", t("courseMetadataSaved"));
    } catch (saveError) {
      if (generation !== requestGenerationRef.current) return;
      if (saveError instanceof ApiError && saveError.code === "course_metadata_conflict") {
        setConflict(true);
        setError(t("courseMetadataConflict"));
      } else {
        setError(saveError instanceof ApiError ? saveError.message : tc("error"));
      }
    } finally {
      if (generation === requestGenerationRef.current) setSaving(false);
    }
  };

  return (
    <section className="min-w-0 rounded-xl border border-border bg-bg-surface p-[12px] sm:p-6" aria-labelledby="course-metadata-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id="course-metadata-heading" className="break-words text-lg font-semibold">{t("courseMetadataTitle")}</h2>
          <p className="mt-1 break-words text-sm text-text-muted">{t("courseMetadataHelp")}</p>
        </div>
        {!editing && (
          <Button type="button" size="sm" onClick={beginEditing} disabled={archived || lessonAuthoringActive || courseActionActive || mappingController.saving}>
            {t("courseMetadataEdit")}
          </Button>
        )}
      </div>

      {archived && (
        <p className="mt-4 rounded-lg border border-accent-warning/30 bg-accent-warning/10 px-3 py-2 text-sm text-text-secondary">
          {t("courseMetadataArchivedReadOnly")}
        </p>
      )}
      {!archived && lessonAuthoringActive && !editing && (
        <p className="mt-4 rounded-lg border border-accent-warning/30 bg-accent-warning/10 px-3 py-2 text-sm text-text-secondary">
          {t("courseMetadataLessonBusy")}
        </p>
      )}
      {!archived && !lessonAuthoringActive && courseActionActive && !editing && (
        <p className="mt-4 rounded-lg border border-accent-warning/30 bg-accent-warning/10 px-3 py-2 text-sm text-text-secondary">
          {t("courseMetadataActionBusy")}
        </p>
      )}

      {editing && (
        <form className="mt-5 min-w-0 space-y-5" onSubmit={handleSubmit} aria-busy={busy} noValidate>
          <p className="rounded-lg border border-accent-primary/20 bg-accent-primary/5 px-3 py-2 text-sm text-text-secondary">
            {t("courseMetadataEditingHelp")}
          </p>
          {error && (
            <div role="alert" className="rounded-lg border border-accent-error/30 bg-accent-error/10 px-3 py-2 text-sm text-accent-error">
              <p>{error}</p>
              {conflict && (
                <Button type="button" className="mt-3" variant="secondary" size="sm" onClick={requestLatest} disabled={saving || reloading}>
                  {reloading ? tc("processing") : t("courseMetadataLoadLatest")}
                </Button>
              )}
            </div>
          )}

          <div>
            <label htmlFor="course-metadata-title" className="mb-1.5 block text-sm font-medium">
              {t("courseTitleLabel")} <span className="text-accent-error">*</span>
            </label>
            <input
              ref={titleInputRef}
              id="course-metadata-title"
              required
              maxLength={200}
              disabled={busy}
              value={draft.title}
              onChange={(event) => setDraft((current) => ({ ...current, title: event.target.value }))}
              className="w-full min-w-0 rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm focus:border-accent-primary focus:outline-none"
            />
          </div>
          <div className="min-w-0">
            <div id="course-metadata-description-label" className="mb-1.5 text-sm font-medium">{t("courseDescLabel")}</div>
            <div
              aria-labelledby="course-metadata-description-label"
              aria-disabled={busy}
              inert={busy ? true : undefined}
              className="min-w-0"
            >
              <RichTextEditor
                value={draft.description}
                onChange={(description) => setDraft((current) => ({ ...current, description }))}
                placeholder={t("courseDescPlaceholder")}
                ariaLabel={t("courseDescLabel")}
              />
            </div>
          </div>
          <KnowledgeTagMappingEditor entityType="course" entityId={course.id} controller={mappingController} disabled={busy} />
          <div className="flex flex-wrap justify-end gap-2">
            <Button type="button" variant="ghost" onClick={requestCancel} disabled={saving || reloading}>
              {tc("cancel")}
            </Button>
            <Button type="submit" disabled={saving || reloading || !draft.title.trim() || !dirty || !mappingController.payload}>
              {saving ? tc("processing") : tc("save")}
            </Button>
          </div>
        </form>
      )}

      <ConfirmModal
        open={confirmDiscardOpen}
        title={t("courseMetadataUnsavedTitle")}
        description={t("courseMetadataUnsavedDescription")}
        confirmLabel={t("discardCourseMetadataChanges")}
        variant="danger"
        onConfirm={confirmDiscard}
        onCancel={cancelDiscard}
      />
    </section>
  );
}
