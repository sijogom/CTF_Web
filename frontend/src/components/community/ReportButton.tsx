"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { apiFetch, ApiError } from "@/lib/api-client";

interface ReportButtonProps {
  targetType: "post" | "comment" | "marketplace_item" | "marketplace_comment";
  targetId: string;
}

const REASON_KEYS = [
  { value: "spam", i18nKey: "spam" },
  { value: "abuse", i18nKey: "abuse" },
  { value: "inappropriate", i18nKey: "inappropriate" },
  { value: "other", i18nKey: "other" },
];

export function ReportButton({ targetType, targetId }: ReportButtonProps) {
  const t = useTranslations("community");
  const tc = useTranslations("common");
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [detail, setDetail] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    if (!reason || loading) return;
    setLoading(true);
    setError(null);

    try {
      await apiFetch("/api/community/reports", {
        method: "POST",
        body: {
          target_type: targetType,
          target_id: targetId,
          reason,
          detail,
        },
      });
      setSubmitted(true);
      setTimeout(() => {
        setOpen(false);
        setSubmitted(false);
        setReason("");
        setDetail("");
      }, 1500);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("reportFailed"));
    } finally {
      setLoading(false);
    }
  };

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="text-xs text-text-muted hover:text-accent-error"
      >
        {t("report")}
      </button>
    );
  }

  return (
    <div className="rounded-lg border border-border bg-bg-surface p-4">
      {submitted ? (
        <p className="text-sm text-accent-success">{t("reportSuccess")}</p>
      ) : (
        <>
          <h4 className="mb-3 text-sm font-medium">{t("reportReason")}</h4>
          <div className="mb-3 flex flex-wrap gap-2">
            {REASON_KEYS.map((r) => (
              <button
                key={r.value}
                onClick={() => setReason(r.value)}
                className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                  reason === r.value
                    ? "bg-accent-error/10 text-accent-error"
                    : "bg-bg-primary text-text-muted hover:text-text-primary"
                }`}
              >
                {t(`reportReasons.${r.i18nKey}`)}
              </button>
            ))}
          </div>
          <textarea
            value={detail}
            onChange={(e) => setDetail(e.target.value)}
            placeholder={t("reportDetail")}
            rows={2}
            className="mb-3 w-full resize-none rounded-lg border border-border bg-bg-primary px-3 py-2 text-xs focus:border-accent-primary focus:outline-none"
          />
          {error && (
            <p className="mb-2 text-xs text-accent-error">{error}</p>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>{tc("cancel")}</Button>
            <Button size="sm" onClick={handleSubmit} disabled={!reason || loading}>
              {loading ? t("submittingReport") : t("report")}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
