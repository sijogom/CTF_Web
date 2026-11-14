"use client";

import { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";
import { useLocale } from "next-intl";
import { Link } from "@/i18n/navigation";
import { apiFetch, ApiError } from "@/lib/api-client";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import type { CompetitionSummary as Competition } from "@/types/competition";
import { isFixedRosterCompetition } from "@/lib/competition-presentation";

const STATUS_COLORS: Record<string, string> = {
  upcoming: "bg-accent-primary/10 text-accent-primary",
  ongoing: "bg-accent-success/10 text-accent-success",
  ended: "bg-text-muted/10 text-text-muted",
};

const STATUS_KEYS: Record<string, string> = {
  upcoming: "upcoming",
  ongoing: "ongoing",
  ended: "ended",
};

type StatusFilter = "all" | "upcoming" | "ongoing" | "ended";

export default function CTFListPage() {
  const t = useTranslations("ctf");
  const tc = useTranslations("common");
  const locale = useLocale();
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [competitions, setCompetitions] = useState<Competition[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchCompetitions = useCallback((status: StatusFilter) => {
    setLoading(true);
    setError(null);
    const query = status === "all" ? "" : `?status=${status}`;
    apiFetch<Competition[]>(`/api/ctf/competitions${query}`)
      .then(setCompetitions)
      .catch((err) => setError(err instanceof ApiError ? err.message : t("loadFailed")))
      .finally(() => setLoading(false));
  }, [t]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 데이터 페칭 트리거
    fetchCompetitions(filter);
  }, [filter, fetchCompetitions]);

  const getStatusLabel = (status: string) => {
    const key = STATUS_KEYS[status];
    return key ? t(key) : status;
  };

  const getFilterLabel = (key: StatusFilter) => {
    if (key === "all") return tc("all");
    return getStatusLabel(key);
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">{t("title")}</h1>
        <p className="mt-2 text-text-secondary">{t("description")}</p>
      </div>

      {/* 필터 */}
      <div className="flex gap-2">
        {(["all", "upcoming", "ongoing", "ended"] as StatusFilter[]).map((key) => (
          <button
            key={key}
            onClick={() => setFilter(key)}
            className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
              filter === key
                ? "bg-accent-primary text-white"
                : "bg-bg-surface text-text-secondary hover:text-text-primary"
            }`}
          >
            {getFilterLabel(key)}
          </button>
        ))}
      </div>

      {/* 대회 목록 */}
      {loading ? (
        <div className="flex justify-center py-20">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
        </div>
      ) : error ? (
        <div className="py-20 text-center text-status-error">{error}</div>
      ) : competitions.length === 0 ? (
        <div className="py-16 text-center text-text-muted">
          {tc("noData")}
        </div>
      ) : (
        <div className="space-y-4">
          {competitions.map((comp) => {
            const colorClass = STATUS_COLORS[comp.status] ?? STATUS_COLORS.upcoming;
            const startDate = new Date(comp.start_at);
            const endDate = new Date(comp.end_at);
            const localeMap: Record<string, string> = { ko: "ko-KR", en: "en-US", ja: "ja-JP", zh: "zh-CN" };
            const dateLocale = localeMap[locale] || "ko-KR";

            return (
              <Link
                key={comp.id}
                href={`/ctf/${comp.id}`}
                className="group block overflow-hidden rounded-xl border border-border bg-bg-surface transition-all hover:border-accent-primary hover:shadow-lg"
              >
                {comp.banner_url && (
                  <div className="relative h-40 w-full overflow-hidden bg-bg-primary">
                    <img
                      src={comp.banner_url}
                      alt={comp.title}
                      loading="lazy"
                      className="h-full w-full object-cover transition-transform group-hover:scale-105"
                    />
                    <div className="absolute inset-0 bg-gradient-to-t from-bg-surface/80 to-transparent" />
                  </div>
                )}
                <div className="flex items-start justify-between p-6">
                  <div className="flex-1">
                    <div className="mb-2 flex items-center gap-2">
                      <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${colorClass}`}>
                        {getStatusLabel(comp.status)}
                      </span>
                      <span className="rounded-full bg-bg-primary px-2.5 py-0.5 text-xs text-text-muted">
                        {comp.mode === "team" ? t("teamMode") : t("individualMode")}
                      </span>
                    </div>
                    <h3 className="text-xl font-semibold group-hover:text-accent-primary">
                      {comp.title}
                    </h3>
                    {comp.description && <MarkdownRenderer content={comp.description} />}
                  </div>
                  <div className="ml-6 text-right text-sm text-text-muted">
                    <p>{startDate.toLocaleDateString(dateLocale)}</p>
                    <p>
                      {startDate.toLocaleTimeString(dateLocale, { hour: "2-digit", minute: "2-digit" })}
                      {" ~ "}
                      {endDate.toLocaleTimeString(dateLocale, { hour: "2-digit", minute: "2-digit" })}
                    </p>
                    <p className="mt-2 font-medium">{t(isFixedRosterCompetition(comp) ? "registeredTeamCount" : "participantCount", { count: comp.participant_count })}</p>
                  </div>
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
