"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { EventCategoryBadge, EventPhaseBadge } from "@/components/events/EventBadges";
import { apiFetch, ApiError } from "@/lib/api-client";
import { formatEventDateRange } from "@/lib/event-date";
import type { EventPublicDetail } from "@/types/event";

function getSafeExternalUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    if (
      (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
      !parsed.hostname ||
      parsed.username ||
      parsed.password
    ) {
      return null;
    }
    return parsed.toString();
  } catch {
    return null;
  }
}

export default function EventDetailPage() {
  const params = useParams<{ id?: string | string[] }>();
  const eventId = typeof params.id === "string" ? params.id : "";
  const t = useTranslations("events");
  const tc = useTranslations("common");
  const locale = useLocale();
  const [event, setEvent] = useState<EventPublicDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchEvent = useCallback(async () => {
    if (!eventId) {
      setLoading(false);
      setError(t("notFound"));
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const response = await apiFetch<EventPublicDetail>(`/api/learn/events/${encodeURIComponent(eventId)}`);
      setEvent(response);
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.status === 404) {
        setError(t("notFound"));
      } else {
        setError(requestError instanceof ApiError ? requestError.message : t("loadFailed"));
      }
    } finally {
      setLoading(false);
    }
  }, [eventId, t]);

  useEffect(() => {
    void fetchEvent();
  }, [fetchEvent]);

  const externalUrl = useMemo(() => getSafeExternalUrl(event?.external_url ?? null), [event?.external_url]);

  if (loading) {
    return (
      <div className="mx-auto max-w-4xl space-y-5">
        <div className="h-5 w-20 animate-pulse rounded bg-bg-surface" />
        <div className="h-12 w-2/3 animate-pulse rounded bg-bg-surface" />
        <div className="h-96 animate-pulse rounded-xl border border-border bg-bg-surface" />
      </div>
    );
  }

  if (error || !event) {
    return (
      <div className="mx-auto max-w-4xl space-y-5 py-12 text-center">
        <p className="text-text-muted">{error ?? t("notFound")}</p>
        <div className="flex justify-center gap-3">
          <button type="button" onClick={() => void fetchEvent()} className="text-sm font-medium text-accent-primary hover:underline">
            {tc("retry")}
          </button>
          <Link href="/events" className="text-sm font-medium text-text-secondary hover:text-text-primary">
            {t("backToEvents")}
          </Link>
        </div>
      </div>
    );
  }

  return (
    <article className="mx-auto max-w-4xl space-y-6">
      <Link href="/events" className="inline-flex text-sm text-text-muted hover:text-text-primary">
        {`← ${t("backToEvents")}`}
      </Link>

      <header className="rounded-xl border border-border bg-bg-surface p-6 sm:p-8">
        <div className="flex flex-wrap items-center gap-2">
          <EventCategoryBadge category={event.category} labels={event.category_labels} />
          <EventPhaseBadge phase={event.event_phase} />
        </div>
        <h1 className="mt-4 text-3xl font-bold tracking-tight">{event.title}</h1>
        {event.summary && <p className="mt-3 text-lg text-text-secondary">{event.summary}</p>}

        <dl className="mt-6 grid gap-4 border-t border-border pt-5 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-text-muted">{t("schedule")}</dt>
            <dd className="mt-1 text-text-primary">{formatEventDateRange(event.starts_at, event.ends_at, locale)}</dd>
          </div>
          {event.location && (
            <div>
              <dt className="text-text-muted">{t("location")}</dt>
              <dd className="mt-1 text-text-primary">{event.location}</dd>
            </div>
          )}
          {externalUrl && (
            <div>
              <dt className="text-text-muted">{t("externalUrl")}</dt>
              <dd className="mt-1">
                <a
                  href={externalUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="font-medium text-accent-primary hover:underline"
                >
                  {t("openExternalLink")}
                </a>
              </dd>
            </div>
          )}
        </dl>
        <p className="mt-5 text-xs text-text-muted">{t("timeZoneNotice")}</p>
      </header>

      <section aria-labelledby="event-content-heading" className="rounded-xl border border-border bg-bg-surface p-6 sm:p-8">
        <h2 id="event-content-heading" className="sr-only">{t("content")}</h2>
        <MarkdownRenderer content={event.content_markdown} />
      </section>
    </article>
  );
}
