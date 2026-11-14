"use client";

import { useState, useEffect, useLayoutEffect, useRef } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { apiFetch, apiFormFetch, ApiError, captureApiRequestOwner, isCurrentApiRequestOwner, type ApiRequestOwner } from "@/lib/api-client";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { richContentVisibleLength } from "@/lib/rich-content.mjs";
import { useAuth } from "@/providers/AuthProvider";

const BJ_TIERS = [
  "Unrated","Bronze V","Bronze IV","Bronze III","Bronze II","Bronze I",
  "Silver V","Silver IV","Silver III","Silver II","Silver I",
  "Gold V","Gold IV","Gold III","Gold II","Gold I",
  "Platinum V","Platinum IV","Platinum III","Platinum II","Platinum I",
  "Diamond V","Diamond IV","Diamond III","Diamond II","Diamond I",
  "Ruby V","Ruby IV","Ruby III","Ruby II","Ruby I","Master",
];
function getBjTierName(tier: number | null): string {
  if (tier === null || tier < 0 || tier >= BJ_TIERS.length) return "Unrated";
  return BJ_TIERS[tier];
}

interface MeData {
  username: string;
  bio: string | null;
  avatar_url: string | null;
  discord_id: string | null;
  discord_username: string | null;
  baekjoon_handle: string | null;
  baekjoon_tier: number | null;
}

export default function ProfileSettingsPage() {
  const t = useTranslations("settings");
  const tc = useTranslations("common");
  const { user } = useAuth();
  const userId = user?.id;
  const ownerGeneration = useRef(0);
  useLayoutEffect(() => () => { ownerGeneration.current += 1; }, [userId]);

  const [username, setUsername] = useState("");
  const [bio, setBio] = useState("");
  const [originalUsername, setOriginalUsername] = useState("");
  const [originalBio, setOriginalBio] = useState("");
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [avatarUrl, setAvatarUrl] = useState<string | null>(null);
  const [originalAvatarUrl, setOriginalAvatarUrl] = useState<string | null>(null);
  const [pendingAvatarFile, setPendingAvatarFile] = useState<File | null>(null);
  const [pendingAvatarPreview, setPendingAvatarPreview] = useState<string | null>(null);
  const [pendingAvatarDelete, setPendingAvatarDelete] = useState(false);
  const avatarInputRef = useRef<HTMLInputElement>(null);

  // 백준/디스코드 연동
  const [discordUsername, setDiscordUsername] = useState<string | null>(null);
  const [baekjoonHandle, setBaekjoonHandle] = useState<string | null>(null);
  const [baekjoonTier, setBaekjoonTier] = useState<number | null>(null);
  const [bjInput, setBjInput] = useState("");
  const [bjLinking, setBjLinking] = useState(false);
  const [bjVerifyCode, setBjVerifyCode] = useState<string | null>(null);
  const [bjVerifying, setBjVerifying] = useState(false);

  useEffect(() => {
    if (!userId) return;
    const generation = ++ownerGeneration.current;
    const controller = new AbortController();
    setLoading(true);
    apiFetch<MeData>("/api/auth/me", { signal: controller.signal })
      .then((data) => {
        if (generation !== ownerGeneration.current || controller.signal.aborted) return;
        setUsername(data.username);
        setBio(data.bio || "");
        setOriginalUsername(data.username);
        setOriginalBio(data.bio || "");
        setAvatarUrl(data.avatar_url || null);
        setOriginalAvatarUrl(data.avatar_url || null);
        setDiscordUsername(data.discord_username || null);
        setBaekjoonHandle(data.baekjoon_handle || null);
        setBaekjoonTier(data.baekjoon_tier ?? null);
      })
      .catch(() => { if (generation === ownerGeneration.current && !controller.signal.aborted) setMessage({ type: "error", text: t("loadFailed") }); })
      .finally(() => { if (generation === ownerGeneration.current && !controller.signal.aborted) setLoading(false); });
    return () => { ownerGeneration.current += 1; controller.abort(); };
  }, [t, userId]);

  // 미리보기 URL 정리
  useEffect(() => {
    return () => {
      if (pendingAvatarPreview) URL.revokeObjectURL(pendingAvatarPreview);
    };
  }, [pendingAvatarPreview]);

  const hasAvatarChanges = pendingAvatarFile !== null || pendingAvatarDelete;
  const hasChanges = username !== originalUsername || bio !== originalBio || hasAvatarChanges;

  const handleSave = async () => {
    if (!userId || saving || !hasChanges) return;
    let requestOwner: ApiRequestOwner;
    try {
      requestOwner = captureApiRequestOwner(userId);
    } catch (err) {
      setMessage({ type: "error", text: err instanceof ApiError ? err.message : t("saveFailed") });
      return;
    }
    const generation = ownerGeneration.current;
    const isCurrent = () => generation === ownerGeneration.current && isCurrentApiRequestOwner(requestOwner);
    setSaving(true);
    setMessage(null);

    try {
      // 1) 아바타 삭제 처리
      if (pendingAvatarDelete) {
        await apiFetch("/api/auth/me/avatar", { method: "DELETE", requestOwner });
        if (!isCurrent()) return;
        setAvatarUrl(null);
        setOriginalAvatarUrl(null);
        setPendingAvatarDelete(false);
      }

      // 2) 아바타 업로드 처리
      if (pendingAvatarFile) {
        const formData = new FormData();
        formData.append("file", pendingAvatarFile);
        const res = await apiFormFetch<{ avatar_url: string }>(
          "/api/auth/me/avatar",
          formData,
          { requestOwner },
        );
        if (!isCurrent()) return;
        setAvatarUrl(res.avatar_url);
        setOriginalAvatarUrl(res.avatar_url);
        setPendingAvatarFile(null);
        if (pendingAvatarPreview) {
          URL.revokeObjectURL(pendingAvatarPreview);
          setPendingAvatarPreview(null);
        }
      }

      // 3) 프로필 정보 변경 처리
      if (username !== originalUsername || bio !== originalBio) {
        const body: Record<string, string> = {};
        if (username !== originalUsername) body.username = username;
        if (bio !== originalBio) body.bio = bio;
        await apiFetch("/api/auth/me", { method: "PATCH", body, requestOwner });
        if (!isCurrent()) return;
        setOriginalUsername(username);
        setOriginalBio(bio);
      }

      setMessage({ type: "success", text: t("saved") });
    } catch (err) {
      if (!isCurrent()) return;
      const msg = err instanceof ApiError ? err.message : t("saveFailed");
      setMessage({ type: "error", text: msg });
    } finally {
      if (generation === ownerGeneration.current) setSaving(false);
    }
  };

  // 현재 표시할 아바타 이미지 결정
  const displayAvatar = pendingAvatarDelete
    ? null
    : pendingAvatarPreview || avatarUrl;

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">{t("profileSettings")}</h2>

      {message && (
        <div
          className={`rounded-lg px-4 py-3 text-sm ${
            message.type === "success"
              ? "border border-green-500/30 bg-green-500/10 text-green-400"
              : "border border-status-error/30 bg-status-error/10 text-status-error"
          }`}
        >
          {message.text}
        </div>
      )}

      {/* 아바타 */}
      <div>
        <label className="mb-2 block text-sm font-medium text-text-secondary">{t("profileImage")}</label>
        <div className="flex items-center gap-4">
          {displayAvatar ? (
            <img
              src={displayAvatar}
              alt={username}
              className="h-20 w-20 rounded-full object-cover border border-border"
            />
          ) : (
            <div className="flex h-20 w-20 items-center justify-center rounded-full bg-accent-primary/10 text-2xl font-bold text-accent-primary">
              {username ? username[0].toUpperCase() : "?"}
            </div>
          )}
          <div className="flex flex-col gap-2">
            <input
              ref={avatarInputRef}
              type="file"
              accept="image/png,image/jpeg,image/gif,image/webp"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                if (file.size > 2 * 1024 * 1024) {
                  setMessage({ type: "error", text: t("avatarTooLarge") });
                  e.target.value = "";
                  return;
                }
                // 이전 미리보기 URL 정리
                if (pendingAvatarPreview) URL.revokeObjectURL(pendingAvatarPreview);
                setPendingAvatarFile(file);
                setPendingAvatarPreview(URL.createObjectURL(file));
                setPendingAvatarDelete(false);
                setMessage(null);
                e.target.value = "";
              }}
            />
            <Button
              variant="secondary"
              size="sm"
              onClick={() => avatarInputRef.current?.click()}
            >
              {t("changeImage")}
            </Button>
            {displayAvatar && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  if (pendingAvatarPreview) URL.revokeObjectURL(pendingAvatarPreview);
                  setPendingAvatarFile(null);
                  setPendingAvatarPreview(null);
                  if (originalAvatarUrl) {
                    setPendingAvatarDelete(true);
                  }
                  setMessage(null);
                }}
              >
                {t("removeImage")}
              </Button>
            )}
            {hasAvatarChanges && (
              <p className="text-xs text-accent-warning">{t("unsavedChanges")}</p>
            )}
          </div>
        </div>
      </div>

      {/* 닉네임 */}
      <div>
        <label className="mb-2 block text-sm font-medium text-text-secondary">{t("nickname")}</label>
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
          maxLength={50}
        />
        <p className="mt-1 text-xs text-text-muted">
          {t("nicknameHint")}
        </p>
      </div>

      {/* 자기소개 */}
      <div>
        <label className="mb-2 block text-sm font-medium text-text-secondary">{t("bio")}</label>
        <RichTextEditor
          value={bio}
          onChange={setBio}
          ariaLabel={t("bio")}
        />
        <p className="mt-1 text-xs text-text-muted">
          {richContentVisibleLength(bio)}/500
        </p>
      </div>

      <Button onClick={handleSave} disabled={saving || !hasChanges}>
        {saving ? t("saving") : tc("save")}
      </Button>

      {/* 외부 연동 */}
      <div className="border-t border-border pt-6 mt-6 space-y-6">
        <h2 className="text-lg font-semibold">{t("socialConnect")}</h2>

        {/* 디스코드 */}
        <div className="rounded-lg border border-border bg-bg-surface p-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <svg className="h-6 w-6 text-[#5865F2]" viewBox="0 0 24 24" fill="currentColor">
                <path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128 10.2 10.2 0 0 0 .372-.292.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127 12.299 12.299 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839 19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03z" />
              </svg>
              <div>
                <p className="text-sm font-medium">Discord</p>
                {discordUsername ? (
                  <p className="text-xs text-accent-success">{discordUsername}</p>
                ) : (
                  <p className="text-xs text-text-muted">{t("notLinked")}</p>
                )}
              </div>
            </div>
            {discordUsername ? (
              <Button
                variant="secondary"
                size="sm"
                onClick={async () => {
                  try {
                    await apiFetch("/api/auth/discord/unlink", { method: "POST" });
                    setDiscordUsername(null);
                    setMessage({ type: "success", text: "Discord 연동이 해제되었습니다." });
                  } catch (err) {
                    setMessage({ type: "error", text: err instanceof ApiError ? err.message : "실패" });
                  }
                }}
              >
                {t("unlink")}
              </Button>
            ) : (
              <Button
                size="sm"
                onClick={async () => {
                  try {
                    const res = await apiFetch<{ url: string }>("/api/auth/discord/link");
                    window.location.href = res.url;
                  } catch (err) {
                    setMessage({ type: "error", text: err instanceof ApiError ? err.message : "실패" });
                  }
                }}
              >
                {t("link")}
              </Button>
            )}
          </div>
        </div>

        {/* 백준(solved.ac) */}
        <section data-testid="baekjoon-settings" aria-labelledby="baekjoon-settings-title" className="min-w-0 rounded-lg border border-border bg-bg-surface p-4">
          <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
            <div className="flex min-w-0 items-center gap-3">
              <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded bg-[#17ce3a] text-white text-xs font-bold">B</div>
              <div className="min-w-0">
                <p id="baekjoon-settings-title" className="text-sm font-medium">{t("baekjoon")}</p>
                {baekjoonHandle ? (
                  <p className="break-words text-xs text-accent-success">{baekjoonHandle} — {getBjTierName(baekjoonTier)}</p>
                ) : (
                  <p className="text-xs text-text-muted">{t("notLinked")}</p>
                )}
              </div>
            </div>
            {baekjoonHandle ? (
              <div className="flex w-full min-w-0 flex-wrap items-center gap-2 sm:w-auto">
                <Button
                  variant="secondary"
                  size="sm"
                  className="min-h-11 shrink-0 whitespace-nowrap"
                  onClick={async () => {
                    try {
                      const res = await apiFetch<{ tier: number; tier_name: string }>("/api/auth/baekjoon/refresh", { method: "POST" });
                      setBaekjoonTier(res.tier);
                      setMessage({ type: "success", text: t("tierRefreshed") });
                    } catch (err) {
                      setMessage({ type: "error", text: err instanceof ApiError ? err.message : t("baekjoonActionFailed") });
                    }
                  }}
                >
                  {t("refresh")}
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  className="min-h-11 shrink-0 whitespace-nowrap"
                  onClick={async () => {
                    try {
                      await apiFetch("/api/auth/baekjoon/unlink", { method: "POST" });
                      setBaekjoonHandle(null);
                      setBaekjoonTier(null);
                      setMessage({ type: "success", text: t("baekjoonUnlinked") });
                    } catch (err) {
                      setMessage({ type: "error", text: err instanceof ApiError ? err.message : t("baekjoonActionFailed") });
                    }
                  }}
                >
                  {t("unlink")}
                </Button>
              </div>
            ) : bjVerifyCode ? (
              /* 2단계: 인증 코드 확인 */
              <div className="w-full min-w-0 space-y-2 text-right sm:w-auto">
                <div className="rounded-lg border border-accent-warning/30 bg-accent-warning/5 px-3 py-2 text-left">
                  <p className="break-words text-xs text-text-secondary">{t("baekjoonVerificationInstruction")}</p>
                  <p className="mt-1 break-all font-mono text-sm font-bold text-accent-warning">{bjVerifyCode}</p>
                </div>
                <div className="flex flex-wrap justify-end gap-2">
                  <Button variant="secondary" size="sm" className="min-h-11 shrink-0 whitespace-nowrap" onClick={() => { setBjVerifyCode(null); setBjInput(""); }}>
                    {tc("cancel")}
                  </Button>
                  <Button
                    size="sm"
                    className="min-h-11 shrink-0 whitespace-nowrap"
                    disabled={bjVerifying}
                    onClick={async () => {
                      setBjVerifying(true);
                      try {
                        const res = await apiFetch<{ handle: string; tier: number }>("/api/auth/baekjoon/verify", { method: "POST" });
                        setBaekjoonHandle(res.handle);
                        setBaekjoonTier(res.tier);
                        setBjVerifyCode(null);
                        setBjInput("");
                        setMessage({ type: "success", text: t("baekjoonLinked") });
                      } catch (err) {
                        setMessage({ type: "error", text: err instanceof ApiError ? err.message : t("baekjoonVerifyFailed") });
                      } finally {
                        setBjVerifying(false);
                      }
                    }}
                  >
                    <span className="inline-grid">
                      <span className={`col-start-1 row-start-1${bjVerifying ? " invisible" : ""}`} aria-hidden={bjVerifying}>{t("baekjoonVerify")}</span>
                      <span className={`col-start-1 row-start-1${bjVerifying ? "" : " invisible"}`} aria-hidden={!bjVerifying}>{tc("processing")}</span>
                    </span>
                  </Button>
                </div>
              </div>
            ) : (
              /* 1단계: 아이디 입력 */
              <div className="flex w-full min-w-0 flex-wrap items-center gap-2 sm:w-auto">
                <input
                  id="baekjoon-handle"
                  aria-label={t("baekjoonHandle")}
                  value={bjInput}
                  onChange={(e) => setBjInput(e.target.value)}
                  placeholder={t("baekjoonHandle")}
                  className="min-h-11 w-full min-w-0 flex-1 rounded-lg border border-border bg-bg-primary px-3 py-1.5 text-sm focus:border-accent-primary focus:outline-none sm:w-48"
                />
                <Button
                  size="sm"
                  className="min-h-11 shrink-0 whitespace-nowrap"
                  disabled={bjLinking || !bjInput.trim()}
                  onClick={async () => {
                    setBjLinking(true);
                    try {
                      const res = await apiFetch<{ code: string }>("/api/auth/baekjoon/start-link", {
                        method: "POST",
                        body: { handle: bjInput.trim() },
                      });
                      setBjVerifyCode(res.code);
                    } catch (err) {
                      setMessage({ type: "error", text: err instanceof ApiError ? err.message : t("baekjoonActionFailed") });
                    } finally {
                      setBjLinking(false);
                    }
                  }}
                >
                  <span className="inline-grid">
                    <span className={`col-start-1 row-start-1${bjLinking ? " invisible" : ""}`} aria-hidden={bjLinking}>{t("link")}</span>
                    <span className={`col-start-1 row-start-1${bjLinking ? "" : " invisible"}`} aria-hidden={!bjLinking}>{tc("processing")}</span>
                  </span>
                </Button>
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
