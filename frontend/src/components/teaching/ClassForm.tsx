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
import type { EducationClassDetail, EducationClassWritableStatus, EducationClassWritePayload } from "@/types/education";

interface ClassFormProps {
  initialClass?: EducationClassDetail;
  onSubmit: (payload: EducationClassWritePayload) => Promise<void>;
  onCancel: () => void;
  submitLabel: string;
}

interface FormValues {
  title: string;
  summary: string;
  descriptionMarkdown: string;
  startsAt: string;
  endsAt: string;
  status: EducationClassWritableStatus;
}

function initialValues(value?: EducationClassDetail): FormValues {
  const description = value?.description_markdown ?? "";
  return {
    title: value?.title ?? "",
    summary: value?.summary ?? "",
    descriptionMarkdown: requiresRichContentEditorReview(description) ? description : legacyRichContentToHtml(description),
    startsAt: toLocalDateTimeInput(value?.starts_at),
    endsAt: toLocalDateTimeInput(value?.ends_at),
    status: value?.status === "published" ? "published" : "draft",
  };
}

export function ClassForm({ initialClass, onSubmit, onCancel, submitLabel }: ClassFormProps) {
  const t = useTranslations("education");
  const tc = useTranslations("common");
  const initial = useMemo(() => initialValues(initialClass), [initialClass]);
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
    const startsAt = localDateTimeInputToIso(values.startsAt);
    const endsAt = localDateTimeInputToIso(values.endsAt);
    if (!values.title.trim() || isRichContentEmpty(values.descriptionMarkdown)) {
      setError(t("form.requiredFields"));
      return;
    }
    if ((values.startsAt && !startsAt) || (values.endsAt && !endsAt) || (startsAt && endsAt && Date.parse(endsAt) <= Date.parse(startsAt))) {
      setError(t("form.invalidSchedule"));
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit({
        title: values.title.trim(),
        summary: values.summary.trim() || null,
        description_markdown: values.descriptionMarkdown,
        starts_at: startsAt,
        ends_at: endsAt,
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
      <div>
        <label htmlFor="education-class-title" className="mb-1.5 block text-sm font-medium">{t("form.classTitle")} <span className="text-accent-error">*</span></label>
        <input id="education-class-title" required maxLength={200} value={values.title} onChange={(event) => update("title", event.target.value)} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm focus:border-accent-primary focus:outline-none" />
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="education-class-summary" className="mb-1.5 block text-sm font-medium">{t("form.summary")}</label>
          <input id="education-class-summary" maxLength={500} value={values.summary} onChange={(event) => update("summary", event.target.value)} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm focus:border-accent-primary focus:outline-none" />
        </div>
        <div>
          <label htmlFor="education-class-status" className="mb-1.5 block text-sm font-medium">{t("form.status")}</label>
          <select id="education-class-status" value={values.status} onChange={(event) => update("status", event.target.value as EducationClassWritableStatus)} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm focus:border-accent-primary focus:outline-none"><option value="draft">{t("status.draft")}</option><option value="published">{t("status.published")}</option></select>
        </div>
      </div>
      <fieldset className="grid gap-4 rounded-xl border border-border p-4 sm:grid-cols-2">
        <legend className="px-1 text-sm font-medium">{t("form.schedule")}</legend>
        <div>
          <label htmlFor="education-class-start" className="mb-1.5 block text-sm font-medium">{t("form.startsAt")}</label>
          <input id="education-class-start" type="datetime-local" value={values.startsAt} onChange={(event) => update("startsAt", event.target.value)} className="datetime-input w-full rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm focus:border-accent-primary focus:outline-none" />
        </div>
        <div>
          <label htmlFor="education-class-end" className="mb-1.5 block text-sm font-medium">{t("form.endsAt")}</label>
          <input id="education-class-end" type="datetime-local" value={values.endsAt} onChange={(event) => update("endsAt", event.target.value)} className="datetime-input w-full rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm focus:border-accent-primary focus:outline-none" />
        </div>
      </fieldset>
      <div>
        <div id="education-class-description-label" className="mb-1.5 text-sm font-medium">{t("form.description")} <span className="text-accent-error">*</span></div>
        <div aria-labelledby="education-class-description-label"><RichTextEditor value={values.descriptionMarkdown} onChange={(value) => update("descriptionMarkdown", value)} placeholder={t("form.descriptionPlaceholder")} /></div>
      </div>
      <div className="flex justify-end gap-3"><Button type="button" variant="ghost" onClick={() => requestDiscard(onCancel)} disabled={submitting}>{tc("cancel")}</Button><Button type="submit" disabled={submitting}>{submitting ? tc("processing") : submitLabel}</Button></div>
    </form>
    <ConfirmModal open={confirmDiscardOpen} title={t("form.unsavedTitle")} description={t("form.unsavedDescription")} confirmLabel={t("form.discard")} variant="danger" onConfirm={confirmDiscard} onCancel={cancelDiscard} />
    </>
  );
}
