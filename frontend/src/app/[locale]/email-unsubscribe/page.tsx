"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";

type UnsubscribeStatus = "loading" | "success" | "error";

export default function EmailUnsubscribePage() {
  const t = useTranslations("emailUnsubscribe");
  const searchParams = useSearchParams();
  const token = searchParams.get("token");

  const [requestStatus, setRequestStatus] = useState<UnsubscribeStatus>("loading");
  const status: UnsubscribeStatus = token ? requestStatus : "error";

  useEffect(() => {
    if (!token) {
      return;
    }

    const unsubscribe = async () => {
      try {
        const res = await fetch(
          `/api/auth/email-unsubscribe?token=${encodeURIComponent(token)}`,
          { credentials: "include" }
        );
        setRequestStatus(res.ok ? "success" : "error");
      } catch {
        setRequestStatus("error");
      }
    };

    unsubscribe();
  }, [token]);

  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <div className="mx-auto max-w-md rounded-xl border border-border bg-bg-surface p-8 text-center">
        {status === "loading" && (
          <>
            <div className="mx-auto mb-4 h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
            <p className="text-text-secondary">{t("processing")}</p>
          </>
        )}

        {status === "success" && (
          <>
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-accent-success/10">
              <svg
                className="h-6 w-6 text-accent-success"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={2}
                stroke="currentColor"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
              </svg>
            </div>
            <h2 className="mb-2 text-xl font-bold">{t("success")}</h2>
            <p className="mb-6 text-sm text-text-secondary">{t("successDetail")}</p>
            <Link
              href="/"
              className="inline-block rounded-lg bg-accent-primary px-6 py-2.5 text-sm font-medium text-white hover:opacity-90 transition-opacity"
            >
              {t("goHome")}
            </Link>
          </>
        )}

        {status === "error" && (
          <>
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-accent-error/10">
              <svg
                className="h-6 w-6 text-accent-error"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={2}
                stroke="currentColor"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </div>
            <h2 className="mb-2 text-xl font-bold text-accent-error">{t("error")}</h2>
            <Link
              href="/"
              className="mt-4 inline-block text-sm text-accent-primary hover:underline"
            >
              {t("goHome")}
            </Link>
          </>
        )}
      </div>
    </div>
  );
}
