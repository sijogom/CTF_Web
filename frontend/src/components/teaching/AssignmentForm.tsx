"use client";

import { useMemo, useRef, useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { ApiError } from "@/lib/api-client";
import { isRichContentEmpty, legacyRichContentToHtml } from "@/lib/rich-content.mjs";
import { requiresRichContentEditorReview } from "@/lib/rich-content-editor.mjs";
import { useUnsavedChangesGuard } from "@/lib/use-unsaved-changes-guard";
import { localDateTimeInputToIso, toLocalDateTimeInput } from "@/components/teaching/education-format";
import type { EducationAssignment, EducationAssignmentWritableStatus, EducationAssignmentWritePayload } from "@/types/education";

interface AssignmentFormProps {
  initialAssignment?: EducationAssignment;
  onSubmit: (payload: EducationAssignmentWritePayload) => Promise<void>;
  onCancel: () => void;
  submitLabel: string;
}

interface FormValues {
  title: string;
  instructionsMarkdown: string;
  availableFromAt: string;
  dueAt: string;
  maxScore: string;
  status: EducationAssignmentWritableStatus;
}

function initialValues(value?: EducationAssignment): FormValues {
  const instructions = value?.instructions_markdown ?? "";
  return {
    title: value?.title ?? "",
    instructionsMarkdown: requiresRichContentEditorReview(instructions) ? instructions : legacyRichContentToHtml(instructions),
    availableFromAt: toLocalDateTimeInput(value?.available_from_at),
    dueAt: toLocalDateTimeInput(value?.due_at),
    maxScore: value ? String(value.max_score) : "100",
    status: value?.status === "published" ? "published" : "draft",
  };
}

export function AssignmentForm({ initialAssignment, onSubmit, onCancel, submitLabel }: AssignmentFormProps) {
  const t = useTranslations("education");
  const tc = useTranslations("common");
  const initial = useMemo(() => initialValues(initialAssignment), [initialAssignment]);
  const initialSnapshotRef = useRef(JSON.stringify(initial));
  const [values, setValues] = useState<FormValues>(initial);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dirty = JSON.stringify(values) !== initialSnapshotRef.current;
  const { confirmDiscardOpen, requestDiscard, confirmDiscard, cancelDiscard } = useUnsavedChangesGuard(dirty, { busy: submitting });

  const update = <K extends keyof FormValues>(key: K, value: FormValues[K]) => {
    setValues((current) => ({ ...current, [key]: value }));
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    const availableFromAt = localDateTimeInputToIso(values.availableFromAt);
    const dueAt = localDateTimeInputToIso(values.dueAt);
    const maxScore = Number(values.maxScore);
    if (!values.title.trim() || isRichContentEmpty(values.instructionsMarkdown)) {
      setError(t("form.requiredFields"));
      return;
    }
    if (!Number.isInteger(maxScore) || maxScore < 1 || maxScore > 1000) {
      setError(t("form.invalidMaxScore"));
      return;
    }
    if ((values.availableFromAt && !availableFromAt) || (values.dueAt && !dueAt) || (availableFromAt && dueAt && Date.parse(dueAt) <= Date.parse(availableFromAt))) {
      setError(t("form.invalidSchedule"));
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit({
        title: values.title.trim(),
        instructions_markdown: values.instructionsMarkdown,
        available_from_at: availableFromAt,
        due_at: dueAt,
        max_score: maxScore,
        status: values.status,
      });
    } catch (submitError) {
      setError(submitError instanceof ApiError ? submitError.message : tc("error"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
    <form
      className="space-y-5"
      onSubmit={handleSubmit}
      aria-busy={submitting}
      inert={submitting ? true : undefined}
      noValidate
    >
      {error && <div role="alert" className="rounded-lg border border-accent-error/30 bg-accent-error/10 px-4 py-3 text-sm text-accent-error">{error}</div>}
      <div className="grid gap-4 sm:grid-cols-2"><div><label htmlFor="education-assignment-title" className="mb-1.5 block text-sm font-medium">{t("form.assignmentTitle")} <span className="text-accent-error">*</span></label><input id="education-assignment-title" required maxLength={200} value={values.title} onChange={(event) => update("title", event.target.value)} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm focus:border-accent-primary focus:outline-none" /></div><div><label htmlFor="education-assignment-status" className="mb-1.5 block text-sm font-medium">{t("form.status")}</label><select id="education-assignment-status" value={values.status} onChange={(event) => update("status", event.target.value as EducationAssignmentWritableStatus)} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm focus:border-accent-primary focus:outline-none"><option value="draft">{t("status.draft")}</option><option value="published">{t("status.published")}</option></select></div></div>
      <fieldset className="grid gap-4 rounded-xl border border-border p-4 sm:grid-cols-3"><legend className="px-1 text-sm font-medium">{t("form.schedule")}</legend><div><label htmlFor="education-assignment-opens" className="mb-1.5 block text-sm font-medium">{t("form.opensAt")}</label><input id="education-assignment-opens" type="datetime-local" value={values.availableFromAt} onChange={(event) => update("availableFromAt", event.target.value)} className="datetime-input w-full rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm focus:border-accent-primary focus:outline-none" /></div><div><label htmlFor="education-assignment-due" className="mb-1.5 block text-sm font-medium">{t("form.dueAt")}</label><input id="education-assignment-due" type="datetime-local" value={values.dueAt} onChange={(event) => update("dueAt", event.target.value)} className="datetime-input w-full rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm focus:border-accent-primary focus:outline-none" /></div><div><label htmlFor="education-assignment-score" className="mb-1.5 block text-sm font-medium">{t("form.maxScore")} <span className="text-accent-error">*</span></label><input id="education-assignment-score" required type="number" min="1" max="1000" step="1" inputMode="numeric" value={values.maxScore} onChange={(event) => update("maxScore", event.target.value)} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm focus:border-accent-primary focus:outline-none" /></div></fieldset>
      <div><div id="education-assignment-instructions-label" className="mb-1.5 text-sm font-medium">{t("form.instructions")} <span className="text-accent-error">*</span></div><div aria-labelledby="education-assignment-instructions-label"><RichTextEditor value={values.instructionsMarkdown} onChange={(value) => update("instructionsMarkdown", value)} placeholder={t("form.instructionsPlaceholder")} /></div></div>
      <div className="flex justify-end gap-3"><Button type="button" variant="ghost" onClick={() => requestDiscard(onCancel)} disabled={submitting}>{tc("cancel")}</Button><Button type="submit" disabled={submitting}>{submitting ? tc("processing") : submitLabel}</Button></div>
    </form>
    <ConfirmModal open={confirmDiscardOpen} title={t("form.unsavedTitle")} description={t("form.unsavedDescription")} confirmLabel={t("form.discard")} variant="danger" onConfirm={confirmDiscard} onCancel={cancelDiscard} />
    </>
  );
}
