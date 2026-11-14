"use client";

import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { Link, useRouter } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useAuth } from "@/providers/AuthProvider";
import { PasswordInput } from "@/components/ui/PasswordInput";
import { useSiteSettings } from "@/providers/SiteSettingsProvider";

const BAN_APPEAL_STORAGE_KEY = "ctf_ban_appeal";

interface BannedLoginDetail {
  code: "ACCOUNT_BANNED";
  appeal_token?: string;
  ban?: {
    id: string;
    reason: string;
    started_at: string;
    expires_at: string | null;
    appeal_allowed: boolean;
  };
}

interface DiscordSSOStartResponse {
  configured: boolean;
  url: string | null;
}

function isBannedLoginDetail(value: unknown): value is BannedLoginDetail {
  if (!value || typeof value !== "object") return false;
  const detail = value as Record<string, unknown>;
  return detail.code === "ACCOUNT_BANNED";
}

export default function LoginPage() {
  const t = useTranslations("auth");
  const tc = useTranslations("common");
  const locale = useLocale();
  const router = useRouter();
  const searchParams = useSearchParams();
  const { login } = useAuth();
  const { siteName } = useSiteSettings();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [needVerify, setNeedVerify] = useState(false);
  const [resending, setResending] = useState(false);
  const [resendMsg, setResendMsg] = useState("");
  const [bannedDetail, setBannedDetail] = useState<BannedLoginDetail | null>(null);
  const [discordLoading, setDiscordLoading] = useState(false);
  const [discordError, setDiscordError] = useState("");

  const ssoOutcome = searchParams.get("sso");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setNeedVerify(false);
    setResendMsg("");
    setBannedDetail(null);
    setLoading(true);

    try {
      const authResult = await login(email, password);
      if (authResult !== "authenticated") {
        setError(authResult === "unknown" ? tc("authStatusUnavailable") : t("loginError"));
        return;
      }
      const redirect = searchParams.get("redirect");
      // 안전한 상대 경로만 허용 (open redirect 방지)
      const isSafe = redirect && redirect.startsWith("/") && !redirect.startsWith("//") && !redirect.startsWith("/\\");
      const target = isSafe ? redirect : "/wargame";
      router.push(target);
    } catch (err) {
      if (err instanceof ApiError && err.message === "EMAIL_NOT_VERIFIED") {
        setNeedVerify(true);
      } else if (err instanceof ApiError && isBannedLoginDetail(err.detail)) {
        if (typeof err.detail.appeal_token === "string") {
          window.sessionStorage.setItem(BAN_APPEAL_STORAGE_KEY, JSON.stringify(err.detail));
          router.push("/ban-appeal");
        } else {
          setBannedDetail(err.detail);
        }
      } else {
        const message = err instanceof ApiError ? err.message : t("loginError");
        setError(message);
      }
    } finally {
      setLoading(false);
    }
  };

  const handleResend = async () => {
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

  const handleDiscordLogin = async () => {
    setDiscordLoading(true);
    setDiscordError("");

    try {
      const data = await apiFetch<DiscordSSOStartResponse>(
        `/api/auth/discord/sso/start?redirect=${encodeURIComponent(`/${locale}/wargame`)}&locale=${encodeURIComponent(locale)}`,
      );
      if (!data.configured || typeof data.url !== "string" || !data.url) {
        setDiscordError(t("discordLoginUnavailable"));
        return;
      }
      window.location.assign(data.url);
    } catch {
      setDiscordError(t("discordLoginUnavailable"));
    } finally {
      setDiscordLoading(false);
    }
  };

  return (
    <div className="mx-auto max-w-md py-20">
      <h1 className="mb-2 text-2xl font-bold">{t("login")}</h1>
      <p className="mb-6 text-sm text-text-secondary">
        {t("loginDesc", { siteName })}
      </p>

      {/* 이메일 미인증 안내 */}
      {needVerify && (
        <div className="mb-4 rounded-lg border border-accent-warning/30 bg-accent-warning/10 p-4">
          <p className="mb-2 text-sm font-medium text-text-primary">{t("verifyRequired")}</p>
          <p className="mb-3 text-sm text-text-secondary">{t("verifyRequiredDesc")}</p>
          <button
            onClick={handleResend}
            disabled={resending}
            className="text-sm font-medium text-accent-primary hover:underline disabled:opacity-50"
          >
            {resending ? t("sending") : t("verifyResendBtn")}
          </button>
          {resendMsg && (
            <p className="mt-2 text-sm text-text-secondary">{resendMsg}</p>
          )}
        </div>
      )}

      {bannedDetail && (
        <div className="mb-4 rounded-lg border border-accent-error/30 bg-accent-error/10 p-4 text-sm">
          <p className="font-medium text-text-primary">{t("banAppealTitle")}</p>
          {bannedDetail.ban?.reason && (
            <p className="mt-2 whitespace-pre-wrap text-text-secondary">{bannedDetail.ban.reason}</p>
          )}
          <p className="mt-2 text-text-secondary">{t("banAppealNotAllowed")}</p>
        </div>
      )}

      {ssoOutcome && (
        <div className="mb-4 rounded-lg border border-accent-error/30 bg-accent-error/10 p-4 text-sm text-text-secondary">
          {ssoOutcome === "unavailable" ? t("discordLoginUnavailable") : t("discordLoginFailed")}
        </div>
      )}

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
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
          />
        </div>
        <div>
          <div className="mb-1.5 flex items-center justify-between">
            <label htmlFor="password" className="text-sm font-medium">
              {t("password")}
            </label>
            <Link
              href="/forgot-password"
              className="text-xs text-accent-primary hover:underline"
            >
              {t("forgotPassword")}
            </Link>
          </div>
          <PasswordInput
            id="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 pr-10 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
          />
        </div>
        {error && <p className="text-sm text-accent-error">{error}</p>}
        <Button type="submit" className="w-full" disabled={loading}>
          {loading ? t("loggingIn") : t("login")}
        </Button>
      </form>

      <div className="mt-5 border-t border-border pt-5">
        <Button
          type="button"
          variant="secondary"
          className="w-full"
          disabled={loading || discordLoading}
          onClick={handleDiscordLogin}
        >
          {discordLoading ? t("discordLoginPreparing") : t("loginWithDiscord")}
        </Button>
        <p className="mt-2 text-center text-xs text-text-muted">
          {t("discordLoginPrelinkedOnly")}
        </p>
        {discordError && (
          <p className="mt-2 text-center text-sm text-accent-error">{discordError}</p>
        )}
      </div>

      <p className="mt-6 text-center text-sm text-text-muted">
        {t("noAccount")}{" "}
        <Link href="/register" className="text-accent-primary hover:underline">
          {t("signup")}
        </Link>
      </p>
    </div>
  );
}
