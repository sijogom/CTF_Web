"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { apiFetch, ApiError } from "@/lib/api-client";

export default function ForgotPasswordPage() {
  const t = useTranslations("auth");
  const tc = useTranslations("common");
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (loading) return;
    setError(null);
    setLoading(true);
    try {
      await apiFetch("/api/auth/forgot-password", {
        method: "POST",
        body: { email },
      });
      setSent(true);
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setLoading(false);
    }
  };

  if (sent) {
    return (
      <div className="mx-auto max-w-md py-20 text-center">
        <span className="mb-4 block text-4xl">📧</span>
        <h1 className="mb-2 text-2xl font-bold">{t("checkEmail")}</h1>
        <p className="mb-6 text-text-secondary">
          {t("resetLinkSent", { email })}
        </p>
        <Link href="/login" className="text-sm text-accent-primary hover:underline">
          {t("backToLogin")}
        </Link>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-md py-20">
      <h1 className="mb-2 text-2xl font-bold">{t("forgotPasswordTitle")}</h1>
      <p className="mb-6 text-sm text-text-secondary">
        {t("resetDesc")}
      </p>
      {error && <p role="alert" className="mb-4 text-sm text-accent-error">{error}</p>}
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="email" className="mb-1.5 block text-sm font-medium">
            {t("email")}
          </label>
          <input
            id="email"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder={t("emailPlaceholder")}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
          />
        </div>
        <Button type="submit" className="w-full" disabled={loading}>
          {loading ? t("sending") : t("sendResetLink")}
        </Button>
      </form>
      <p className="mt-4 text-center text-sm text-text-muted">
        <Link href="/login" className="text-accent-primary hover:underline">
          {t("backToLogin")}
        </Link>
      </p>
    </div>
  );
}
