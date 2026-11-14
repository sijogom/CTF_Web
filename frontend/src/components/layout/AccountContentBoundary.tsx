"use client";

import { Fragment } from "react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { useAuth } from "@/providers/AuthProvider";

export function AccountContentBoundary({ children }: { children: React.ReactNode }) {
  const { user, loading, sessionStatus, refresh } = useAuth();
  const t = useTranslations("common");
  const tn = useTranslations("nav");
  if (user && sessionStatus !== "anonymous") return <Fragment key={user.id}>{children}</Fragment>;
  if (loading) return <div role="status" className="flex min-h-48 items-center justify-center"><span className="sr-only">{t("loading")}</span><span className="h-6 w-6 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" /></div>;
  if (sessionStatus === "unknown") return <div className="space-y-3 py-8 text-center"><p role="alert" className="text-sm text-text-secondary">{t("authStatusUnavailable")}</p><button type="button" onClick={() => void refresh()} className="rounded-md border border-border px-3 py-2 text-sm font-medium text-accent-primary">{t("retry")}</button></div>;
  return <div className="py-8 text-center"><Link href="/login" className="text-sm font-medium text-accent-primary">{tn("login")}</Link></div>;
}
