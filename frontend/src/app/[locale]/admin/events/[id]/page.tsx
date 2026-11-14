"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { useRouter, Link } from "@/i18n/navigation";
import { useLocale, useTranslations } from "next-intl";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { Button } from "@/components/ui/Button";
import { EventForm, createEventFormValues } from "@/components/events/EventForm";
import {
  EventCategoryBadge,
  EventPhaseBadge,
  EventStatusBadge,
} from "@/components/events/EventBadges";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { apiFetch, ApiError } from "@/lib/api-client";
import { parseListPage } from "@/lib/list-response.mjs";
import { formatEventDateRange, formatEventDateTime } from "@/lib/event-date";
import { useToast } from "@/providers/ToastProvider";
import type {
  EventAdminDetail,
  EventAuditEvent,
  EventListResponse,
  EventWritableStatus,
  EventWritePayload,
} from "@/types/event";

export default function AdminEventDetailPage() {
  const params = useParams<{ id?: string | string[] }>();
  const eventId = typeof params.id === "string" ? params.id : "";
  const router = useRouter();
  const locale = useLocale();
  const t = useTranslations("events");
  const tc = useTranslations("common");
  const { toast } = useToast();
  const [event, setEvent] = useState<EventAdminDetail | null>(null);
  const [audits, setAudits] = useState<EventAuditEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [auditLoading, setAuditLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [archiveReason, setArchiveReason] = useState("");
  const [archiveConfirmOpen, setArchiveConfirmOpen] = useState(false);
  const [archiveSubmitting, setArchiveSubmitting] = useState(false);
  const [restoreSubmitting, setRestoreSubmitting] = useState(false);
  const [restoreStatus, setRestoreStatus] = useState<EventWritableStatus>("draft");

  const refreshAudits = useCallback(async () => {
    if (!eventId) return;
    setAuditLoading(true);
    setAuditError(null);
    try {
      const rawResponse = await apiFetch<unknown>(
        `/api/learn/admin/events/${encodeURIComponent(eventId)}/audit-events?size=50`,
      );
      const response = parseListPage<EventAuditEvent>(rawResponse, 1, 50) as EventListResponse<EventAuditEvent>;
      setAudits(response.items);
    } catch (requestError) {
      setAuditError(requestError instanceof ApiError ? requestError.message : t("auditLoadFailed"));
    } finally {
      setAuditLoading(false);
    }
  }, [eventId, t]);

  const fetchEvent = useCallback(async () => {
    if (!eventId) {
      setError(t("notFound"));
      setLoading(false);
      setAuditLoading(false);
      return;
    }

    setLoading(true);
    setAuditLoading(true);
    setError(null);
    setAuditError(null);
    const [eventResult, auditResult] = await Promise.allSettled([
      apiFetch<EventAdminDetail>(`/api/learn/admin/events/${encodeURIComponent(eventId)}`),
      apiFetch<unknown>(
        `/api/learn/admin/events/${encodeURIComponent(eventId)}/audit-events?size=50`,
      ),
    ]);

    if (eventResult.status === "fulfilled") {
      setEvent(eventResult.value);
    } else {
      const requestError = eventResult.reason;
      setError(requestError instanceof ApiError ? requestError.message : t("notFound"));
    }

    if (auditResult.status === "fulfilled") {
      try {
        const auditPage = parseListPage<EventAuditEvent>(auditResult.value, 1, 50);
        setAudits(auditPage.items);
      } catch (requestError) {
        setAuditError(requestError instanceof ApiError ? requestError.message : t("auditLoadFailed"));
      }
    } else {
      const requestError = auditResult.reason;
      setAuditError(requestError instanceof ApiError ? requestError.message : t("auditLoadFailed"));
    }
    setLoading(false);
    setAuditLoading(false);
  }, [eventId, t]);

  useEffect(() => {
    void fetchEvent();
  }, [fetchEvent]);

  const handleUpdate = async (payload: EventWritePayload) => {
    if (!eventId) return;
    const updated = await apiFetch<EventAdminDetail>(`/api/learn/admin/events/${encodeURIComponent(eventId)}`, {
      method: "PATCH",
      body: payload,
    });
    setEvent(updated);
    toast("success", t("saveSuccess"));
    await refreshAudits();
  };

  const openArchiveConfirm = () => {
    setActionError(null);
    if (!archiveReason.trim()) {
      setActionError(t("archiveReasonRequired"));
      return;
    }
    setArchiveConfirmOpen(true);
  };

  const handleArchive = async () => {
    if (!eventId || archiveSubmitting) return;
    setArchiveSubmitting(true);
    setActionError(null);
    try {
      const archived = await apiFetch<EventAdminDetail>(`/api/learn/admin/events/${encodeURIComponent(eventId)}/archive`, {
        method: "POST",
        body: { reason: archiveReason.trim() },
      });
      setEvent(archived);
      setArchiveConfirmOpen(false);
      setArchiveReason("");
      toast("success", t("archiveSuccess"));
      await refreshAudits();
    } catch (requestError) {
      setActionError(requestError instanceof ApiError ? requestError.message : tc("errorOccurred"));
    } finally {
      setArchiveSubmitting(false);
    }
  };

  const handleRestore = async () => {
    if (!eventId || restoreSubmitting) return;
    setRestoreSubmitting(true);
    setActionError(null);
    try {
      const restored = await apiFetch<EventAdminDetail>(`/api/learn/admin/events/${encodeURIComponent(eventId)}/restore`, {
        method: "POST",
        body: { status: restoreStatus },
      });
      setEvent(restored);
      toast("success", t("restoreSuccess"));
      await refreshAudits();
    } catch (requestError) {
      setActionError(requestError instanceof ApiError ? requestError.message : tc("errorOccurred"));
    } finally {
      setRestoreSubmitting(false);
    }
  };

  if (loading) {
    return (
      <div className="mx-auto max-w-3xl space-y-5">
        <div className="h-5 w-32 animate-pulse rounded bg-bg-surface" />
        <div className="h-10 w-1/2 animate-pulse rounded bg-bg-surface" />
        <div className="h-[32rem] animate-pulse rounded-xl border border-border bg-bg-surface" />
      </div>
    );
  }

  if (error || !event) {
    return (
      <div className="mx-auto max-w-3xl space-y-4 py-12 text-center">
        <p className="text-text-muted">{error ?? t("notFound")}</p>
        <div className="flex justify-center gap-3">
          <button type="button" onClick={() => void fetchEvent()} className="text-sm font-medium text-accent-primary hover:underline">
            {tc("retry")}
          </button>
          <Link href="/admin/events" className="text-sm font-medium text-text-secondary hover:text-text-primary">
            {t("backToAdminEvents")}
          </Link>
        </div>
      </div>
    );
  }

  const isArchived = event.status === "archived";

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <Link href="/admin/events" className="inline-flex text-sm text-text-muted hover:text-text-primary">
        {`← ${t("backToAdminEvents")}`}
      </Link>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-2xl font-bold">{event.title}</h1>
            <EventStatusBadge status={event.status} />
            <EventCategoryBadge category={event.category} labels={event.category_labels} />
            <EventPhaseBadge phase={event.event_phase} />
          </div>
          <p className="mt-2 text-sm text-text-secondary">{formatEventDateRange(event.starts_at, event.ends_at, locale)}</p>
        </div>
      </div>

      {actionError && (
        <div role="alert" className="rounded-lg border border-accent-error/30 bg-accent-error/10 px-4 py-3 text-sm text-accent-error">
          {actionError}
        </div>
      )}

      {isArchived ? (
        <>
          <section className="rounded-xl border border-border bg-bg-surface p-6">
            <h2 className="text-lg font-semibold">{t("archivedEvent")}</h2>
            <p className="mt-2 text-sm text-text-secondary">{t("archivedEventDescription")}</p>
            {event.archive_reason && (
              <div className="mt-4 rounded-lg bg-bg-primary p-4 text-sm">
                <p className="text-xs text-text-muted">{t("archiveReason")}</p>
                <p className="mt-1 text-text-primary">{event.archive_reason}</p>
              </div>
            )}
            <div className="mt-5 border-t border-border pt-5">
              <label htmlFor="event-restore-status" className="mb-1.5 block text-sm font-medium">
                {t("restoreStatus")}
              </label>
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
                <select
                  id="event-restore-status"
                  value={restoreStatus}
                  onChange={(changeEvent) => setRestoreStatus(changeEvent.target.value as EventWritableStatus)}
                  className="rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm"
                >
                  <option value="draft">{t("statuses.draft")}</option>
                  <option value="published">{t("statuses.published")}</option>
                </select>
                <Button onClick={() => void handleRestore()} disabled={restoreSubmitting}>
                  {restoreSubmitting ? tc("processing") : t("restoreEvent")}
                </Button>
              </div>
            </div>
          </section>

          <section aria-labelledby="archived-event-content" className="rounded-xl border border-border bg-bg-surface p-6">
            <h2 id="archived-event-content" className="mb-4 text-lg font-semibold">{t("content")}</h2>
            <MarkdownRenderer content={event.content_markdown} />
          </section>
        </>
      ) : (
        <>
          <EventForm
            key={`${event.id}-${event.updated_at}`}
            initialValues={createEventFormValues(event)}
            onSubmit={handleUpdate}
            onCancel={() => router.push("/admin/events")}
            submitLabel={tc("save")}
          />

          <section className="rounded-xl border border-accent-error/30 bg-accent-error/5 p-5">
            <h2 className="text-lg font-semibold text-accent-error">{t("archiveEvent")}</h2>
            <p className="mt-1 text-sm text-text-secondary">{t("archiveDescription")}</p>
            <label htmlFor="event-archive-reason" className="mt-4 block text-sm font-medium">
              {t("archiveReason")} <span className="text-accent-error">*</span>
            </label>
            <textarea
              id="event-archive-reason"
              rows={3}
              maxLength={1000}
              value={archiveReason}
              onChange={(changeEvent) => setArchiveReason(changeEvent.target.value)}
              className="mt-1.5 w-full resize-y rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm focus:border-accent-error focus:outline-none"
              placeholder={t("archiveReasonPlaceholder")}
            />
            <Button type="button" variant="danger" onClick={openArchiveConfirm} className="mt-3">
              {t("archiveEvent")}
            </Button>
          </section>
        </>
      )}

      <section aria-labelledby="event-audit-heading" className="rounded-xl border border-border bg-bg-surface p-6">
        <div className="flex items-center justify-between gap-3">
          <h2 id="event-audit-heading" className="text-lg font-semibold">{t("auditHistory")}</h2>
          <button type="button" onClick={() => void refreshAudits()} className="text-sm font-medium text-accent-primary hover:underline">
            {tc("retry")}
          </button>
        </div>

        {auditLoading && <div className="mt-4 h-20 animate-pulse rounded-lg bg-bg-primary" />}
        {!auditLoading && auditError && <p className="mt-4 text-sm text-accent-error">{auditError}</p>}
        {!auditLoading && !auditError && audits.length === 0 && (
          <p className="mt-4 text-sm text-text-muted">{t("noAuditEvents")}</p>
        )}
        {!auditLoading && !auditError && audits.length > 0 && (
          <ol className="mt-4 divide-y divide-border">
            {audits.map((audit) => (
              <li key={audit.id} className="py-3 first:pt-0 last:pb-0">
                <p className="text-sm font-medium text-text-primary">{t(`auditActions.${audit.action}`)}</p>
                <p className="mt-1 text-xs text-text-muted">
                  {formatEventDateTime(audit.created_at, locale)} · {t("auditActor", { actorId: audit.actor_id.slice(0, 8) })}
                </p>
              </li>
            ))}
          </ol>
        )}
      </section>

      <ConfirmModal
        open={archiveConfirmOpen}
        title={t("archiveConfirmTitle")}
        description={t("archiveConfirmDescription")}
        confirmLabel={archiveSubmitting ? tc("processing") : t("archiveEvent")}
        variant="danger"
        onConfirm={() => void handleArchive()}
        onCancel={() => {
          if (!archiveSubmitting) setArchiveConfirmOpen(false);
        }}
      />
    </div>
  );
}
