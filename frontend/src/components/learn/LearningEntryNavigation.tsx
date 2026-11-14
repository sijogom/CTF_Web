"use client";

import { BookOpen, GraduationCap, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { useAuth } from "@/providers/AuthProvider";
import { EducationAccessProvider, useEducationAccess } from "@/components/teaching/EducationAccessProvider";

function LearningEntryLinks() {
  const t = useTranslations("education");
  const tc = useTranslations("common");
  const { user, sessionStatus } = useAuth();
  const { permissions, loading, errorStatus, refresh } = useEducationAccess();
  if (!user || sessionStatus !== "authenticated") return null;

  const linkClass = "inline-flex min-w-0 items-center gap-2 rounded-md border border-border px-3 py-2 text-sm font-medium text-text-secondary hover:bg-bg-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary";
  return (
    <nav aria-label={t("nav.label")} className="flex flex-wrap items-center gap-2">
      <Link href="/learn/classes" className={linkClass}>
        <BookOpen className="h-4 w-4 shrink-0" aria-hidden="true" />{t("studentClasses.title")}
      </Link>
      {!loading && errorStatus === null && permissions?.is_education_manager && <Link href="/teaching" className={linkClass}>
        <GraduationCap className="h-4 w-4 shrink-0" aria-hidden="true" />{t("nav.dashboard")}
      </Link>}
      {errorStatus !== null && <button type="button" disabled={loading} onClick={() => void refresh()}
        aria-label={tc("retry")} title={tc("retry")} className="rounded-md p-2 text-accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary disabled:opacity-50">
        <RefreshCw className="h-4 w-4" aria-hidden="true" />
      </button>}
    </nav>
  );
}

export function LearningEntryNavigation() {
  return <EducationAccessProvider><LearningEntryLinks /></EducationAccessProvider>;
}
