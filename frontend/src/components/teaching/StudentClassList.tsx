"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { ApiError } from "@/lib/api-client";
import { getAllEducationClasses } from "@/components/teaching/education-api";
import { formatEducationDate } from "@/components/teaching/education-format";
import type { EducationClassSummary } from "@/types/education";

export function StudentClassList() {
  const t = useTranslations("education");
  const tc = useTranslations("common");
  const locale = useLocale();
  const [classes, setClasses] = useState<EducationClassSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestGenerationRef = useRef(0);
  const loadClasses = useCallback(async () => {
    const generation = ++requestGenerationRef.current;
    setLoading(true); setError(null);
    try {
      const nextClasses = await getAllEducationClasses();
      if (generation === requestGenerationRef.current) setClasses(nextClasses);
    }
    catch (requestError) {
      if (generation === requestGenerationRef.current) setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    }
    finally { if (generation === requestGenerationRef.current) setLoading(false); }
  }, [tc]);
  useEffect(() => {
    void loadClasses();
    return () => { requestGenerationRef.current += 1; };
  }, [loadClasses]);
  return <section className="mx-auto max-w-6xl space-y-6" aria-labelledby="student-classes-title"><div><h1 id="student-classes-title" className="text-3xl font-bold">{t("studentClasses.title")}</h1><p className="mt-2 text-text-secondary">{t("studentClasses.description")}</p></div>{loading && <div className="grid gap-4 md:grid-cols-2" aria-busy="true">{Array.from({ length: 4 }).map((_, index) => <div key={index} className="h-52 animate-pulse rounded-xl border border-border bg-bg-surface" />)}</div>}{!loading && error && <div className="rounded-xl border border-accent-error/30 bg-accent-error/10 p-6 text-center"><p role="alert" className="text-sm text-accent-error">{error}</p><button type="button" onClick={() => void loadClasses()} className="mt-3 text-sm font-medium text-accent-primary hover:underline">{tc("retry")}</button></div>}{!loading && !error && classes.length === 0 && <div className="rounded-xl border border-dashed border-border p-12 text-center"><h2 className="text-lg font-semibold">{t("studentClasses.emptyTitle")}</h2><p className="mt-2 text-sm text-text-secondary">{t("studentClasses.emptyDescription")}</p></div>}{!loading && !error && classes.length > 0 && <div className="grid gap-4 md:grid-cols-2">{classes.map((classItem) => <Link key={classItem.id} href={`/learn/classes/${classItem.id}`} className="group rounded-xl border border-border bg-bg-surface p-5 transition-colors hover:border-accent-primary/50 hover:bg-bg-elevated"><h2 className="text-xl font-semibold text-text-primary group-hover:text-accent-primary">{classItem.title}</h2>{classItem.summary && <p className="mt-3 line-clamp-3 text-sm text-text-secondary">{classItem.summary}</p>}<p className="mt-5 text-xs text-text-muted">{t("studentClasses.schedule", { startsAt: formatEducationDate(classItem.starts_at, locale), endsAt: formatEducationDate(classItem.ends_at, locale) })}</p></Link>)}</div>}<p className="text-xs text-text-muted">{t("studentClasses.serverAuthority")}</p></section>;
}
