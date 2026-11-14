"use client";

import { useState } from "react";
import { useParams } from "next/navigation";
import { useRouter } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { PasswordInput } from "@/components/ui/PasswordInput";
import { apiFetch, ApiError } from "@/lib/api-client";

export default function ResetPasswordPage() {
  const t = useTranslations("auth");
  const params = useParams();
  const router = useRouter();
  const token = params.token as string;

  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (password !== confirmPassword) {
      setError(t("passwordMismatch"));
      return;
    }

    if (password.length < 8) {
      setError(t("passwordMinLength"));
      return;
    }

    setLoading(true);
    try {
      await apiFetch("/api/auth/reset-password", {
        method: "POST",
        body: { token, new_password: password },
      });

      router.push("/login?reset=success");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("resetFailed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mx-auto max-w-md py-20">
      <h1 className="mb-2 text-2xl font-bold">{t("resetPasswordTitle")}</h1>
      <p className="mb-6 text-sm text-text-secondary">
        {t("resetPasswordDesc")}
      </p>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="password" className="mb-1.5 block text-sm font-medium">
            {t("newPassword")}
          </label>
          <PasswordInput
            id="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={t("newPasswordPlaceholder")}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 pr-10 text-sm focus:border-accent-primary focus:outline-none"
          />
        </div>
        <div>
          <label htmlFor="confirm-password" className="mb-1.5 block text-sm font-medium">
            {t("confirmPassword")}
          </label>
          <PasswordInput
            id="confirm-password"
            required
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            placeholder={t("confirmPlaceholder")}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 pr-10 text-sm focus:border-accent-primary focus:outline-none"
          />
        </div>
        {error && (
          <p className="text-sm text-accent-error">{error}</p>
        )}
        <Button type="submit" className="w-full" disabled={loading}>
          {loading ? t("changing") : t("changePassword")}
        </Button>
      </form>
    </div>
  );
}
