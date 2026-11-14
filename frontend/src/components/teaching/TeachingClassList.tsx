"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { ApiError, apiFetch } from "@/lib/api-client";
import { ClassForm } from "@/components/teaching/ClassForm";
import { EDUCATION_API, getAllEducationClasses } from "@/components/teaching/education-api";
import { formatEducationDate } from "@/components/teaching/education-format";
import type { EducationClassSummary, EducationClassWritePayload } from "@/types/education";

const STATUS_CLASS: Record<string, string> = {
  draft: "bg-bg-elevated text-text-secondary",
  published: "bg-accent-success/10 text-accent-success",
  archived: "bg-accent-warning/10 text-accent-warning",
};

export function TeachingClassList() {
  const t = useTranslations("education");
  const tc = useTranslations("common");
  const locale = useLocale();
  const [classes, setClasses] = useState<EducationClassSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [archiveTarget, setArchiveTarget] = useState<EducationClassSummary | null>(null);
  const [archiveReason, setArchiveReason] = useState("");
  const [archiving, setArchiving] = useState(false);
  const [restoringId, setRestoringId] = useState<string | null>(null);
  const requestGenerationRef = useRef(0);

  const loadClasses = useCallback(async () => {
    const generation = ++requestGenerationRef.current;
    setLoading(true);
    setError(null);
    try {
      const nextClasses = await getAllEducationClasses();
      if (generation === requestGenerationRef.current) setClasses(nextClasses);
    } catch (requestError) {
      if (generation === requestGenerationRef.current) setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      if (generation === requestGenerationRef.current) setLoading(false);
    }
  }, [tc]);

  useEffect(() => {
    void loadClasses();
    return () => { requestGenerationRef.current += 1; };
  }, [loadClasses]);

  const createClass = async (payload: EducationClassWritePayload) => {
    await apiFetch<EducationClassSummary>(`${EDUCATION_API}/classes`, { method: "POST", body: payload });
    setShowCreate(false);
    await loadClasses();
  };

  const archiveClass = async () => {
    if (!archiveTarget || !archiveReason.trim()) return;
    setArchiving(true);
    try {
      await apiFetch(`${EDUCATION_API}/classes/${archiveTarget.id}/archive`, {
        method: "POST",
        body: { reason: archiveReason.trim() },
      });
      setArchiveTarget(null);
      setArchiveReason("");
      await loadClasses();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setArchiving(false);
    }
  };

  const restoreClass = async (classItem: EducationClassSummary) => {
    setRestoringId(classItem.id);
    setError(null);
    try {
      await apiFetch(`${EDUCATION_API}/classes/${classItem.id}/restore`, {
        method: "POST",
        body: { status: "draft" },
      });
      await loadClasses();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setRestoringId(null);
    }
  };

  return (
    <section className="space-y-6" aria-labelledby="teaching-classes-title">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 id="teaching-classes-title" className="text-2xl font-bold">{t("classes.title")}</h1>
          <p className="mt-1 text-sm text-text-secondary">{t("classes.description")}</p>
        </div>
        {!showCreate && <Button type="button" onClick={() => setShowCreate(true)} aria-expanded={false} aria-controls="teaching-class-create-form">{t("classes.create")}</Button>}
      </div>

      {showCreate && (
        <section id="teaching-class-create-form" className="rounded-xl border border-border bg-bg-surface p-5" aria-labelledby="teaching-class-create-title">
          <h2 id="teaching-class-create-title" className="mb-4 text-lg font-semibold">{t("classes.create")}</h2>
          <ClassForm onSubmit={createClass} onCancel={() => setShowCreate(false)} submitLabel={t("classes.create")} />
        </section>
      )}

      {loading && <div className="space-y-3" aria-busy="true">{Array.from({ length: 3 }).map((_, index) => <div key={index} className="h-24 animate-pulse rounded-xl border border-border bg-bg-surface" />)}</div>}

      {!loading && error && (
        <div role="alert" className="rounded-xl border border-accent-error/30 bg-accent-error/10 p-5 text-center">
          <p className="text-sm text-accent-error">{error}</p>
          <button type="button" onClick={() => void loadClasses()} className="mt-3 text-sm font-medium text-accent-primary hover:underline">{tc("retry")}</button>
        </div>
      )}

      {!loading && !error && classes.length === 0 && (
        <div className="rounded-xl border border-dashed border-border p-10 text-center">
          <h2 className="text-lg font-semibold">{t("classes.emptyTitle")}</h2>
          <p className="mt-2 text-sm text-text-secondary">{t("classes.emptyDescription")}</p>
        </div>
      )}

      {!loading && !error && classes.length > 0 && (
        <div className="space-y-3">
          {classes.map((classItem) => (
            <article key={classItem.id} className="rounded-xl border border-border bg-bg-surface p-5">
              <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-lg font-semibold text-text-primary"><Link href={`/teaching/classes/${classItem.id}`} className="hover:text-accent-primary">{classItem.title}</Link></h2>
                    <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_CLASS[classItem.status] ?? STATUS_CLASS.draft}`}>{t(`status.${classItem.status}`)}</span>
                  </div>
                  {classItem.summary && <p className="mt-2 line-clamp-2 text-sm text-text-muted">{classItem.summary}</p>}
                  <p className="mt-3 text-xs text-text-muted">{t("classes.schedule", { startsAt: formatEducationDate(classItem.starts_at, locale), endsAt: formatEducationDate(classItem.ends_at, locale) })}</p>
                </div>
                <div className="flex shrink-0 gap-2">
                  <Link href={`/teaching/classes/${classItem.id}`} className="rounded-lg border border-border px-3 py-2 text-sm font-medium text-text-secondary hover:bg-bg-elevated hover:text-text-primary">{tc("edit")}</Link>
                  {classItem.status !== "archived" && <button type="button" onClick={() => setArchiveTarget(classItem)} className="rounded-lg border border-accent-warning/30 px-3 py-2 text-sm font-medium text-accent-warning hover:bg-accent-warning/10">{t("classes.archive")}</button>}
                  {classItem.status === "archived" && <button type="button" onClick={() => void restoreClass(classItem)} disabled={restoringId === classItem.id} className="rounded-lg border border-accent-primary/30 px-3 py-2 text-sm font-medium text-accent-primary hover:bg-accent-primary/10 disabled:opacity-50">{restoringId === classItem.id ? tc("processing") : t("classes.restore")}</button>}
                </div>
              </div>
            </article>
          ))}
        </div>
      )}

      {archiveTarget && (
        <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/50 p-4" role="presentation">
          <div role="alertdialog" aria-modal="true" aria-labelledby="education-archive-title" aria-describedby="education-archive-description" className="w-full max-w-md rounded-xl border border-border bg-bg-primary p-6 shadow-xl">
            <h2 id="education-archive-title" className="text-lg font-semibold">{t("classes.archiveTitle")}</h2>
            <p id="education-archive-description" className="mt-2 text-sm text-text-secondary">{t("classes.archiveDescription", { title: archiveTarget.title })}</p>
            <label htmlFor="education-class-archive-reason" className="mt-4 block text-sm font-medium">{t("classes.archiveReason")}</label>
            <textarea id="education-class-archive-reason" rows={3} maxLength={1000} value={archiveReason} onChange={(event) => setArchiveReason(event.target.value)} className="mt-1 w-full resize-y rounded-lg border border-border bg-bg-surface px-3 py-2 text-sm focus:border-accent-primary focus:outline-none" />
            <div className="mt-5 flex justify-end gap-3">
              <Button type="button" variant="ghost" onClick={() => { setArchiveTarget(null); setArchiveReason(""); }} disabled={archiving}>{tc("cancel")}</Button>
              <Button type="button" variant="secondary" onClick={() => void archiveClass()} disabled={archiving || !archiveReason.trim()}>{archiving ? tc("processing") : t("classes.archive")}</Button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
