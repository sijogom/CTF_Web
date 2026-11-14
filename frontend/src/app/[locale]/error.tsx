"use client";

import { useTranslations } from "next-intl";

export default function Error({ reset }: { reset: () => void }) {
  const t = useTranslations("errors");
  const tc = useTranslations("common");

  return (
    <main className="flex min-h-[60vh] items-center justify-center px-6">
      <div className="text-center">
        <p className="text-6xl font-bold text-text-primary">{t("pageError")}</p>
        <p className="mt-4 text-lg text-text-muted">
          {t("serverError")}
        </p>
        <button
          onClick={reset}
          className="mt-6 rounded-lg bg-accent-primary px-6 py-2 text-white transition-colors hover:bg-accent-primary/90"
        >
          {tc("retry")}
        </button>
      </div>
    </main>
  );
}
