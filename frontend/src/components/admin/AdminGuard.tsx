"use client";

import { useEffect } from "react";
import { useTranslations } from "next-intl";
import { useRouter } from "@/i18n/navigation";
import { useAuth } from "@/providers/AuthProvider";

export function AdminGuard({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const t = useTranslations("common");
  const { user, loading, sessionStatus, refresh, isAdmin } = useAuth();

  useEffect(() => {
    if (loading) return;
    if (sessionStatus === "anonymous") router.replace("/login");
    else if (user && !isAdmin) router.replace("/");
  }, [user, loading, sessionStatus, isAdmin, router]);

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }
  if (sessionStatus === "unknown" && !user) {
    return (
      <div className="mx-auto flex min-h-64 max-w-lg flex-col items-center justify-center gap-4 text-center">
        <p role="alert" className="text-sm text-text-secondary">
          {t("authStatusUnavailable")}
        </p>
        <button
          type="button"
          onClick={() => void refresh()}
          className="rounded-lg bg-accent-primary px-4 py-2 text-sm font-medium text-white hover:bg-accent-primary/90"
        >
          {t("retry")}
        </button>
      </div>
    );
  }
  if (!isAdmin) return null;
  return <>{children}</>;
}
