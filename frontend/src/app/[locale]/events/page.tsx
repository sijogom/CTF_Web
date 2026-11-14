"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";
import { Link } from "@/i18n/navigation";
import { apiFetch, ApiError } from "@/lib/api-client";
import { formatEventDateRange } from "@/lib/event-date";
import { EventCategoryBadge, EventPhaseBadge } from "@/components/events/EventBadges";
import { ListPagination } from "@/components/ui/ListPagination";
import { useEventCategories } from "@/components/events/useEventCategories";
import {
  eventCategoryLabel,
  readEventCategory,
  type EventCategory,
  type EventListResponse,
  type EventSummary,
} from "@/types/event";
import { readPositiveInt, writeUrlQuery } from "@/lib/url-query";
import { parseListPage } from "@/lib/list-response.mjs";

const PAGE_SIZE = 20;

export default function EventsPage() {
  const t = useTranslations("events");
  const tc = useTranslations("common");
  const locale = useLocale();
  const categoryOptions = useEventCategories();
  const searchParams = useSearchParams();
  const [events, setEvents] = useState<EventSummary[]>([]);
  const [category, setCategory] = useState<EventCategory | "all">(() => (
    readEventCategory(searchParams.get("category"))
  ));
  const [includeEnded, setIncludeEnded] = useState(() => searchParams.get("include_ended") === "1");
  const [page, setPage] = useState(() => readPositiveInt(searchParams.get("page")));
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);

  const syncFromUrl = useCallback(() => {
    const params = new URLSearchParams(window.location.search);
    setCategory(readEventCategory(params.get("category")));
    setIncludeEnded(params.get("include_ended") === "1");
    setPage(readPositiveInt(params.get("page")));
  }, []);

  useEffect(() => {
    window.addEventListener("popstate", syncFromUrl);
    return () => {
      window.removeEventListener("popstate", syncFromUrl);
      controllerRef.current?.abort();
    };
  }, [syncFromUrl]);

  const fetchEvents = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError(null);
    try {
      const query = new URLSearchParams({
        page: String(page),
        size: String(PAGE_SIZE),
      });
      if (category !== "all") query.set("category", category);
      if (includeEnded) query.set("include_ended", "true");

      const rawResponse = await apiFetch<unknown>(
        `/api/learn/events?${query.toString()}`,
        { signal: controller.signal },
      );
      if (controller.signal.aborted || requestId !== requestIdRef.current) return;
      const response = parseListPage<EventSummary>(rawResponse, page, PAGE_SIZE) as EventListResponse;
      const responseTotalPages = Math.max(1, Math.ceil(response.total / PAGE_SIZE));
      if (page > responseTotalPages) {
        setPage(responseTotalPages);
        writeUrlQuery({ page: responseTotalPages === 1 ? null : responseTotalPages });
        return;
      }
      setEvents(response.items);
      setTotal(response.total);
    } catch (requestError) {
      if (controller.signal.aborted || requestId !== requestIdRef.current) return;
      setError(requestError instanceof ApiError ? requestError.message : t("loadFailed"));
    } finally {
      if (!controller.signal.aborted && requestId === requestIdRef.current) setLoading(false);
    }
  }, [category, includeEnded, page, t]);

  useEffect(() => {
    void fetchEvents();
  }, [fetchEvents]);

  const changeCategory = (nextCategory: EventCategory | "all") => {
    setCategory(nextCategory);
    setPage(1);
    writeUrlQuery({ category: nextCategory === "all" ? null : nextCategory, page: null }, "push");
  };

  const changePage = (nextPage: number) => {
    setPage(nextPage);
    writeUrlQuery({ page: nextPage === 1 ? null : nextPage }, "push");
  };

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div>
        <h1 className="text-3xl font-bold">{t("title")}</h1>
        <p className="mt-2 text-text-secondary">{t("description")}</p>
        <p className="mt-3 text-xs text-text-muted">{t("timeZoneNotice")}</p>
      </div>

      <div className="flex flex-col gap-4 border-b border-border pb-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0 sm:w-full sm:max-w-sm">
          <label htmlFor="public-event-category-filter" className="mb-1.5 block text-sm font-medium text-text-secondary">{t("form.category")}</label>
          <select id="public-event-category-filter" value={category} onChange={(event) => changeCategory(event.target.value)} disabled={categoryOptions.loading || Boolean(categoryOptions.error)} className="min-h-11 w-full min-w-0 rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm text-text-primary">
            <option value="all">{t("allCategories")}</option>
            {category !== "all" && !categoryOptions.categories.some((item) => item.key === category) && <option value={category} disabled>{categoryOptions.loading ? tc("loading") : category}</option>}
            {categoryOptions.categories.map((item) => <option key={item.key} value={item.key}>{eventCategoryLabel(item.labels, locale, item.key)}</option>)}
          </select>
        </div>
        <label className="flex min-h-11 shrink-0 items-center gap-2 text-sm text-text-secondary">
          <input
            type="checkbox"
            checked={includeEnded}
            onChange={(event) => {
              setIncludeEnded(event.target.checked);
              setPage(1);
              writeUrlQuery({ include_ended: event.target.checked, page: null }, "push");
            }}
            className="h-4 w-4 rounded border-border accent-accent-primary"
          />
          {t("includeEnded")}
        </label>
      </div>

      {categoryOptions.error && <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-accent-error"><span>{categoryOptions.error}</span><button type="button" onClick={() => void categoryOptions.reload()} className="underline">{tc("retry")}</button></div>}

      {loading && (
        <div className="grid gap-4 md:grid-cols-2">
          {Array.from({ length: 4 }).map((_, index) => (
            <div key={index} className="h-52 animate-pulse rounded-xl border border-border bg-bg-surface" />
          ))}
        </div>
      )}

      {!loading && error && (
        <div className="flex flex-col items-center gap-4 rounded-xl border border-border bg-bg-surface px-6 py-14 text-center">
          <p className="text-text-muted">{error}</p>
          <button type="button" onClick={() => void fetchEvents()} className="text-sm font-medium text-accent-primary hover:underline">
            {tc("retry")}
          </button>
        </div>
      )}

      {!loading && !error && events.length === 0 && (
        <div className="rounded-xl border border-dashed border-border px-6 py-16 text-center">
          <h2 className="text-lg font-semibold">{t("noEvents")}</h2>
          <p className="mt-2 text-sm text-text-secondary">{t("noEventsDescription")}</p>
        </div>
      )}

      {!loading && !error && events.length > 0 && (
        <div className="grid gap-4 md:grid-cols-2">
          {events.map((event) => (
            <Link
              key={event.id}
              href={`/events/${event.id}`}
              className="group flex min-h-52 flex-col rounded-xl border border-border bg-bg-surface p-5 transition-colors hover:border-accent-primary/50 hover:bg-bg-elevated"
            >
              <div className="flex flex-wrap items-center gap-2">
                <EventCategoryBadge category={event.category} labels={event.category_labels} />
                <EventPhaseBadge phase={event.event_phase} />
              </div>
              <h2 className="mt-4 text-xl font-semibold text-text-primary group-hover:text-accent-primary">{event.title}</h2>
              {event.summary && <p className="mt-2 line-clamp-3 text-sm text-text-secondary">{event.summary}</p>}
              <div className="mt-auto space-y-1 pt-5 text-sm text-text-muted">
                <p>{formatEventDateRange(event.starts_at, event.ends_at, locale)}</p>
                {event.location && <p>{t("locationValue", { location: event.location })}</p>}
              </div>
            </Link>
          ))}
        </div>
      )}

      {!loading && !error && (
        <ListPagination page={page} pageSize={PAGE_SIZE} total={total} onPageChange={changePage} />
      )}
    </div>
  );
}
