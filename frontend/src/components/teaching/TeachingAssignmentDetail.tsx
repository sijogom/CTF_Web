"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { ApiError, apiFetch } from "@/lib/api-client";
import { isRichContentEmpty, legacyRichContentToHtml } from "@/lib/rich-content.mjs";
import { requiresRichContentEditorReview } from "@/lib/rich-content-editor.mjs";
import { useUnsavedChangesGuard } from "@/lib/use-unsaved-changes-guard";
import { AssignmentForm } from "@/components/teaching/AssignmentForm";
import { EducationAssetPanel } from "@/components/teaching/EducationAssetPanel";
import { EDUCATION_API, getAllAssignmentSubmissions, getAssignmentSubmissionForGrading, getEducationAssignment } from "@/components/teaching/education-api";
import { formatEducationDate } from "@/components/teaching/education-format";
import type { EducationAssignment, EducationAssignmentWritePayload, EducationGrade, EducationGradePayload, EducationOwnSubmissionResponse, EducationSubmission } from "@/types/education";

const STATUS_CLASS: Record<string, string> = {
  draft: "bg-bg-elevated text-text-secondary",
  published: "bg-accent-success/10 text-accent-success",
  archived: "bg-accent-warning/10 text-accent-warning",
};

export function TeachingAssignmentDetail({ assignmentId }: { assignmentId: string }) {
  const t = useTranslations("education");
  const tc = useTranslations("common");
  const locale = useLocale();
  const [assignment, setAssignment] = useState<EducationAssignment | null>(null);
  const [submissions, setSubmissions] = useState<EducationSubmission[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showEdit, setShowEdit] = useState(false);
  const [gradeTarget, setGradeTarget] = useState<EducationSubmission | null>(null);
  const [gradeDetail, setGradeDetail] = useState<EducationOwnSubmissionResponse | null>(null);
  const [score, setScore] = useState("");
  const [feedback, setFeedback] = useState("");
  const [gradeStatus, setGradeStatus] = useState<"draft" | "published">("published");
  const [savingGrade, setSavingGrade] = useState(false);
  const [gradeLoading, setGradeLoading] = useState(false);
  const [gradeError, setGradeError] = useState<string | null>(null);
  const assignmentRequestGenerationRef = useRef(0);
  const gradeRequestGenerationRef = useRef(0);
  const gradeTargetRef = useRef<EducationSubmission | null>(null);
  const loadedGradeRef = useRef<EducationOwnSubmissionResponse | null>(null);
  const gradeSaveRef = useRef<{ targetId: string; generation: number } | null>(null);
  const gradeDialogRef = useRef<HTMLDialogElement>(null);
  const gradeReturnFocusRef = useRef<HTMLElement | null>(null);
  const gradeReturnTargetRef = useRef<string | null>(null);
  const initialGradeSnapshotRef = useRef<string | null>(null);
  const gradeOpen = gradeTarget !== null;
  const gradeSnapshot = JSON.stringify({ score, feedback, gradeStatus });
  const gradeDirty = Boolean(gradeDetail && initialGradeSnapshotRef.current && initialGradeSnapshotRef.current !== gradeSnapshot);
  const { confirmDiscardOpen, requestDiscard, confirmDiscard, cancelDiscard } = useUnsavedChangesGuard(gradeDirty, { busy: savingGrade || gradeLoading });

  const restoreGradeFocus = useCallback(() => {
    window.requestAnimationFrame(() => {
      if (gradeTargetRef.current || gradeDialogRef.current?.open) return;
      const returnFocus = gradeReturnFocusRef.current;
      const target = document.getElementById(`education-grade-target-${gradeReturnTargetRef.current}`)
        ?? (returnFocus?.isConnected ? returnFocus : null)
        ?? document.getElementById("teaching-assignment-title")
        ?? document.getElementById("teaching-assignment-error");
      if (!target || target.closest("[inert]")) return;
      target.focus();
      gradeReturnFocusRef.current = null;
      gradeReturnTargetRef.current = null;
    });
  }, []);

  useEffect(() => {
    if (!gradeOpen) return;
    const dialog = gradeDialogRef.current;
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();
    return () => {
      if (dialog.open) dialog.close();
      restoreGradeFocus();
    };
  }, [gradeOpen, restoreGradeFocus]);

  const loadAssignment = useCallback(async () => {
    const generation = ++assignmentRequestGenerationRef.current;
    setLoading(true);
    setError(null);
    try {
      const [detail, submissionResponse] = await Promise.all([
        getEducationAssignment(assignmentId),
        getAllAssignmentSubmissions(assignmentId),
      ]);
      if (generation !== assignmentRequestGenerationRef.current) return;
      setAssignment(detail);
      setSubmissions(submissionResponse);
    } catch (requestError) {
      if (generation !== assignmentRequestGenerationRef.current) return;
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      if (generation === assignmentRequestGenerationRef.current) setLoading(false);
    }
  }, [assignmentId, tc]);

  useEffect(() => {
    void loadAssignment();
    return () => {
      assignmentRequestGenerationRef.current += 1;
      gradeRequestGenerationRef.current += 1;
      gradeTargetRef.current = null;
      loadedGradeRef.current = null;
      gradeSaveRef.current = null;
    };
  }, [loadAssignment]);

  const updateAssignment = async (payload: EducationAssignmentWritePayload) => {
    await apiFetch<EducationAssignment>(`${EDUCATION_API}/assignments/${assignmentId}`, { method: "PATCH", body: payload });
    setShowEdit(false);
    await loadAssignment();
  };

  const loadGrade = async (submission: EducationSubmission) => {
    if (gradeSaveRef.current) return;
    const generation = ++gradeRequestGenerationRef.current;
    gradeTargetRef.current = submission;
    gradeReturnTargetRef.current = submission.id;
    loadedGradeRef.current = null;
    initialGradeSnapshotRef.current = null;
    setGradeTarget(submission);
    setGradeDetail(null);
    setGradeError(null);
    setGradeLoading(true);
    try {
      const detail = await getAssignmentSubmissionForGrading(assignmentId, submission.student_id);
      if (generation !== gradeRequestGenerationRef.current) return;
      loadedGradeRef.current = detail;
      setGradeDetail(detail);
      const nextScore = detail.grade ? String(detail.grade.score) : "";
      const persistedFeedback = detail.grade?.feedback_markdown ?? "";
      const nextFeedback = requiresRichContentEditorReview(persistedFeedback)
        ? persistedFeedback
        : legacyRichContentToHtml(persistedFeedback);
      const nextStatus = detail.grade?.status ?? "published";
      initialGradeSnapshotRef.current = JSON.stringify({ score: nextScore, feedback: nextFeedback, gradeStatus: nextStatus });
      setScore(nextScore);
      setFeedback(nextFeedback);
      setGradeStatus(nextStatus);
    } catch (requestError) {
      if (generation !== gradeRequestGenerationRef.current) return;
      setGradeError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      if (generation === gradeRequestGenerationRef.current) setGradeLoading(false);
    }
  };

  const openGrade = (submission: EducationSubmission) => {
    if (gradeSaveRef.current || confirmDiscardOpen) return;
    if (gradeTargetRef.current) {
      requestDiscard(() => void loadGrade(submission));
      return;
    }
    gradeReturnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    void loadGrade(submission);
  };

  const discardGradeDraft = () => {
    if (gradeSaveRef.current) return;
    gradeRequestGenerationRef.current += 1;
    gradeTargetRef.current = null;
    loadedGradeRef.current = null;
    initialGradeSnapshotRef.current = null;
    setGradeTarget(null);
    setGradeDetail(null);
    setGradeError(null);
    setGradeLoading(false);
  };

  const closeGrade = () => {
    if (gradeSaveRef.current || confirmDiscardOpen) return;
    requestDiscard(discardGradeDraft);
  };

  const cancelGrade = (event: React.SyntheticEvent<HTMLDialogElement>) => {
    event.preventDefault();
    closeGrade();
  };

  const trapGradeFocus = (event: React.KeyboardEvent<HTMLDialogElement>) => {
    if (event.key !== "Tab") return;
    const dialog = event.currentTarget;
    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [contenteditable="true"], [tabindex]:not([tabindex="-1"])',
    )).filter((element) => !element.matches(":disabled") && !element.closest("[inert]") && element.getClientRects().length > 0);
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first) {
      event.preventDefault();
      dialog.focus();
    } else if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog)) {
      event.preventDefault();
      first.focus();
    }
  };

  const saveGrade = async () => {
    if (!assignment || !gradeTarget || !gradeDetail || gradeSaveRef.current || confirmDiscardOpen
      || gradeTargetRef.current !== gradeTarget || loadedGradeRef.current !== gradeDetail) return;
    const numericScore = Number(score);
    if (!score.trim() || !Number.isInteger(numericScore) || numericScore < 0 || numericScore > assignment.max_score) {
      setGradeError(t("grading.invalidScore", { maxScore: assignment.max_score }));
      return;
    }
    const operation = { targetId: gradeTarget.id, generation: gradeRequestGenerationRef.current };
    const targetStudentId = gradeTarget.student_id;
    const targetAssignmentId = assignmentId;
    const isCurrentOperation = () => gradeSaveRef.current === operation
      && gradeRequestGenerationRef.current === operation.generation
      && gradeTargetRef.current?.id === operation.targetId;
    gradeSaveRef.current = operation;
    setSavingGrade(true);
    setGradeError(null);
    try {
      const payload: EducationGradePayload = {
        score: numericScore,
        feedback_markdown: isRichContentEmpty(feedback) ? null : feedback,
        status: gradeStatus,
      };
      await apiFetch<EducationGrade>(`${EDUCATION_API}/assignments/${targetAssignmentId}/submissions/${targetStudentId}/grade`, { method: "PUT", body: payload });
      if (!isCurrentOperation()) return;
      gradeSaveRef.current = null;
      setSavingGrade(false);
      discardGradeDraft();
      const closedGeneration = gradeRequestGenerationRef.current;
      await loadAssignment();
      if (gradeRequestGenerationRef.current === closedGeneration && !gradeTargetRef.current) restoreGradeFocus();
    } catch (requestError) {
      if (isCurrentOperation()) setGradeError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      if (gradeSaveRef.current === operation) {
        gradeSaveRef.current = null;
        if (gradeRequestGenerationRef.current === operation.generation && gradeTargetRef.current?.id === operation.targetId) setSavingGrade(false);
      }
    }
  };

  if (loading) {
    return <div className="space-y-4" aria-busy="true"><div className="h-10 w-1/2 animate-pulse rounded bg-bg-surface" /><div className="h-80 animate-pulse rounded-xl border border-border bg-bg-surface" /></div>;
  }
  if (error || !assignment) {
    return <section id="teaching-assignment-error" tabIndex={-1} className="rounded-xl border border-border bg-bg-surface p-8 text-center"><p role="alert" className="text-sm text-accent-error">{error || tc("error")}</p><button type="button" onClick={() => void loadAssignment()} className="mt-4 text-sm font-medium text-accent-primary hover:underline">{tc("retry")}</button></section>;
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <Link href={`/teaching/classes/${assignment.class_id}`} className="inline-flex text-sm text-text-muted hover:text-text-primary">← {t("assignments.backToClass")}</Link>
      <section className="rounded-xl border border-border bg-bg-surface p-6" aria-labelledby="teaching-assignment-title">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h1 id="teaching-assignment-title" tabIndex={-1} className="text-2xl font-bold">{assignment.title}</h1>
              <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_CLASS[assignment.status] ?? STATUS_CLASS.draft}`}>{t(`status.${assignment.status}`)}</span>
            </div>
            <p className="mt-2 text-sm text-text-secondary">{t("assignments.due", { dueAt: formatEducationDate(assignment.due_at, locale), maxScore: assignment.max_score })}</p>
          </div>
          {!showEdit && <Button type="button" variant="secondary" onClick={() => setShowEdit(true)} aria-expanded={false}>{tc("edit")}</Button>}
        </div>
        <div className="mt-5 border-t border-border pt-5"><MarkdownRenderer content={assignment.instructions_markdown} /></div>
      </section>
      {showEdit && <section className="rounded-xl border border-border bg-bg-surface p-5"><h2 className="mb-4 text-lg font-semibold">{t("assignments.edit")}</h2><AssignmentForm initialAssignment={assignment} onSubmit={updateAssignment} onCancel={() => setShowEdit(false)} submitLabel={tc("save")} /></section>}
      <EducationAssetPanel scope="assignment" targetId={assignmentId} canManage />
      <section className="rounded-xl border border-border bg-bg-surface p-5" aria-labelledby="education-submissions-title">
        <div><h2 id="education-submissions-title" className="text-lg font-semibold">{t("submissions.title")}</h2><p className="mt-1 text-sm text-text-secondary">{t("submissions.manageDescription")}</p></div>
        {submissions.length === 0 && <p className="mt-5 text-sm text-text-muted">{t("submissions.empty")}</p>}
        {submissions.length > 0 && <div className="mt-5 space-y-3">{submissions.map((submission) => <article key={submission.id} className="rounded-lg border border-border bg-bg-primary p-4"><div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between"><div><p className="font-mono text-xs text-text-secondary">{submission.student_id}</p><p className="mt-2 text-xs text-text-muted">{t("submissions.submittedAt", { submittedAt: formatEducationDate(submission.submitted_at, locale) })}</p><p className="mt-1 text-xs text-text-muted">{t("submissions.contentLength", { count: submission.content_length })}</p></div><button id={`education-grade-target-${submission.id}`} type="button" onClick={() => void openGrade(submission)} disabled={showEdit || gradeOpen || savingGrade} className="shrink-0 text-sm font-medium text-accent-primary hover:underline disabled:cursor-not-allowed disabled:opacity-50">{submission.grade_status === "published" ? t("grading.edit") : t("grading.grade")}</button></div></article>)}</div>}
      </section>
      {gradeTarget && (
          <dialog ref={gradeDialogRef} tabIndex={-1} onCancel={cancelGrade} onKeyDown={trapGradeFocus} aria-modal="true" aria-labelledby="education-grade-title" aria-busy={gradeLoading || savingGrade} className="fixed inset-0 z-[90] m-auto max-h-[90vh] w-[calc(100vw-32px)] max-w-2xl overflow-y-auto rounded-xl border border-border bg-bg-primary p-6 backdrop:bg-black/50">
            <h2 id="education-grade-title" className="text-lg font-semibold">{t("grading.title")}</h2>
            {gradeLoading && <div className="mt-4 h-40 animate-pulse rounded-lg bg-bg-surface" aria-busy="true" />}
            {gradeError && (
              <div className="mt-4 rounded-lg bg-accent-error/10 p-3">
                <p className="text-sm text-accent-error" role="alert">{gradeError}</p>
                {!gradeDetail && <button type="button" onClick={() => void openGrade(gradeTarget)} className="mt-2 text-sm font-medium text-accent-primary hover:underline">{tc("retry")}</button>}
              </div>
            )}
            {gradeDetail && (
              <>
                <div className="mt-4 rounded-lg border border-border bg-bg-surface p-4"><MarkdownRenderer content={gradeDetail.submission.content_markdown} /></div>
                <div className="mt-4">
                  <label htmlFor="education-grade-score" className="mb-1.5 block text-sm font-medium">{t("grading.scoreLabel", { maxScore: assignment.max_score })}</label>
                  <input id="education-grade-score" type="number" min="0" max={assignment.max_score} step="1" value={score} onChange={(event) => setScore(event.target.value)} disabled={savingGrade} className="w-full rounded-lg border border-border bg-bg-surface px-3 py-2.5 text-sm focus:border-accent-primary focus:outline-none disabled:opacity-60" />
                </div>
                <div className="mt-4">
                  <label htmlFor="education-grade-status" className="mb-1.5 block text-sm font-medium">{t("grading.visibility")}</label>
                  <select id="education-grade-status" value={gradeStatus} onChange={(event) => setGradeStatus(event.target.value as "draft" | "published")} disabled={savingGrade} className="w-full rounded-lg border border-border bg-bg-surface px-3 py-2.5 text-sm disabled:opacity-60"><option value="draft">{t("grading.draft")}</option><option value="published">{t("grading.published")}</option></select>
                </div>
                <div className="mt-4">
                  <div id="education-grade-feedback-label" className="mb-1.5 text-sm font-medium">{t("grading.feedback")}</div>
                  <div aria-labelledby="education-grade-feedback-label" aria-disabled={savingGrade} inert={savingGrade ? true : undefined}><RichTextEditor value={feedback} onChange={setFeedback} placeholder={t("grading.feedbackPlaceholder")} /></div>
                </div>
              </>
            )}
            <div className="mt-5 flex justify-end gap-3">
              <Button type="button" variant="ghost" onClick={closeGrade} disabled={savingGrade}>{tc("cancel")}</Button>
              <Button type="button" onClick={() => void saveGrade()} disabled={savingGrade || !gradeDetail}>{savingGrade ? tc("processing") : tc("save")}</Button>
            </div>
          </dialog>
      )}
      <ConfirmModal
        open={confirmDiscardOpen}
        title={t("grading.unsavedTitle")}
        description={t("grading.unsavedDescription")}
        confirmLabel={t("grading.discard")}
        variant="danger"
        onConfirm={confirmDiscard}
        onCancel={cancelDiscard}
      />
      <p className="text-xs text-text-muted">{t("detail.serverAuthority")}</p>
    </div>
  );
}
