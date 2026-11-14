"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { ApiError, apiFetch } from "@/lib/api-client";
import { EDUCATION_API, educationItems, getClassEnrollments } from "@/components/teaching/education-api";
import { formatEducationDate } from "@/components/teaching/education-format";
import type { EducationEnrollment } from "@/types/education";

export function EnrollmentManager({ classId }: { classId: string }) {
  const t = useTranslations("education");
  const tc = useTranslations("common");
  const locale = useLocale();
  const [enrollments, setEnrollments] = useState<EducationEnrollment[]>([]);
  const [studentId, setStudentId] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<EducationEnrollment | null>(null);
  const [dropReason, setDropReason] = useState("");
  const requestGenerationRef = useRef(0);

  const loadEnrollments = useCallback(async () => {
    const generation = ++requestGenerationRef.current;
    setLoading(true);
    setError(null);
    try {
      const nextEnrollments = educationItems(await getClassEnrollments(classId));
      if (generation === requestGenerationRef.current) setEnrollments(nextEnrollments);
    } catch (requestError) {
      if (generation === requestGenerationRef.current) setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      if (generation === requestGenerationRef.current) setLoading(false);
    }
  }, [classId, tc]);

  useEffect(() => {
    void loadEnrollments();
    return () => { requestGenerationRef.current += 1; };
  }, [loadEnrollments]);

  const addEnrollment = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalizedStudentId = studentId.trim();
    if (!normalizedStudentId) return;
    setSubmitting(true);
    setError(null);
    try {
      await apiFetch<EducationEnrollment>(`${EDUCATION_API}/classes/${classId}/enrollments`, {
        method: "POST",
        body: { student_id: normalizedStudentId },
      });
      setStudentId("");
      await loadEnrollments();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setSubmitting(false);
    }
  };

  const dropEnrollment = async () => {
    if (!dropTarget || !dropReason.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      await apiFetch<EducationEnrollment>(`${EDUCATION_API}/classes/${classId}/enrollments/${dropTarget.student_id}/drop`, {
        method: "POST",
        body: { reason: dropReason.trim() },
      });
      setDropTarget(null);
      setDropReason("");
      await loadEnrollments();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="rounded-xl border border-border bg-bg-surface p-5" aria-labelledby="education-enrollments-title">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between"><div><h2 id="education-enrollments-title" className="text-lg font-semibold">{t("enrollments.title")}</h2><p className="mt-1 text-sm text-text-secondary">{t("enrollments.description")}</p></div><span className="text-sm text-text-muted">{t("enrollments.count", { count: enrollments.filter((value) => value.status === "active").length })}</span></div>
      <form className="mt-4 flex flex-col gap-2 sm:flex-row" onSubmit={addEnrollment}><label className="sr-only" htmlFor={`education-student-${classId}`}>{t("enrollments.studentId")}</label><input id={`education-student-${classId}`} value={studentId} onChange={(event) => setStudentId(event.target.value)} maxLength={100} placeholder={t("enrollments.studentIdPlaceholder")} className="min-w-0 flex-1 rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm focus:border-accent-primary focus:outline-none" /><Button type="submit" disabled={submitting || !studentId.trim()}>{t("enrollments.add")}</Button></form>
      {error && <p role="alert" className="mt-3 text-sm text-accent-error">{error}</p>}
      {loading && <div className="mt-4 h-24 animate-pulse rounded-lg bg-bg-primary" aria-busy="true" />}
      {!loading && enrollments.length === 0 && <p className="mt-4 text-sm text-text-muted">{t("enrollments.empty")}</p>}
      {!loading && enrollments.length > 0 && <div className="mt-4 overflow-x-auto"><table className="w-full min-w-[520px] text-sm"><thead className="border-b border-border text-left text-text-muted"><tr><th className="px-2 py-2 font-medium">{t("enrollments.studentId")}</th><th className="px-2 py-2 font-medium">{t("enrollments.status")}</th><th className="px-2 py-2 font-medium">{t("enrollments.enrolledAt")}</th><th className="px-2 py-2 text-right font-medium">{t("enrollments.actions")}</th></tr></thead><tbody>{enrollments.map((enrollment) => <tr key={enrollment.id} className="border-b border-border/70 last:border-0"><td className="px-2 py-3 font-mono text-xs text-text-secondary">{enrollment.student_id}</td><td className="px-2 py-3"><span className={enrollment.status === "active" ? "text-accent-success" : "text-text-muted"}>{t(`enrollmentStatus.${enrollment.status}`)}</span></td><td className="px-2 py-3 text-text-secondary">{formatEducationDate(enrollment.enrolled_at, locale)}</td><td className="px-2 py-3 text-right">{enrollment.status === "active" && <button type="button" onClick={() => setDropTarget(enrollment)} disabled={submitting} className="text-sm font-medium text-accent-warning hover:underline">{t("enrollments.withdraw")}</button>}</td></tr>)}</tbody></table></div>}
      {dropTarget && <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/50 p-4"><section role="alertdialog" aria-modal="true" aria-labelledby="education-drop-title" className="w-full max-w-md rounded-xl border border-border bg-bg-primary p-6"><h3 id="education-drop-title" className="text-lg font-semibold">{t("enrollments.dropTitle")}</h3><p className="mt-2 text-sm text-text-secondary">{t("enrollments.dropDescription")}</p><label htmlFor="education-drop-reason" className="mt-4 block text-sm font-medium">{t("enrollments.dropReason")}</label><textarea id="education-drop-reason" rows={3} maxLength={1000} value={dropReason} onChange={(event) => setDropReason(event.target.value)} className="mt-1 w-full resize-y rounded-lg border border-border bg-bg-surface px-3 py-2 text-sm focus:border-accent-primary focus:outline-none" /><div className="mt-5 flex justify-end gap-3"><Button type="button" variant="ghost" onClick={() => { setDropTarget(null); setDropReason(""); }} disabled={submitting}>{tc("cancel")}</Button><Button type="button" variant="secondary" onClick={() => void dropEnrollment()} disabled={submitting || !dropReason.trim()}>{submitting ? tc("processing") : t("enrollments.withdraw")}</Button></div></section></div>}
    </section>
  );
}
