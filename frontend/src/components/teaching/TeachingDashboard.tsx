"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { ApiError } from "@/lib/api-client";
import { educationItems, getEducationClasses } from "@/components/teaching/education-api";
import type { EducationClassSummary } from "@/types/education";

const metricKeys = ["draft", "published", "archived"] as const;

export function TeachingDashboard() {
  const t = useTranslations("education");
  const tc = useTranslations("common");
  const [classes, setClasses] = useState<EducationClassSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadDashboard = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setClasses(educationItems(await getEducationClasses()));
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setLoading(false);
    }
  }, [tc]);

  useEffect(() => {
    void loadDashboard();
  }, [loadDashboard]);

  if (loading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4" aria-busy="true">
        {metricKeys.map((key) => <div key={key} className="h-28 animate-pulse rounded-xl border border-border bg-bg-surface" />)}
      </div>
    );
  }

  if (error) {
    return (
      <section className="rounded-xl border border-border bg-bg-surface p-6 text-center" aria-labelledby="teaching-dashboard-error">
        <h2 id="teaching-dashboard-error" className="text-lg font-semibold">{t("dashboard.loadFailed")}</h2>
        <p className="mt-2 text-sm text-text-secondary">{error}</p>
        <button type="button" onClick={() => void loadDashboard()} className="mt-4 text-sm font-medium text-accent-primary hover:underline">{tc("retry")}</button>
      </section>
    );
  }

  return (
    <section aria-labelledby="teaching-dashboard-title" className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 id="teaching-dashboard-title" className="text-2xl font-bold">{t("dashboard.title")}</h1>
          <p className="mt-1 text-sm text-text-secondary">{t("dashboard.description")}</p>
        </div>
        <Link href="/teaching/classes" className="text-sm font-medium text-accent-primary hover:underline">{t("dashboard.manageClasses")}</Link>
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {metricKeys.map((key) => (
          <div key={key} className="rounded-xl border border-border bg-bg-surface p-4">
            <p className="text-sm text-text-secondary">{t(`dashboard.metricLabels.${key}`)}</p>
            <p className="mt-2 text-3xl font-semibold tabular-nums text-text-primary">{classes.filter((item) => item.status === key).length}</p>
          </div>
        ))}
      </div>
      <p className="text-xs text-text-muted">{t("dashboard.serverAuthority")}</p>
    </section>
  );
}
