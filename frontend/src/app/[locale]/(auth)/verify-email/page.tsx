"use client";

import { useState, useEffect, useCallback } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { apiFetch, ApiError } from "@/lib/api-client";

type VerifyStatus = "loading" | "code" | "success" | "expired" | "invalid" | "error";

export default function VerifyEmailPage() {
  const t = useTranslations("auth");
  const searchParams = useSearchParams();
  const token = searchParams.get("token");
  const emailParam = searchParams.get("email") ?? "";

  const [status, setStatus] = useState<VerifyStatus>(token ? "loading" : "code");
  const [verificationEmail, setVerificationEmail] = useState(emailParam);
  const [verificationCode, setVerificationCode] = useState("");
  const [verifyingCode, setVerifyingCode] = useState(false);
  const [codeError, setCodeError] = useState("");
  const [resending, setResending] = useState(false);
  const [resendMsg, setResendMsg] = useState("");

  const verifyToken = useCallback(async () => {
    if (!token) {
      setStatus("code");
      return;
    }

    try {
      const response = await apiFetch<{ code_required?: boolean; email?: string }>(
        `/api/auth/verify-email?token=${encodeURIComponent(token)}`,
      );
      if (response.code_required) {
        setVerificationEmail(response.email ?? emailParam);
        setStatus("code");
        return;
      }
      setStatus("success");
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 410 || err.message.includes("expired") || err.message.includes("만료")) {
          setStatus("expired");
        } else {
          setStatus("invalid");
        }
      } else {
        setStatus("error");
      }
    }
  }, [emailParam, token]);

  useEffect(() => {
    verifyToken();
  }, [verifyToken]);

  const handleVerifyCode = async (event: React.FormEvent) => {
    event.preventDefault();
    const email = verificationEmail.trim();
    const code = verificationCode.replace(/\s/g, "");
    if (!email || code.length !== 6) {
      setCodeError(t("verifyCodeFormat"));
      return;
    }

    setVerifyingCode(true);
    setCodeError("");
    try {
      await apiFetch("/api/auth/verify-email/code", {
        method: "POST",
        body: { email, code },
      });
      setStatus("success");
    } catch (err) {
      if (err instanceof ApiError) {
        setCodeError(err.message || t("verifyCodeFailed"));
      } else {
        setCodeError(t("verifyCodeFailed"));
      }
    } finally {
      setVerifyingCode(false);
    }
  };

  const handleResend = async () => {
    const email = verificationEmail.trim();
    if (!email) return;
    setResending(true);
    setResendMsg("");
    try {
      await apiFetch(`/api/auth/send-verification?email=${encodeURIComponent(email)}`, {
        method: "POST",
      });
      setResendMsg(t("verifyResendSuccess"));
    } catch {
      setResendMsg(t("verifyResendFailed"));
    } finally {
      setResending(false);
    }
  };

  const resendForm = (
    <div className="rounded-lg border border-border bg-bg-surface p-4">
      <p className="mb-3 text-sm font-medium text-text-primary">{t("verifyResendTitle")}</p>
      <input
        type="email"
        value={verificationEmail}
        onChange={(e) => setVerificationEmail(e.target.value)}
        placeholder={t("emailPlaceholder")}
        className="mb-3 w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
      />
      <Button onClick={handleResend} disabled={resending || !verificationEmail.trim()} className="w-full">
        {resending ? t("sending") : t("verifyResendBtn")}
      </Button>
      {resendMsg && <p className="mt-2 text-sm text-text-secondary">{resendMsg}</p>}
    </div>
  );

  return (
    <div className="mx-auto max-w-md py-20">
      {status === "loading" && (
        <div className="flex flex-col items-center gap-4">
          <div className="h-10 w-10 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
          <p className="text-text-secondary">{t("verifyChecking")}</p>
        </div>
      )}

      {status === "code" && (
        <div>
          <h1 className="mb-2 text-2xl font-bold text-text-primary">{t("verifyCodeTitle")}</h1>
          <p className="mb-6 text-sm text-text-secondary">{t("verifyCodeHint")}</p>
          <form onSubmit={handleVerifyCode} className="space-y-4 rounded-lg border border-border bg-bg-surface p-4">
            <div>
              <label htmlFor="verification-email" className="mb-1.5 block text-sm font-medium">{t("email")}</label>
              <input
                id="verification-email"
                type="email"
                required
                value={verificationEmail}
                onChange={(e) => setVerificationEmail(e.target.value)}
                placeholder={t("emailPlaceholder")}
                className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
              />
            </div>
            <div>
              <label htmlFor="verification-code" className="mb-1.5 block text-sm font-medium">{t("verificationCode")}</label>
              <input
                id="verification-code"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                maxLength={6}
                required
                value={verificationCode}
                onChange={(e) => setVerificationCode(e.target.value.replace(/\D/g, ""))}
                placeholder="000000"
                className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 font-mono text-lg tracking-[0.35em] text-text-primary focus:border-accent-primary focus:outline-none"
              />
            </div>
            {codeError && <p className="text-sm text-accent-error">{codeError}</p>}
            <Button type="submit" disabled={verifyingCode} className="w-full">
              {verifyingCode ? t("verifyChecking") : t("verifyCodeBtn")}
            </Button>
          </form>
          <div className="mt-4">{resendForm}</div>
        </div>
      )}

      {status === "success" && (
        <div className="text-center">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-accent-success/20">
            <svg className="h-8 w-8 text-accent-success" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
            </svg>
          </div>
          <h1 className="mb-2 text-2xl font-bold text-text-primary">{t("verifySuccess")}</h1>
          <p className="mb-6 text-text-secondary">{t("verifySuccessDesc")}</p>
          <Link href="/login">
            <Button className="w-full">{t("login")}</Button>
          </Link>
        </div>
      )}

      {(status === "expired" || status === "invalid") && (
        <div className="text-center">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-accent-error/20">
            <svg className="h-8 w-8 text-accent-error" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </div>
          <h1 className="mb-2 text-2xl font-bold text-text-primary">
            {status === "expired" ? t("verifyExpired") : t("verifyInvalid")}
          </h1>
          <p className="mb-6 text-text-secondary">
            {status === "expired" ? t("verifyExpiredDesc") : t("verifyInvalidDesc")}
          </p>
          {resendForm}
        </div>
      )}

      {status === "error" && (
        <div className="text-center">
          <h1 className="mb-2 text-2xl font-bold text-text-primary">{t("verifyError")}</h1>
          <p className="mb-6 text-text-secondary">{t("verifyErrorDesc")}</p>
          <Link href="/login">
            <Button className="w-full">{t("backToLogin")}</Button>
          </Link>
        </div>
      )}
    </div>
  );
}
