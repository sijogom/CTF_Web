"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { apiFetch, ApiError } from "@/lib/api-client";
import { PasswordInput } from "@/components/ui/PasswordInput";

export default function RegisterPage() {
  const t = useTranslations("auth");
  const [form, setForm] = useState({
    email: "",
    username: "",
    password: "",
    confirmPassword: "",
  });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [registered, setRegistered] = useState(false);
  const [autoVerified, setAutoVerified] = useState(false);
  const [deliveryStatus, setDeliveryStatus] = useState<"sent" | "failed">("sent");
  const [resending, setResending] = useState(false);
  const [resendMsg, setResendMsg] = useState("");

  const updateField = (field: string, value: string) => {
    setForm((prev) => ({ ...prev, [field]: value }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (form.password !== form.confirmPassword) {
      setError(t("passwordMismatch"));
      return;
    }

    setLoading(true);
    try {
      const res = await apiFetch<{ message: string; auto_verified?: boolean; delivery_status?: "sent" | "failed" }>("/api/auth/register", {
        method: "POST",
        body: {
          email: form.email,
          username: form.username,
          password: form.password,
        },
      });
      setRegistered(true);
      setDeliveryStatus(res.delivery_status ?? "sent");
      if (res.auto_verified) {
        setAutoVerified(true);
      }
    } catch (err) {
      const message = err instanceof ApiError ? err.message : t("registerFailed");
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  const handleResend = async () => {
    setResending(true);
    setResendMsg("");
    try {
      await apiFetch(`/api/auth/send-verification?email=${encodeURIComponent(form.email)}`, {
        method: "POST",
      });
      setResendMsg(t("verifyResendSuccess"));
    } catch {
      setResendMsg(t("verifyResendFailed"));
    } finally {
      setResending(false);
    }
  };

  // 가입 완료 후 안내
  if (registered) {
    return (
      <div className="mx-auto max-w-md py-20 text-center">
        <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-accent-primary/20">
          {autoVerified ? (
            <svg className="h-8 w-8 text-accent-primary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
            </svg>
          ) : (
            <svg className="h-8 w-8 text-accent-primary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
            </svg>
          )}
        </div>

        {autoVerified ? (
          <>
            <h1 className="mb-2 text-2xl font-bold text-text-primary">{t("registerComplete")}</h1>
            <p className="mb-6 text-text-secondary">{t("registerCompleteDesc")}</p>
            <Link
              href="/login"
              className="inline-flex items-center rounded-lg bg-accent-primary px-6 py-3 text-sm font-medium text-white hover:bg-accent-primary/90 transition-colors"
            >
              {t("goToLogin")}
            </Link>
          </>
        ) : (
          <>
            <h1 className="mb-2 text-2xl font-bold text-text-primary">
              {deliveryStatus === "failed" ? t("verifyEmailDeliveryFailed") : t("verifyEmailSent")}
            </h1>
            <p className="mb-2 text-text-secondary">
              {deliveryStatus === "failed"
                ? t("verifyEmailDeliveryFailedDesc")
                : t("verifyEmailSentDesc", { email: form.email })}
            </p>
            <p className="mb-6 text-sm text-text-muted">{t("verifyCodeHint")}</p>

            <Link
              href={`/verify-email?email=${encodeURIComponent(form.email)}`}
              className="mb-4 inline-flex items-center rounded-lg bg-accent-primary px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-accent-primary/90"
            >
              {t("enterVerificationCode")}
            </Link>

            <button
              onClick={handleResend}
              disabled={resending}
              className="text-sm text-accent-primary hover:underline disabled:opacity-50"
            >
              {resending ? t("sending") : t("verifyResendBtn")}
            </button>
            {resendMsg && (
              <p className="mt-2 text-sm text-text-secondary">{resendMsg}</p>
            )}

            <p className="mt-6 text-sm text-text-muted">
              <Link href="/login" className="text-accent-primary hover:underline">
                {t("backToLogin")}
              </Link>
            </p>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-md py-20">
      <h1 className="mb-2 text-2xl font-bold">{t("signup")}</h1>
      <p className="mb-6 text-sm text-text-secondary">
        {t("platformDesc")}
      </p>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="email" className="mb-1.5 block text-sm font-medium">
            {t("email")} <span className="text-accent-error">*</span>
          </label>
          <input
            id="email"
            type="email"
            required
            value={form.email}
            onChange={(e) => updateField("email", e.target.value)}
            placeholder={t("emailPlaceholder")}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
          />
        </div>
        <div>
          <label htmlFor="username" className="mb-1.5 block text-sm font-medium">
            {t("username")} <span className="text-accent-error">*</span>
          </label>
          <input
            id="username"
            type="text"
            required
            value={form.username}
            onChange={(e) => updateField("username", e.target.value)}
            placeholder={t("usernamePlaceholder")}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
          />
        </div>
        <div>
          <label htmlFor="password" className="mb-1.5 block text-sm font-medium">
            {t("password")} <span className="text-accent-error">*</span>
          </label>
          <PasswordInput
            id="password"
            required
            value={form.password}
            onChange={(e) => updateField("password", e.target.value)}
            placeholder={t("passwordPlaceholder")}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 pr-10 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
          />
        </div>
        <div>
          <label htmlFor="confirm" className="mb-1.5 block text-sm font-medium">
            {t("confirmPassword")} <span className="text-accent-error">*</span>
          </label>
          <PasswordInput
            id="confirm"
            required
            value={form.confirmPassword}
            onChange={(e) => updateField("confirmPassword", e.target.value)}
            placeholder={t("confirmPlaceholder")}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 pr-10 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
          />
        </div>
        {error && <p className="text-sm text-accent-error">{error}</p>}
        <Button type="submit" className="w-full" disabled={loading}>
          {loading ? t("registering") : t("signup")}
        </Button>
      </form>

      <p className="mt-6 text-center text-sm text-text-muted">
        {t("hasAccount")}{" "}
        <Link href="/login" className="text-accent-primary hover:underline">
          {t("login")}
        </Link>
      </p>
    </div>
  );
}
