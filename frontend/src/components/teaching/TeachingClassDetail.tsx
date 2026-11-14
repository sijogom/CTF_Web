"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { ApiError, apiFetch } from "@/lib/api-client";
import { AssignmentForm } from "@/components/teaching/AssignmentForm";
import { ClassForm } from "@/components/teaching/ClassForm";
import { EducationAssetPanel } from "@/components/teaching/EducationAssetPanel";
import { EducationResourceManager } from "@/components/teaching/EducationResourceManager";
import { EnrollmentManager } from "@/components/teaching/EnrollmentManager";
import {
  EDUCATION_API,
  getAllClassAssignments,
  getEducationClass,
} from "@/components/teaching/education-api";
import { formatEducationDate } from "@/components/teaching/education-format";
import type {
  EducationAssignment,
  EducationAssignmentSummary,
  EducationAssignmentWritePayload,
  EducationClassDetail as EducationClassDetailType,
  EducationClassWritePayload,
} from "@/types/education";

const STATUS_CLASS: Record<string, string> = {
  draft: "bg-bg-elevated text-text-secondary",
  published: "bg-accent-success/10 text-accent-success",
  archived: "bg-accent-warning/10 text-accent-warning",
};

export function TeachingClassDetail({ classId }: { classId: string }) {
  const t = useTranslations("education");
  const tc = useTranslations("common");
  const locale = useLocale();
  const [classItem, setClassItem] = useState<EducationClassDetailType | null>(null);
  const [assignments, setAssignments] = useState<EducationAssignmentSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showEdit, setShowEdit] = useState(false);
  const [showNewAssignment, setShowNewAssignment] = useState(false);
  const [mutatingId, setMutatingId] = useState<string | null>(null);
  const [archiveTarget, setArchiveTarget] = useState<EducationAssignmentSummary | null>(null);
  const [archiveReason, setArchiveReason] = useState("");
  const [restoringClass, setRestoringClass] = useState(false);
  const classRequestGenerationRef = useRef(0);

  const loadClass = useCallback(async () => {
    const generation = ++classRequestGenerationRef.current;
    setLoading(true);
    setError(null);
    try {
      const [detail, assignmentResponse] = await Promise.all([
        getEducationClass(classId),
        getAllClassAssignments(classId),
      ]);
      if (generation !== classRequestGenerationRef.current) return;
      setClassItem(detail);
      setAssignments(assignmentResponse);
    } catch (requestError) {
      if (generation !== classRequestGenerationRef.current) return;
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      if (generation === classRequestGenerationRef.current) setLoading(false);
    }
  }, [classId, tc]);

  useEffect(() => {
    void loadClass();
    return () => {
      classRequestGenerationRef.current += 1;
    };
  }, [loadClass]);

  const updateClass = async (payload: EducationClassWritePayload) => {
    await apiFetch<EducationClassDetailType>(`${EDUCATION_API}/classes/${classId}`, {
      method: "PATCH",
      body: payload,
    });
    setShowEdit(false);
    await loadClass();
  };

  const restoreClass = async () => {
    setRestoringClass(true);
    setError(null);
    try {
      await apiFetch<EducationClassDetailType>(`${EDUCATION_API}/classes/${classId}/restore`, {
        method: "POST",
        body: { status: "draft" },
      });
      await loadClass();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setRestoringClass(false);
    }
  };

  const createAssignment = async (payload: EducationAssignmentWritePayload) => {
    await apiFetch<EducationAssignment>(`${EDUCATION_API}/classes/${classId}/assignments`, {
      method: "POST",
      body: payload,
    });
    setShowNewAssignment(false);
    await loadClass();
  };

  const publishAssignment = async (assignment: EducationAssignmentSummary) => {
    setMutatingId(assignment.id);
    setError(null);
    try {
      await apiFetch<EducationAssignment>(`${EDUCATION_API}/assignments/${assignment.id}`, {
        method: "PATCH",
        body: { status: "published" },
      });
      await loadClass();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setMutatingId(null);
    }
  };

  const archiveAssignment = async () => {
    if (!archiveTarget || !archiveReason.trim()) return;
    setMutatingId(archiveTarget.id);
    setError(null);
    try {
      await apiFetch<EducationAssignment>(`${EDUCATION_API}/assignments/${archiveTarget.id}/archive`, {
        method: "POST",
        body: { reason: archiveReason.trim() },
      });
      setArchiveTarget(null);
      setArchiveReason("");
      await loadClass();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setMutatingId(null);
    }
  };

  const restoreAssignment = async (assignment: EducationAssignmentSummary) => {
    setMutatingId(assignment.id);
    setError(null);
    try {
      await apiFetch<EducationAssignment>(`${EDUCATION_API}/assignments/${assignment.id}/restore`, {
        method: "POST",
        body: { status: "draft" },
      });
      await loadClass();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setMutatingId(null);
    }
  };

  if (loading) {
    return <div className="space-y-4" aria-busy="true"><div className="h-10 w-1/2 animate-pulse rounded bg-bg-surface" /><div className="h-60 animate-pulse rounded-xl border border-border bg-bg-surface" /></div>;
  }
  if (error || !classItem) {
    return <section className="rounded-xl border border-border bg-bg-surface p-8 text-center"><p role="alert" className="text-sm text-accent-error">{error || tc("error")}</p><button type="button" onClick={() => void loadClass()} className="mt-4 text-sm font-medium text-accent-primary hover:underline">{tc("retry")}</button></section>;
  }

  const canManage = classItem.is_education_manager;
  const classIsArchived = classItem.status === "archived";
  const canModify = canManage && !classIsArchived;
  const authoringFormOpen = showEdit || showNewAssignment;

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <Link href="/teaching/classes" className="inline-flex text-sm text-text-muted hover:text-text-primary">← {t("classes.title")}</Link>

      <section className="rounded-xl border border-border bg-bg-surface p-6" aria-labelledby="teaching-class-title">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 id="teaching-class-title" className="text-2xl font-bold">{classItem.title}</h1>
              <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_CLASS[classItem.status] ?? STATUS_CLASS.draft}`}>{t(`status.${classItem.status}`)}</span>
            </div>
            {classItem.summary && <p className="mt-2 text-sm text-text-secondary">{classItem.summary}</p>}
            <p className="mt-3 text-sm text-text-muted">{t("detail.schedule", { startsAt: formatEducationDate(classItem.starts_at, locale), endsAt: formatEducationDate(classItem.ends_at, locale) })}</p>
          </div>
          {canModify && !showEdit && <Button type="button" variant="secondary" onClick={() => setShowEdit(true)} aria-expanded={false} disabled={showNewAssignment}>{tc("edit")}</Button>}
        </div>
        <div className="mt-5 border-t border-border pt-5"><MarkdownRenderer content={classItem.description_markdown} /></div>
        {classIsArchived && canManage && (
          <div className="mt-5 rounded-lg border border-accent-warning/30 bg-accent-warning/10 p-4">
            <p className="text-sm font-medium text-text-primary">{t("classes.archivedNotice")}</p>
            {classItem.archive_reason && <p className="mt-1 text-sm text-text-secondary">{t("classes.archiveReasonLabel", { reason: classItem.archive_reason })}</p>}
            <Button type="button" variant="secondary" className="mt-3" onClick={() => void restoreClass()} disabled={restoringClass}>{restoringClass ? tc("processing") : t("classes.restore")}</Button>
          </div>
        )}
      </section>

      {error && <p role="alert" className="text-sm text-accent-error">{error}</p>}

      {showEdit && canModify && <section className="rounded-xl border border-border bg-bg-surface p-5" aria-labelledby="teaching-class-edit-title"><h2 id="teaching-class-edit-title" className="mb-4 text-lg font-semibold">{t("classes.edit")}</h2><ClassForm key={classItem.id} initialClass={classItem} onSubmit={updateClass} onCancel={() => setShowEdit(false)} submitLabel={tc("save")} /></section>}

      {!authoringFormOpen && (
        <>
          <EducationResourceManager classId={classId} resources={classItem.resources} canManage={canManage} canAdd={!classIsArchived} onChanged={loadClass} />
          <EducationAssetPanel scope="class" targetId={classId} canManage={canManage} />
          {canManage && <EnrollmentManager classId={classId} />}
        </>
      )}

      <section className="rounded-xl border border-border bg-bg-surface p-5" aria-labelledby="teaching-assignments-title">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div><h2 id="teaching-assignments-title" className="text-lg font-semibold">{t("assignments.title")}</h2><p className="mt-1 text-sm text-text-secondary">{t("assignments.manageDescription")}</p></div>
          {canModify && !showNewAssignment && <Button type="button" onClick={() => setShowNewAssignment(true)} aria-expanded={false} disabled={showEdit}>{t("assignments.create")}</Button>}
        </div>
        {showNewAssignment && canModify && <div className="mt-5 border-t border-border pt-5"><AssignmentForm key={`new-${classItem.id}`} onSubmit={createAssignment} onCancel={() => setShowNewAssignment(false)} submitLabel={t("assignments.create")} /></div>}
        {assignments.length === 0 && <p className="mt-5 text-sm text-text-muted">{t("assignments.empty")}</p>}
        {assignments.length > 0 && <div className="mt-5 space-y-3">{assignments.map((assignment) => <article key={assignment.id} className="rounded-lg border border-border bg-bg-primary p-4"><div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold text-text-primary"><Link href={`/teaching/assignments/${assignment.id}`} className="hover:text-accent-primary">{assignment.title}</Link></h3><span className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_CLASS[assignment.status] ?? STATUS_CLASS.draft}`}>{t(`status.${assignment.status}`)}</span></div><p className="mt-2 text-sm text-text-secondary">{t("assignments.due", { dueAt: formatEducationDate(assignment.due_at, locale), maxScore: assignment.max_score })}</p></div><div className="flex shrink-0 flex-wrap gap-2"><Link href={`/teaching/assignments/${assignment.id}`} className="rounded-lg border border-border px-3 py-2 text-sm font-medium text-text-secondary hover:bg-bg-elevated">{tc("edit")}</Link>{canModify && assignment.status === "draft" && <button type="button" onClick={() => void publishAssignment(assignment)} disabled={Boolean(mutatingId) || authoringFormOpen} className="rounded-lg border border-accent-success/30 px-3 py-2 text-sm font-medium text-accent-success hover:bg-accent-success/10 disabled:opacity-50">{t("assignments.publish")}</button>}{canModify && assignment.status !== "archived" && <button type="button" onClick={() => setArchiveTarget(assignment)} disabled={Boolean(mutatingId) || authoringFormOpen} className="rounded-lg border border-accent-warning/30 px-3 py-2 text-sm font-medium text-accent-warning hover:bg-accent-warning/10 disabled:opacity-50">{t("assignments.archive")}</button>}{canModify && assignment.status === "archived" && <button type="button" onClick={() => void restoreAssignment(assignment)} disabled={Boolean(mutatingId) || authoringFormOpen} className="rounded-lg border border-accent-primary/30 px-3 py-2 text-sm font-medium text-accent-primary hover:bg-accent-primary/10 disabled:opacity-50">{t("assignments.restore")}</button>}</div></div></article>)}</div>}
      </section>

      {archiveTarget && <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/50 p-4"><section role="alertdialog" aria-modal="true" aria-labelledby="education-assignment-archive-title" className="w-full max-w-md rounded-xl border border-border bg-bg-primary p-6"><h2 id="education-assignment-archive-title" className="text-lg font-semibold">{t("assignments.archiveTitle")}</h2><p className="mt-2 text-sm text-text-secondary">{t("assignments.archiveDescription", { title: archiveTarget.title })}</p><label htmlFor="education-assignment-archive-reason" className="mt-4 block text-sm font-medium">{t("assignments.archiveReason")}</label><textarea id="education-assignment-archive-reason" rows={3} maxLength={1000} value={archiveReason} onChange={(event) => setArchiveReason(event.target.value)} className="mt-1 w-full resize-y rounded-lg border border-border bg-bg-surface px-3 py-2 text-sm focus:border-accent-primary focus:outline-none" /><div className="mt-5 flex justify-end gap-3"><Button type="button" variant="ghost" onClick={() => { setArchiveTarget(null); setArchiveReason(""); }} disabled={mutatingId === archiveTarget.id}>{tc("cancel")}</Button><Button type="button" variant="secondary" onClick={() => void archiveAssignment()} disabled={mutatingId === archiveTarget.id || !archiveReason.trim()}>{mutatingId === archiveTarget.id ? tc("processing") : t("assignments.archive")}</Button></div></section></div>}
      <p className="text-xs text-text-muted">{t("detail.serverAuthority")}</p>
    </div>
  );
}
