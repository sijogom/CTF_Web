"use client";

import { useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { Link } from "@/i18n/navigation";
import { ApiError, apiFetch } from "@/lib/api-client";
import { Button } from "@/components/ui/Button";

const BAN_APPEAL_STORAGE_KEY = "ctf_ban_appeal";

interface BanAppealContext {
  code: "ACCOUNT_BANNED";
  appeal_token: string;
  ban: {
    id: string;
    reason: string;
    started_at: string;
    expires_at: string | null;
    appeal_allowed: boolean;
  };
}

function isBanAppealContext(value: unknown): value is BanAppealContext {
  if (!value || typeof value !== "object") return false;
  const detail = value as Record<string, unknown>;
  const ban = detail.ban as Record<string, unknown> | undefined;
  return (
    detail.code === "ACCOUNT_BANNED"
    && typeof detail.appeal_token === "string"
    && !!ban
    && typeof ban.reason === "string"
  );
}

export default function BanAppealPage() {
  const t = useTranslations("auth");
  const locale = useLocale();
  const [context, setContext] = useState<BanAppealContext | null>(null);
  const [ready, setReady] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    try {
      const raw = window.sessionStorage.getItem(BAN_APPEAL_STORAGE_KEY);
      const parsed: unknown = raw ? JSON.parse(raw) : null;
      if (isBanAppealContext(parsed)) setContext(parsed);
    } catch {
      window.sessionStorage.removeItem(BAN_APPEAL_STORAGE_KEY);
    } finally {
      setReady(true);
    }
  }, []);

  const formatAt = (value: string | null) => value
    ? new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value))
    : "-";

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!context) return;
    setError("");
    setSubmitting(true);
    try {
      await apiFetch("/api/auth/bans/appeals", {
        method: "POST",
        headers: { Authorization: `Bearer ${context.appeal_token}` },
        body: { message },
      });
      window.sessionStorage.removeItem(BAN_APPEAL_STORAGE_KEY);
      setSubmitted(true);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError(t("banAppealAlreadySubmitted"));
      } else {
        setError(err instanceof ApiError ? err.message : t("banAppealError"));
      }
    } finally {
      setSubmitting(false);
    }
  };

  if (!ready) {
    return <div className="mx-auto max-w-md py-20 text-sm text-text-muted">...</div>;
  }

  if (!context) {
    return (
      <div className="mx-auto max-w-md py-20">
        <h1 className="text-2xl font-bold">{t("banAppealTitle")}</h1>
        <p className="mt-3 text-sm text-text-secondary">{t("banAppealUnavailable")}</p>
        <Link href="/login" className="mt-5 inline-block text-sm text-accent-primary hover:underline">
          {t("login")}
        </Link>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-md py-20">
      <h1 className="text-2xl font-bold">{t("banAppealTitle")}</h1>
      <p className="mt-2 text-sm text-text-secondary">{t("banAppealDescription")}</p>

      <dl className="mt-6 space-y-3 rounded-xl border border-border bg-bg-surface p-4 text-sm">
        <div>
          <dt className="text-text-muted">{t("banAppealReason")}</dt>
          <dd className="mt-1 whitespace-pre-wrap">{context.ban.reason}</dd>
        </div>
        <div>
          <dt className="text-text-muted">{t("banAppealExpires")}</dt>
          <dd className="mt-1">{formatAt(context.ban.expires_at)}</dd>
        </div>
      </dl>

      {submitted ? (
        <p className="mt-5 rounded-lg border border-accent-success/30 bg-accent-success/10 p-4 text-sm text-accent-success">
          {t("banAppealSuccess")}
        </p>
      ) : (
        <form onSubmit={handleSubmit} className="mt-5 space-y-3">
          <label htmlFor="ban-appeal-message" className="block text-sm font-medium">
            {t("banAppealMessage")}
          </label>
          <textarea
            id="ban-appeal-message"
            required
            minLength={10}
            maxLength={4000}
            rows={7}
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm"
          />
          {error && <p className="text-sm text-accent-error">{error}</p>}
          <Button type="submit" className="w-full" disabled={submitting}>
            {t("banAppealSubmit")}
          </Button>
        </form>
      )}
    </div>
  );
}
