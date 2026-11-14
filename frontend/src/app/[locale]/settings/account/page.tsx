"use client";

import { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";
import { useRouter } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import { useAuth } from "@/providers/AuthProvider";

interface DiscordStatus {
  configured: boolean;
  bot_configured: boolean;
}

interface UserProfile {
  email: string;
  discord_id: string | null;
  discord_username: string | null;
}

export default function AccountSettingsPage() {
  const t = useTranslations("settings");
  const { toast } = useToast();
  const router = useRouter();
  const { refresh } = useAuth();
  const searchParams = useSearchParams();
  const [email, setEmail] = useState("");
  const [deleteConfirm, setDeleteConfirm] = useState("");
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [discordStatus, setDiscordStatus] = useState<DiscordStatus | null>(null);
  const [discordLinked, setDiscordLinked] = useState(false);
  const [discordUsername, setDiscordUsername] = useState<string | null>(null);
  const [discordLoading, setDiscordLoading] = useState(false);
  const [discordMessage, setDiscordMessage] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const loadProfile = useCallback(async () => {
    try {
      const profile = await apiFetch<UserProfile>("/api/auth/me");
      setEmail(profile.email);
      setDiscordLinked(!!profile.discord_id);
      setDiscordUsername(profile.discord_username);
    } catch {
      /* 프로필 로드 실패 — 무시 */
    }
  }, []);

  useEffect(() => {
    loadProfile();
    // Discord 연동 설정 상태 확인
    apiFetch<DiscordStatus>("/api/auth/discord/status")
      .then(setDiscordStatus)
      .catch(() => setDiscordStatus({ configured: false, bot_configured: false }));
  }, [loadProfile]);

  // Discord 콜백에서 돌아왔을 때
  useEffect(() => {
    if (searchParams.get("discord") === "linked") {
      setDiscordMessage(t("discordLinkSuccess"));
      loadProfile();
    }
  }, [searchParams, t, loadProfile]);

  const handleDiscordLink = async () => {
    setDiscordLoading(true);
    try {
      const data = await apiFetch<{ url: string }>("/api/auth/discord/link");
      window.location.href = data.url;
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : t("discordNotConfigured");
      setDiscordMessage(msg);
      setDiscordLoading(false);
    }
  };

  const handleDiscordUnlink = async () => {
    setDiscordLoading(true);
    try {
      await apiFetch("/api/auth/discord/unlink", { method: "POST" });
      setDiscordLinked(false);
      setDiscordUsername(null);
      setDiscordMessage(t("discordUnlinkSuccess"));
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed";
      setDiscordMessage(msg);
    } finally {
      setDiscordLoading(false);
    }
  };

  const handleDeleteAccount = async () => {
    if (deleting || deleteConfirm !== "DELETE") return;
    setShowDeleteModal(false);
    setDeleting(true);
    try {
      await apiFetch("/api/auth/users/me", { method: "DELETE" });
      toast("success", t("accountDeleted"));
      await refresh();
      router.replace("/");
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : t("deleteFailed"));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="space-y-8">
      <h2 className="text-lg font-semibold">{t("accountTitle")}</h2>

      {/* 이메일 변경 */}
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <h3 className="mb-2 font-medium">{t("emailAddress")}</h3>
        <p className="mb-4 text-sm text-text-muted">{t("emailChangeNotice")}</p>
        <div className="flex gap-3">
          <input
            type="email"
            value={email}
            readOnly
            className="flex-1 rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm text-text-muted cursor-not-allowed"
          />
          <Button variant="secondary" disabled title={t("comingSoon")}>{t("change")}</Button>
        </div>
      </div>

      {/* OAuth 연결 */}
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <h3 className="mb-4 font-medium">{t("socialConnect")}</h3>

        {/* Discord 알림 메시지 */}
        {discordMessage && (
          <div className="mb-4 rounded-lg bg-accent-primary/10 px-4 py-2.5 text-sm text-accent-primary">
            {discordMessage}
          </div>
        )}

        <div className="space-y-3">
          {/* Discord */}
          <div className="flex items-center justify-between rounded-lg border border-border bg-bg-primary p-3">
            <div className="flex items-center gap-3">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" className="text-[#5865F2]">
                <path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028 14.09 14.09 0 0 0 1.226-1.994.076.076 0 0 0-.041-.106 13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128 10.2 10.2 0 0 0 .372-.292.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127 12.299 12.299 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839 19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z"/>
              </svg>
              <div>
                <span className="text-sm font-medium">Discord</span>
                {discordLinked && discordUsername && (
                  <span className="ml-2 text-xs text-text-muted">({discordUsername})</span>
                )}
              </div>
            </div>
            {discordLinked ? (
              <div className="flex items-center gap-2">
                <span className="rounded-full bg-accent-success/10 px-2.5 py-0.5 text-xs font-medium text-accent-success">
                  {t("discordLinked")}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={handleDiscordUnlink}
                  disabled={discordLoading}
                >
                  {t("discordUnlink")}
                </Button>
              </div>
            ) : (
              <Button
                variant="secondary"
                size="sm"
                onClick={handleDiscordLink}
                disabled={discordLoading || discordStatus?.configured === false}
              >
                {discordLoading ? t("discordLinking") : (
                  discordStatus?.configured === false ? t("discordNotConfigured") : t("connect")
                )}
              </Button>
            )}
          </div>

          {/* GitHub */}
          <div className="flex items-center justify-between rounded-lg border border-border bg-bg-primary p-3">
            <div className="flex items-center gap-3">
              <span className="text-lg">🐙</span>
              <span className="text-sm font-medium">GitHub</span>
            </div>
            <Button variant="secondary" size="sm" disabled title={t("comingSoon")}>{t("connect")}</Button>
          </div>

          {/* Google */}
          <div className="flex items-center justify-between rounded-lg border border-border bg-bg-primary p-3">
            <div className="flex items-center gap-3">
              <span className="text-lg">🔵</span>
              <span className="text-sm font-medium">Google</span>
            </div>
            <Button variant="secondary" size="sm" disabled title={t("comingSoon")}>{t("connect")}</Button>
          </div>
        </div>
      </div>

      {/* 계정 삭제 */}
      <div className="rounded-xl border border-accent-error/30 bg-accent-error/5 p-6">
        <h3 className="mb-2 font-medium text-accent-error">{t("deleteAccount")}</h3>
        <p className="mb-4 text-sm text-text-secondary">
          {t("deleteWarning")}
        </p>
        <input
          value={deleteConfirm}
          onChange={(e) => setDeleteConfirm(e.target.value)}
          placeholder={t("deleteConfirmPlaceholder")}
          className="mb-3 w-full rounded-lg border border-accent-error/30 bg-bg-primary px-4 py-3 text-sm focus:border-accent-error focus:outline-none"
        />
        <Button
          variant="ghost"
          disabled={deleting || deleteConfirm !== "DELETE"}
          onClick={() => setShowDeleteModal(true)}
        >
          {t("deletePermanently")}
        </Button>
      </div>

      <ConfirmModal
        open={showDeleteModal}
        title={t("deleteAccount")}
        description={t("deleteWarning")}
        confirmLabel={t("deletePermanently")}
        variant="danger"
        onConfirm={handleDeleteAccount}
        onCancel={() => setShowDeleteModal(false)}
      />
    </div>
  );
}
