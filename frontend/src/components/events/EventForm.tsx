"use client";

import { useRef, useState, type FormEvent } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { isRichContentEmpty, richContentVisibleLength } from "@/lib/rich-content.mjs";
import {
  defaultSeoulDateTimeInput,
  seoulDateTimeInputToUtcIso,
  toSeoulDateTimeInput,
} from "@/lib/event-date";
import {
  eventCategoryLabel,
  type EventAdminDetail,
  type EventCategory,
  type EventWritableStatus,
  type EventWritePayload,
} from "@/types/event";
import { ApiError } from "@/lib/api-client";
import { useEventCategories } from "@/components/events/useEventCategories";

export interface EventFormValues {
  title: string;
  summary: string;
  contentMarkdown: string;
  category: EventCategory;
  location: string;
  externalUrl: string;
  startsAt: string;
  endsAt: string;
  visibleFromAt: string;
  visibleUntilAt: string;
  status: EventWritableStatus;
}

interface EventFormProps {
  initialValues: EventFormValues;
  onSubmit: (payload: EventWritePayload) => Promise<void>;
  onCancel: () => void;
  submitLabel: string;
}

function toEventFormValues(event: EventAdminDetail): EventFormValues {
  return {
    title: event.title,
    summary: event.summary ?? "",
    contentMarkdown: event.content_markdown,
    category: event.category,
    location: event.location ?? "",
    externalUrl: event.external_url ?? "",
    startsAt: toSeoulDateTimeInput(event.starts_at),
    endsAt: toSeoulDateTimeInput(event.ends_at),
    visibleFromAt: toSeoulDateTimeInput(event.visible_from_at),
    visibleUntilAt: event.visible_until_at ? toSeoulDateTimeInput(event.visible_until_at) : "",
    status: event.status === "published" ? "published" : "draft",
  };
}

export function createEventFormValues(event?: EventAdminDetail): EventFormValues {
  if (event) return toEventFormValues(event);

  const startsAt = defaultSeoulDateTimeInput();
  return {
    title: "",
    summary: "",
    contentMarkdown: "",
    category: "workshop",
    location: "",
    externalUrl: "",
    startsAt,
    endsAt: defaultSeoulDateTimeInput(60),
    visibleFromAt: startsAt,
    visibleUntilAt: "",
    status: "draft",
  };
}

function validateExternalUrl(value: string): boolean {
  if (!value) return true;
  try {
    const parsed = new URL(value);
    return (
      (parsed.protocol === "http:" || parsed.protocol === "https:") &&
      Boolean(parsed.hostname) &&
      !parsed.username &&
      !parsed.password
    );
  } catch {
    return false;
  }
}

export function EventForm({ initialValues, onSubmit, onCancel, submitLabel }: EventFormProps) {
  const t = useTranslations("events");
  const tc = useTranslations("common");
  const locale = useLocale();
  const categoryOptions = useEventCategories(true);
  const [values, setValues] = useState<EventFormValues>(initialValues);
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);

  const update = <K extends keyof EventFormValues>(key: K, value: EventFormValues[K]) => {
    setValues((previous) => ({ ...previous, [key]: value }));
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submittingRef.current || categoryOptions.loading || categoryOptions.error) return;
    setError(null);

    if (!values.title.trim() || isRichContentEmpty(values.contentMarkdown)) {
      setError(t("form.requiredFields"));
      return;
    }
    if (!validateExternalUrl(values.externalUrl.trim())) {
      setError(t("form.invalidExternalUrl"));
      return;
    }

    const startsAt = seoulDateTimeInputToUtcIso(values.startsAt);
    const endsAt = seoulDateTimeInputToUtcIso(values.endsAt);
    const visibleFromAt = seoulDateTimeInputToUtcIso(values.visibleFromAt);
    const visibleUntilAt = values.visibleUntilAt
      ? seoulDateTimeInputToUtcIso(values.visibleUntilAt)
      : null;

    if (!startsAt || !endsAt || !visibleFromAt || (values.visibleUntilAt && !visibleUntilAt)) {
      setError(t("form.invalidDateTime"));
      return;
    }
    if (Date.parse(endsAt) <= Date.parse(startsAt)) {
      setError(t("form.endBeforeStart"));
      return;
    }
    if (visibleUntilAt && Date.parse(visibleUntilAt) <= Date.parse(visibleFromAt)) {
      setError(t("form.visibilityEndBeforeStart"));
      return;
    }

    submittingRef.current = true;
    setSubmitting(true);
    try {
      await onSubmit({
        title: values.title.trim(),
        summary: values.summary.trim() || null,
        content_markdown: values.contentMarkdown,
        category: values.category,
        location: values.location.trim() || null,
        external_url: values.externalUrl.trim() || null,
        starts_at: startsAt,
        ends_at: endsAt,
        visible_from_at: visibleFromAt,
        visible_until_at: visibleUntilAt,
        status: values.status,
      });
    } catch (submitError) {
      setError(submitError instanceof ApiError ? submitError.message : tc("errorOccurred"));
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  return (
    <form className="space-y-6" onSubmit={handleSubmit} noValidate>
      {error && (
        <div role="alert" className="rounded-lg border border-accent-error/30 bg-accent-error/10 px-4 py-3 text-sm text-accent-error">
          {error}
        </div>
      )}

      <div>
        <label htmlFor="event-title" className="mb-1.5 block text-sm font-medium">
          {t("form.title")} <span className="text-accent-error">*</span>
        </label>
        <input
          id="event-title"
          required
          maxLength={200}
          value={values.title}
          onChange={(event) => update("title", event.target.value)}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
        />
      </div>

      <div>
        <label htmlFor="event-summary" className="mb-1.5 block text-sm font-medium">
          {t("form.summary")}
        </label>
        <textarea
          id="event-summary"
          maxLength={500}
          rows={3}
          value={values.summary}
          onChange={(event) => update("summary", event.target.value)}
          className="w-full resize-y rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="event-category" className="mb-1.5 block text-sm font-medium">
            {t("form.category")} <span className="text-accent-error">*</span>
          </label>
          <select
            id="event-category"
            value={values.category}
            disabled={categoryOptions.loading || Boolean(categoryOptions.error) || submitting}
            onChange={(event) => update("category", event.target.value as EventCategory)}
            className="w-full rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm"
          >
            {categoryOptions.categories.map((category) => (
              <option key={category.key} value={category.key}>{eventCategoryLabel(category.labels, locale, category.key)}</option>
            ))}
          </select>
          {categoryOptions.error && <div role="alert" className="mt-2 text-sm text-accent-error">{categoryOptions.error} <button type="button" onClick={() => void categoryOptions.reload()} className="underline">{tc("retry")}</button></div>}
        </div>
        <div>
          <label htmlFor="event-status" className="mb-1.5 block text-sm font-medium">
            {t("form.status")}
          </label>
          <select
            id="event-status"
            value={values.status}
            onChange={(event) => update("status", event.target.value as EventWritableStatus)}
            className="w-full rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm"
          >
            <option value="draft">{t("statuses.draft")}</option>
            <option value="published">{t("statuses.published")}</option>
          </select>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="event-location" className="mb-1.5 block text-sm font-medium">
            {t("form.location")}
          </label>
          <input
            id="event-location"
            maxLength={300}
            value={values.location}
            onChange={(event) => update("location", event.target.value)}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
          />
        </div>
        <div>
          <label htmlFor="event-external-url" className="mb-1.5 block text-sm font-medium">
            {t("form.externalUrl")}
          </label>
          <input
            id="event-external-url"
            type="url"
            inputMode="url"
            maxLength={2048}
            value={values.externalUrl}
            onChange={(event) => update("externalUrl", event.target.value)}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
          />
        </div>
      </div>

      <fieldset className="space-y-4 rounded-xl border border-border bg-bg-surface p-4">
        <legend className="px-1 text-sm font-semibold">{t("form.schedule")}</legend>
        <p className="text-xs text-text-muted">{t("timeZoneNotice")}</p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor="event-starts-at" className="mb-1.5 block text-sm font-medium">
              {t("form.startsAt")} <span className="text-accent-error">*</span>
            </label>
            <input
              id="event-starts-at"
              type="datetime-local"
              required
              value={values.startsAt}
              onChange={(event) => update("startsAt", event.target.value)}
              className="datetime-input w-full rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
            />
          </div>
          <div>
            <label htmlFor="event-ends-at" className="mb-1.5 block text-sm font-medium">
              {t("form.endsAt")} <span className="text-accent-error">*</span>
            </label>
            <input
              id="event-ends-at"
              type="datetime-local"
              required
              value={values.endsAt}
              onChange={(event) => update("endsAt", event.target.value)}
              className="datetime-input w-full rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
            />
          </div>
          <div>
            <label htmlFor="event-visible-from" className="mb-1.5 block text-sm font-medium">
              {t("form.visibleFromAt")} <span className="text-accent-error">*</span>
            </label>
            <input
              id="event-visible-from"
              type="datetime-local"
              required
              value={values.visibleFromAt}
              onChange={(event) => update("visibleFromAt", event.target.value)}
              className="datetime-input w-full rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
            />
          </div>
          <div>
            <label htmlFor="event-visible-until" className="mb-1.5 block text-sm font-medium">
              {t("form.visibleUntilAt")}
            </label>
            <input
              id="event-visible-until"
              type="datetime-local"
              value={values.visibleUntilAt}
              onChange={(event) => update("visibleUntilAt", event.target.value)}
              className="datetime-input w-full rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
            />
          </div>
        </div>
      </fieldset>

      <div>
        <div id="event-content-label" className="mb-1.5 text-sm font-medium">
          {t("form.content")} <span className="text-accent-error">*</span>
        </div>
        <div aria-labelledby="event-content-label">
          <RichTextEditor
            value={values.contentMarkdown}
            onChange={(contentMarkdown) => update("contentMarkdown", contentMarkdown)}
            placeholder={t("form.contentPlaceholder")}
          />
        </div>
        <p className="mt-1 text-xs text-text-muted">
          {t("form.contentLength", { count: richContentVisibleLength(values.contentMarkdown), max: 12000 })}
        </p>
      </div>

      <div className="flex justify-end gap-3">
        <Button type="button" variant="ghost" onClick={onCancel} disabled={submitting}>
          {tc("cancel")}
        </Button>
        <Button type="submit" disabled={submitting || categoryOptions.loading || Boolean(categoryOptions.error)}>
          {submitting ? tc("processing") : submitLabel}
        </Button>
      </div>
    </form>
  );
}
