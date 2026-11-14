"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { PasswordInput } from "@/components/ui/PasswordInput";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";

export default function SecuritySettingsPage() {
  const t = useTranslations("settings");
  const { toast } = useToast();
  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [changingPw, setChangingPw] = useState(false);

  const handleChangePw = async () => {
    if (changingPw) return;
    if (newPw !== confirmPw) {
      toast("error", t("passwordMismatch"));
      return;
    }
    setChangingPw(true);
    try {
      await apiFetch("/api/auth/password", {
        method: "PATCH",
        body: { current_password: currentPw, new_password: newPw },
      });
      toast("success", t("passwordChanged"));
      setCurrentPw("");
      setNewPw("");
      setConfirmPw("");
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : t("changeFailed"));
    } finally {
      setChangingPw(false);
    }
  };

  return (
    <div className="space-y-8">
      <h2 className="text-lg font-semibold">{t("securityTitle")}</h2>

      {/* 비밀번호 변경 */}
      <div className="space-y-4 rounded-xl border border-border bg-bg-surface p-6">
        <h3 className="font-medium">{t("changePassword")}</h3>
        <PasswordInput
          value={currentPw}
          onChange={(e) => setCurrentPw(e.target.value)}
          placeholder={t("currentPassword")}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 pr-10 text-sm focus:border-accent-primary focus:outline-none"
        />
        <PasswordInput
          value={newPw}
          onChange={(e) => setNewPw(e.target.value)}
          placeholder={t("newPassword")}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 pr-10 text-sm focus:border-accent-primary focus:outline-none"
        />
        <PasswordInput
          value={confirmPw}
          onChange={(e) => setConfirmPw(e.target.value)}
          placeholder={t("confirmNewPassword")}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 pr-10 text-sm focus:border-accent-primary focus:outline-none"
        />
        <Button onClick={handleChangePw} disabled={changingPw || !currentPw || !newPw || newPw !== confirmPw}>
          {t("changePassword")}
        </Button>
      </div>

      {/* 2FA */}
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <h3 className="mb-2 font-medium">{t("twoFactor")}</h3>
        <p className="mb-4 text-sm text-text-secondary">
          {t("twoFactorDesc")}
        </p>
        <Button variant="secondary" disabled>
          {t("setup2FA")}
        </Button>
        <p className="mt-2 text-xs text-text-muted">{t("comingSoon")}</p>
      </div>

      {/* 활성 세션 */}
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <h3 className="mb-4 font-medium">{t("activeSessions")}</h3>
        <div className="space-y-3">
          <div className="flex items-center justify-between rounded-lg border border-border bg-bg-primary p-3">
            <div>
              <p className="text-sm font-medium">{t("currentSession")}</p>
              <p className="text-xs text-text-muted">{t("currentDevice")}</p>
            </div>
            <span className="rounded-full bg-accent-success/10 px-2 py-0.5 text-xs text-accent-success">
              {t("active")}
            </span>
          </div>
        </div>
        <p className="mt-3 text-xs text-text-muted">{t("sessionManagementComingSoon")}</p>
      </div>
    </div>
  );
}
