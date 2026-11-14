"use client";

import { type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { useAuth } from "@/providers/AuthProvider";
import { useEducationAccess } from "@/components/teaching/EducationAccessProvider";

export function TeachingAccessGate({ children }: { children: ReactNode }) {
  const t = useTranslations("education");
  const tc = useTranslations("common");
  const { user, loading: authLoading, sessionStatus, refresh: refreshAuth } = useAuth();
  const { permissions, loading: permissionLoading, errorStatus, refresh } = useEducationAccess();

  if (authLoading || (sessionStatus === "authenticated" && permissionLoading)) {
    return (
      <div className="flex min-h-64 items-center justify-center" role="status" aria-live="polite">
        <span className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
        <span className="sr-only">{tc("loading")}</span>
      </div>
    );
  }

  if (sessionStatus === "unknown") {
    return (
      <main className="mx-auto max-w-xl rounded-xl border border-border bg-bg-surface p-8 text-center" aria-labelledby="teaching-session-unknown-title">
        <h1 id="teaching-session-unknown-title" className="text-xl font-semibold">{tc("error")}</h1>
        <p className="mt-2 text-sm text-text-secondary">{tc("authStatusUnavailable")}</p>
        <button type="button" onClick={() => void refreshAuth()} className="mt-5 text-sm font-medium text-accent-primary hover:underline">{tc("retry")}</button>
      </main>
    );
  }

  if (sessionStatus === "anonymous" || !user) {
    return (
      <main className="mx-auto max-w-xl rounded-xl border border-border bg-bg-surface p-8 text-center" aria-labelledby="teaching-signin-title">
        <h1 id="teaching-signin-title" className="text-xl font-semibold">{t("access.signInTitle")}</h1>
        <p className="mt-2 text-sm text-text-secondary">{t("access.signInDescription")}</p>
        <Link href="/login" className="mt-5 inline-flex rounded-lg bg-accent-primary px-4 py-2 text-sm font-medium text-white hover:bg-accent-primary-hover">
          {t("access.signIn")}
        </Link>
      </main>
    );
  }

  if (errorStatus !== null) {
    return <main className="mx-auto max-w-xl rounded-xl border border-border bg-bg-surface p-8 text-center" aria-labelledby="teaching-access-error-title"><h1 id="teaching-access-error-title" className="text-xl font-semibold">{tc("error")}</h1><p className="mt-2 text-sm text-text-secondary">{errorStatus === 403 ? t("access.forbiddenDescription") : tc("authStatusUnavailable")}</p><button type="button" onClick={() => void refresh()} className="mt-5 text-sm font-medium text-accent-primary hover:underline">{tc("retry")}</button></main>;
  }

  if (!permissions?.is_education_manager) {
    return <main className="mx-auto max-w-xl rounded-xl border border-border bg-bg-surface p-8 text-center" aria-labelledby="teaching-forbidden-title"><p className="text-sm font-medium text-accent-error">403</p><h1 id="teaching-forbidden-title" className="mt-2 text-xl font-semibold">{t("access.forbiddenTitle")}</h1><p className="mt-2 text-sm text-text-secondary">{t("access.forbiddenDescription")}</p></main>;
  }

  return <>{children}</>;
}
