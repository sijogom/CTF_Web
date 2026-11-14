"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";
import { Plus, Tags } from "lucide-react";
import { Link } from "@/i18n/navigation";
import { buttonVariants } from "@/components/ui/Button";
import { ListPagination } from "@/components/ui/ListPagination";
import { useEventCategories } from "@/components/events/useEventCategories";
import {
  EventCategoryBadge,
  EventPhaseBadge,
  EventStatusBadge,
} from "@/components/events/EventBadges";
import { apiFetch, ApiError } from "@/lib/api-client";
import { formatEventDateRange } from "@/lib/event-date";
import {
  eventCategoryLabel,
  readEventCategory,
  EVENT_STATUSES,
  type EventAdminDetail,
  type EventCategory,
  type EventListResponse,
  type EventStatus,
} from "@/types/event";
import { readEnum, readPositiveInt, writeUrlQuery } from "@/lib/url-query";
import { parseListPage } from "@/lib/list-response.mjs";
import { eventCategoryManagementEnabled } from "@/lib/event-category-release-stage.mjs";

const PAGE_SIZE = 20;
const ADMIN_EVENT_STATUSES = ["all", ...EVENT_STATUSES] as const;

export default function AdminEventsPage() {
  const t = useTranslations("events");
  const ta = useTranslations("admin");
  const tc = useTranslations("common");
  const locale = useLocale();
  const categoryOptions = useEventCategories(true);
  const searchParams = useSearchParams();
  const [events, setEvents] = useState<EventAdminDetail[]>([]);
  const [statusFilter, setStatusFilter] = useState<EventStatus | "all">(() => (
    readEnum(searchParams.get("status"), ADMIN_EVENT_STATUSES, "all")
  ));
  const [categoryFilter, setCategoryFilter] = useState<EventCategory | "all">(() => (
    readEventCategory(searchParams.get("category"))
  ));
  const [page, setPage] = useState(() => readPositiveInt(searchParams.get("page")));
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);

  const syncFromUrl = useCallback(() => {
    const params = new URLSearchParams(window.location.search);
    setStatusFilter(readEnum(params.get("status"), ADMIN_EVENT_STATUSES, "all"));
    setCategoryFilter(readEventCategory(params.get("category")));
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
      if (statusFilter !== "all") query.set("status", statusFilter);
      if (categoryFilter !== "all") query.set("category", categoryFilter);

      const rawResponse = await apiFetch<unknown>(
        `/api/learn/admin/events?${query.toString()}`,
        { signal: controller.signal },
      );
      if (controller.signal.aborted || requestId !== requestIdRef.current) return;
      const response = parseListPage<EventAdminDetail>(rawResponse, page, PAGE_SIZE) as EventListResponse<EventAdminDetail>;
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
  }, [categoryFilter, page, statusFilter, t]);

  useEffect(() => {
    void fetchEvents();
  }, [fetchEvents]);

  const changeStatus = (nextStatus: EventStatus | "all") => {
    setStatusFilter(nextStatus);
    setPage(1);
    writeUrlQuery({ status: nextStatus === "all" ? null : nextStatus, page: null }, "push");
  };

  const changeCategory = (nextCategory: EventCategory | "all") => {
    setCategoryFilter(nextCategory);
    setPage(1);
    writeUrlQuery({ category: nextCategory === "all" ? null : nextCategory, page: null }, "push");
  };

  const changePage = (nextPage: number) => {
    setPage(nextPage);
    writeUrlQuery({ page: nextPage === 1 ? null : nextPage }, "push");
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold">{ta("eventManagement")}</h1>
          <p className="mt-1 text-sm text-text-secondary">{t("adminDescription")}</p>
        </div>
        <div className="flex flex-wrap gap-2">{eventCategoryManagementEnabled() && <Link href="/admin/events/categories" className={buttonVariants({ variant: "secondary", className: "h-11 shrink-0 gap-2 whitespace-nowrap" })}><Tags size={16} aria-hidden="true" />{t("manageCategories")}</Link>}<Link href="/admin/events/new" className={buttonVariants({ className: "h-11 shrink-0 gap-2 whitespace-nowrap" })}><Plus size={16} aria-hidden="true" />{t("createEvent")}</Link></div>
      </div>

      <div className="flex flex-col gap-4 border-b border-border pb-4">
        <fieldset>
          <legend className="mb-2 text-sm font-medium text-text-secondary">{t("statusFilterLabel")}</legend>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => changeStatus("all")}
              aria-pressed={statusFilter === "all"}
              className={`rounded-lg px-3 py-2 text-sm font-medium ${statusFilter === "all" ? "bg-accent-primary text-white" : "hover:bg-bg-elevated text-text-secondary"}`}
            >
              {t("allStatuses")}
            </button>
            {EVENT_STATUSES.map((status) => (
              <button
                key={status}
                type="button"
                onClick={() => changeStatus(status)}
                aria-pressed={statusFilter === status}
                className={`rounded-lg px-3 py-2 text-sm font-medium ${statusFilter === status ? "bg-accent-primary text-white" : "hover:bg-bg-elevated text-text-secondary"}`}
              >
                {t(`statuses.${status}`)}
              </button>
            ))}
          </div>
        </fieldset>
        <div className="min-w-0 sm:w-full sm:max-w-sm">
          <label htmlFor="admin-event-category-filter" className="mb-1.5 block text-sm font-medium text-text-secondary">{t("form.category")}</label>
          <select id="admin-event-category-filter" value={categoryFilter} onChange={(event) => changeCategory(event.target.value)} disabled={categoryOptions.loading || Boolean(categoryOptions.error)} className="min-h-11 w-full min-w-0 rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm text-text-primary">
            <option value="all">{t("allCategories")}</option>
            {categoryFilter !== "all" && !categoryOptions.categories.some((item) => item.key === categoryFilter) && <option value={categoryFilter} disabled>{categoryOptions.loading ? tc("loading") : categoryFilter}</option>}
            {categoryOptions.categories.map((item) => <option key={item.key} value={item.key}>{eventCategoryLabel(item.labels, locale, item.key)}</option>)}
          </select>
        </div>
      </div>

      {categoryOptions.error && <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-accent-error"><span>{categoryOptions.error}</span><button type="button" onClick={() => void categoryOptions.reload()} className="underline">{tc("retry")}</button></div>}

      {loading && (
        <div className="space-y-3">
          {Array.from({ length: 4 }).map((_, index) => (
            <div key={index} className="h-24 animate-pulse rounded-xl border border-border bg-bg-surface" />
          ))}
        </div>
      )}

      {!loading && error && (
        <div className="rounded-xl border border-border bg-bg-surface px-6 py-12 text-center">
          <p className="text-text-muted">{error}</p>
          <button type="button" onClick={() => void fetchEvents()} className="mt-3 text-sm font-medium text-accent-primary hover:underline">
            {tc("retry")}
          </button>
        </div>
      )}

      {!loading && !error && events.length === 0 && (
        <div className="rounded-xl border border-dashed border-border px-6 py-14 text-center">
          <h2 className="text-lg font-semibold">{t("noAdminEvents")}</h2>
          <p className="mt-2 text-sm text-text-secondary">{t("noAdminEventsDescription")}</p>
        </div>
      )}

      {!loading && !error && events.length > 0 && (
        <div className="grid gap-3">
          {events.map((event) => (
            <div key={event.id} className="flex flex-col gap-4 rounded-xl border border-border bg-bg-surface p-5 lg:flex-row lg:items-center lg:justify-between">
              <div className="min-w-0 flex-1 space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="truncate font-semibold">{event.title}</h2>
                  <EventStatusBadge status={event.status} />
                  <EventCategoryBadge category={event.category} labels={event.category_labels} />
                  <EventPhaseBadge phase={event.event_phase} />
                </div>
                <p className="text-sm text-text-muted">{formatEventDateRange(event.starts_at, event.ends_at, locale)}</p>
                {event.archive_reason && <p className="text-xs text-accent-error">{t("archivedReasonRecorded")}</p>}
              </div>
              <Link
                href={`/admin/events/${event.id}`}
                className={buttonVariants({ variant: "secondary", size: "sm", className: "h-11 shrink-0 self-start whitespace-nowrap lg:self-auto" })}
              >
                {tc("edit")}
              </Link>
            </div>
          ))}
        </div>
      )}

      {!loading && !error && (
        <ListPagination page={page} pageSize={PAGE_SIZE} total={total} onPageChange={changePage} />
      )}
    </div>
  );
}
