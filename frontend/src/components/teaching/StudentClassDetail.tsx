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
import { educationSubmissionAvailability, type EducationSubmissionAvailability } from "@/lib/education-state.mjs";
import { EducationAssetPanel } from "@/components/teaching/EducationAssetPanel";
import { EducationResourceManager } from "@/components/teaching/EducationResourceManager";
import {
  EDUCATION_API,
  getAllClassAssignments,
  getEducationAssignment,
  getEducationClass,
  getOwnSubmission,
} from "@/components/teaching/education-api";
import { formatEducationDate } from "@/components/teaching/education-format";
import type {
  EducationAssignment,
  EducationAssignmentSummary,
  EducationClassDetail as EducationClassDetailType,
  EducationOwnSubmissionResponse,
  EducationSubmission,
  EducationSubmissionWritePayload,
} from "@/types/education";

function normalizeLoadedBody(value: string): string {
  return requiresRichContentEditorReview(value) ? value : legacyRichContentToHtml(value);
}

export function StudentClassDetail({ classId }: { classId: string }) {
  const t = useTranslations("education");
  const tc = useTranslations("common");
  const locale = useLocale();
  const [classItem, setClassItem] = useState<EducationClassDetailType | null>(null);
  const [assignments, setAssignments] = useState<EducationAssignmentSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedAssignment, setSelectedAssignment] = useState<EducationAssignmentSummary | null>(null);
  const [assignmentDetail, setAssignmentDetail] = useState<EducationAssignment | null>(null);
  const [existingSubmission, setExistingSubmission] = useState<EducationOwnSubmissionResponse | null>(null);
  const [submissionBody, setSubmissionBody] = useState("");
  const [submissionReady, setSubmissionReady] = useState(false);
  const [dialogLoading, setDialogLoading] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [currentTime, setCurrentTime] = useState(() => Date.now());
  const [hasUnsavedDraft, setHasUnsavedDraft] = useState(false);
  const classRequestGenerationRef = useRef(0);
  const dialogRequestGenerationRef = useRef(0);
  const draftByAssignmentRef = useRef(new Map<string, string>());
  const baselineByAssignmentRef = useRef(new Map<string, string>());
  const { confirmDiscardOpen, confirmDiscard, cancelDiscard } = useUnsavedChangesGuard(hasUnsavedDraft, { busy: submitting });

  const loadClass = useCallback(async () => {
    const generation = ++classRequestGenerationRef.current;
    setLoading(true);
    setError(null);
    try {
      const [detail, list] = await Promise.all([getEducationClass(classId), getAllClassAssignments(classId)]);
      if (generation !== classRequestGenerationRef.current) return;
      setClassItem(detail);
      setAssignments(list);
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
      dialogRequestGenerationRef.current += 1;
    };
  }, [loadClass]);

  useEffect(() => {
    const intervalId = window.setInterval(() => setCurrentTime(Date.now()), 30_000);
    return () => window.clearInterval(intervalId);
  }, []);

  const updateUnsavedState = () => {
    setHasUnsavedDraft(draftByAssignmentRef.current.size > 0);
  };

  const updateSubmissionBody = (value: string) => {
    setSubmissionBody(value);
    if (!selectedAssignment) return;
    const baseline = baselineByAssignmentRef.current.get(selectedAssignment.id) ?? "";
    if (value === baseline) draftByAssignmentRef.current.delete(selectedAssignment.id);
    else draftByAssignmentRef.current.set(selectedAssignment.id, value);
    updateUnsavedState();
  };

  const openSubmission = async (assignment: EducationAssignmentSummary) => {
    if (!classItem) return;
    const generation = ++dialogRequestGenerationRef.current;
    setSelectedAssignment(assignment);
    setAssignmentDetail(null);
    setExistingSubmission(null);
    setSubmissionReady(false);
    setDialogLoading(true);
    setDialogError(null);
    setSubmissionBody(draftByAssignmentRef.current.get(assignment.id) ?? "");

    const detailPromise = getEducationAssignment(assignment.id);
    const ownSubmissionPromise = classItem.is_education_manager
      ? Promise.resolve<EducationOwnSubmissionResponse | null>(null)
      : getOwnSubmission(assignment.id);
    const [detailResult, submissionResult] = await Promise.allSettled([detailPromise, ownSubmissionPromise]);
    if (generation !== dialogRequestGenerationRef.current) return;

    if (detailResult.status === "rejected") {
      setDialogError(detailResult.reason instanceof ApiError ? detailResult.reason.message : tc("error"));
      setDialogLoading(false);
      return;
    }
    setAssignmentDetail(detailResult.value);

    let ownSubmission: EducationOwnSubmissionResponse | null = null;
    if (submissionResult.status === "fulfilled") {
      ownSubmission = submissionResult.value;
    } else if (!(submissionResult.reason instanceof ApiError && submissionResult.reason.status === 404)) {
      setDialogError(submissionResult.reason instanceof ApiError ? submissionResult.reason.message : tc("error"));
      setDialogLoading(false);
      return;
    }

    const serverBody = ownSubmission ? normalizeLoadedBody(ownSubmission.submission.content_markdown) : "";
    baselineByAssignmentRef.current.set(assignment.id, serverBody);
    const draft = draftByAssignmentRef.current.get(assignment.id);
    if (draft === serverBody) {
      draftByAssignmentRef.current.delete(assignment.id);
      updateUnsavedState();
    }
    setExistingSubmission(ownSubmission);
    setSubmissionBody(draft ?? serverBody);
    setSubmissionReady(true);
    setDialogLoading(false);
  };

  const closeSubmission = () => {
    if (submitting) return;
    dialogRequestGenerationRef.current += 1;
    setSelectedAssignment(null);
    setAssignmentDetail(null);
    setExistingSubmission(null);
    setSubmissionReady(false);
    setDialogError(null);
  };

  const submitAssignment = async () => {
    if (!selectedAssignment || !assignmentDetail || !classItem || isRichContentEmpty(submissionBody)) return;
    if (educationSubmissionAvailability(classItem, assignmentDetail, currentTime) !== "open") {
      setDialogError(t("studentAssignments.availability.assignmentUnavailable"));
      return;
    }
    const assignmentId = selectedAssignment.id;
    setSubmitting(true);
    setDialogError(null);
    try {
      const payload: EducationSubmissionWritePayload = { content_markdown: submissionBody };
      const saved = await apiFetch<EducationSubmission & { content_markdown: string }>(`${EDUCATION_API}/assignments/${assignmentId}/submission`, {
        method: "PUT",
        body: payload,
      });
      const normalized = normalizeLoadedBody(saved.content_markdown);
      baselineByAssignmentRef.current.set(assignmentId, normalized);
      draftByAssignmentRef.current.delete(assignmentId);
      updateUnsavedState();
      setSubmissionBody(normalized);
      setExistingSubmission({
        submission: { ...saved, content_markdown: normalized },
        grade: existingSubmission?.grade ?? null,
      });
    } catch (requestError) {
      setDialogError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return <div className="space-y-4" aria-busy="true"><div className="h-10 w-1/2 animate-pulse rounded bg-bg-surface" /><div className="h-64 animate-pulse rounded-xl border border-border bg-bg-surface" /></div>;
  }
  if (error || !classItem) {
    return <section className="rounded-xl border border-border bg-bg-surface p-8 text-center"><p role="alert" className="text-sm text-accent-error">{error || tc("error")}</p><button type="button" onClick={() => void loadClass()} className="mt-4 text-sm font-medium text-accent-primary hover:underline">{tc("retry")}</button></section>;
  }

  const selectedAvailability = selectedAssignment
    ? educationSubmissionAvailability(classItem, assignmentDetail ?? selectedAssignment, currentTime)
    : null;
  const canSubmitSelected = selectedAvailability === "open" && submissionReady;
  const selectedHasUnsavedDraft = Boolean(selectedAssignment && draftByAssignmentRef.current.has(selectedAssignment.id));

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <Link href="/learn/classes" className="inline-flex text-sm text-text-muted hover:text-text-primary">← {t("studentClasses.title")}</Link>
      <section className="rounded-xl border border-border bg-bg-surface p-6" aria-labelledby="student-class-title">
        <h1 id="student-class-title" className="text-2xl font-bold">{classItem.title}</h1>
        {classItem.summary && <p className="mt-2 text-sm text-text-secondary">{classItem.summary}</p>}
        <p className="mt-3 text-sm text-text-secondary">{t("studentClasses.schedule", { startsAt: formatEducationDate(classItem.starts_at, locale), endsAt: formatEducationDate(classItem.ends_at, locale) })}</p>
        <div className="mt-5 border-t border-border pt-5"><MarkdownRenderer content={classItem.description_markdown} /></div>
      </section>

      <EducationResourceManager classId={classId} resources={classItem.resources} canManage={false} canAdd={false} onChanged={loadClass} />
      <EducationAssetPanel scope="class" targetId={classId} canManage={false} />

      <section className="rounded-xl border border-border bg-bg-surface p-5" aria-labelledby="student-assignments-title">
        <h2 id="student-assignments-title" className="text-lg font-semibold">{t("studentAssignments.title")}</h2>
        {assignments.length === 0 && <p className="mt-4 text-sm text-text-muted">{t("studentAssignments.empty")}</p>}
        {assignments.length > 0 && <div className="mt-4 space-y-4">{assignments.map((assignment) => {
          const availability: EducationSubmissionAvailability = educationSubmissionAvailability(classItem, assignment, currentTime);
          return (
            <article key={assignment.id} className="rounded-lg border border-border bg-bg-primary p-4">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <h3 className="font-semibold text-text-primary">{assignment.title}</h3>
                  <p className="mt-1 text-sm text-text-secondary">{t("studentAssignments.due", { dueAt: formatEducationDate(assignment.due_at, locale), maxScore: assignment.max_score })}</p>
                  {availability !== "open" && <p className="mt-2 text-xs text-accent-warning" role="status">{t(`studentAssignments.availability.${availability}`)}</p>}
                </div>
                <Button type="button" variant="secondary" onClick={() => void openSubmission(assignment)} disabled={dialogLoading && selectedAssignment?.id === assignment.id}>{t("assignments.open")}</Button>
              </div>
              <div className="mt-4 border-t border-border pt-4"><EducationAssetPanel scope="assignment" targetId={assignment.id} canManage={false} /></div>
            </article>
          );
        })}</div>}
      </section>

      {selectedAssignment && (
        <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/50 p-4">
          <section role="dialog" aria-modal="true" aria-labelledby="student-submission-title" className="max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded-xl border border-border bg-bg-primary p-6">
            <h2 id="student-submission-title" className="text-lg font-semibold">{selectedAssignment.title}</h2>
            <p className="mt-1 text-sm text-text-secondary">{t("studentAssignments.submissionDescription")}</p>

            {dialogLoading && <div className="mt-4 h-40 animate-pulse rounded-lg bg-bg-surface" aria-busy="true" />}
            {dialogError && <div className="mt-4 rounded-lg bg-accent-error/10 p-3"><p className="text-sm text-accent-error" role="alert">{dialogError}</p><button type="button" onClick={() => void openSubmission(selectedAssignment)} className="mt-2 text-sm font-medium text-accent-primary hover:underline">{tc("retry")}</button></div>}

            {assignmentDetail && (
              <>
                <section className="mt-5 rounded-lg border border-border bg-bg-surface p-4" aria-labelledby="student-assignment-instructions">
                  <h3 id="student-assignment-instructions" className="mb-3 text-sm font-semibold">{t("form.instructions")}</h3>
                  <MarkdownRenderer content={assignmentDetail.instructions_markdown} />
                </section>

                {existingSubmission?.grade?.status === "published" && (
                  <div className="mt-4 rounded-lg bg-accent-success/10 p-3">
                    <p className="text-sm font-semibold text-accent-success">{t("studentAssignments.score", { score: existingSubmission.grade.score, maxScore: assignmentDetail.max_score })}</p>
                    {existingSubmission.grade.feedback_markdown && <div className="mt-2"><MarkdownRenderer content={existingSubmission.grade.feedback_markdown} /></div>}
                  </div>
                )}

                {selectedAvailability && selectedAvailability !== "open" && <p className="mt-4 text-sm text-accent-warning" role="status">{t(`studentAssignments.availability.${selectedAvailability}`)}</p>}

                {submissionReady && canSubmitSelected && (
                  <div
                    className="mt-4"
                    aria-busy={submitting}
                    aria-disabled={submitting}
                    inert={submitting ? true : undefined}
                  ><RichTextEditor value={submissionBody} onChange={updateSubmissionBody} placeholder={t("studentAssignments.submissionPlaceholder")} /></div>
                )}
                {submissionReady && !canSubmitSelected && existingSubmission && (
                  <section className="mt-4 rounded-lg border border-border bg-bg-surface p-4" aria-labelledby="student-existing-submission">
                    <h3 id="student-existing-submission" className="mb-3 text-sm font-semibold">{t("submissions.title")}</h3>
                    <MarkdownRenderer content={existingSubmission.submission.content_markdown} />
                  </section>
                )}
              </>
            )}

            {selectedHasUnsavedDraft && (!canSubmitSelected || !assignmentDetail) && (
              <section className="mt-4 rounded-lg border border-accent-warning/30 bg-accent-warning/10 p-4" aria-labelledby="student-unsaved-submission">
                <h3 id="student-unsaved-submission" className="text-sm font-semibold">{t("studentAssignments.unsavedDraftTitle")}</h3>
                <p className="mt-1 text-xs text-text-secondary">{t("studentAssignments.unsavedDraftReadOnly")}</p>
                <div className="mt-3 rounded-lg border border-border bg-bg-primary p-3"><MarkdownRenderer content={submissionBody} /></div>
              </section>
            )}

            <div className="mt-5 flex justify-end gap-3">
              <Button type="button" variant="ghost" onClick={closeSubmission} disabled={submitting}>{tc("close")}</Button>
              {canSubmitSelected && <Button type="button" onClick={() => void submitAssignment()} disabled={submitting || isRichContentEmpty(submissionBody)}>{submitting ? tc("processing") : existingSubmission ? t("studentAssignments.updateSubmission") : tc("submit")}</Button>}
            </div>
          </section>
        </div>
      )}
      <ConfirmModal open={confirmDiscardOpen} title={t("form.unsavedTitle")} description={t("form.unsavedDescription")} confirmLabel={t("form.discard")} variant="danger" onConfirm={confirmDiscard} onCancel={cancelDiscard} />
      <p className="text-xs text-text-muted">{t("studentClasses.serverAuthority")}</p>
    </div>
  );
}
