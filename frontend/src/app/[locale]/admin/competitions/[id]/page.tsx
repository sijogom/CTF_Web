"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useParams } from "next/navigation";
import { useRouter } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { apiFetch, ApiError } from "@/lib/api-client";
import { BannerUploadZone } from "@/components/admin/competitions/BannerUploadZone";
import { CompetitionChallengeManager } from "@/components/admin/competitions/CompetitionChallengeManager";
import { useToast } from "@/providers/ToastProvider";
import { useImageEditOperation } from "@/lib/use-image-edit-operation";
import { useUnsavedChangesGuard } from "@/lib/use-unsaved-changes-guard";
import { competitionModeChangeAllowed } from "@/lib/competition-presentation";
import type { CompetitionMode, CompetitionTeamContract } from "@/types/competition";

interface Competition {
  id: string;
  title: string;
  description: string | null;
  mode: CompetitionMode;
  team_contract?: CompetitionTeamContract;
  start_at: string;
  end_at: string;
  freeze_minutes: number;
  banner_url: string | null;
  is_published: boolean;
  participant_count: number;
  status: string;
}

/** ISO 문자열 → { date: "YYYY-MM-DD", time: "HH:MM" } */
function splitDateTime(iso: string): { date: string; time: string } {
  const d = new Date(iso);
  const date = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const time = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return { date, time };
}

export default function AdminCompetitionEditPage() {
  const t = useTranslations("admin");
  const tctf = useTranslations("ctf");
  const tc = useTranslations("common");
  const params = useParams();
  const router = useRouter();
  const compId = params.id as string;
  const { toast } = useToast();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState<CompetitionMode>("individual");
  const [teamContract, setTeamContract] = useState<CompetitionTeamContract>(null);
  const [startDate, setStartDate] = useState("");
  const [startTime, setStartTime] = useState("09:00");
  const [endDate, setEndDate] = useState("");
  const [endTime, setEndTime] = useState("18:00");
  const [freezeMinutes, setFreezeMinutes] = useState(60);
  const [bannerUrl, setBannerUrl] = useState<string | null>(null);
  const [isPublished, setIsPublished] = useState(false);
  const bannerUrlRef = useRef(bannerUrl);
  const { operation, busy, uploading: bannerUploading, kind, key: operationKey } = useImageEditOperation(`competition:${compId}`);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const formReady = loadedKey === operationKey;
  const saving = kind === "save";
  const [participantCount, setParticipantCount] = useState(0);
  const [compStatus, setCompStatus] = useState("upcoming");
  const [savedTitle, setSavedTitle] = useState("");
  const [savedDescription, setSavedDescription] = useState("");
  const [savedBannerUrl, setSavedBannerUrl] = useState<string | null>(null);
  const [savedSettings, setSavedSettings] = useState({
    mode: "individual" as CompetitionMode, startDate: "", startTime: "09:00", endDate: "", endTime: "18:00", freezeMinutes: 60,
  });
  const handleBannerChange = (url: string) => {
    bannerUrlRef.current = url;
    setBannerUrl(url);
  };

  const fetchCompetition = useCallback(async () => {
    let lease;
    try {
      lease = operation.claim("load");
      if (!lease) return;
      setLoading(true);
      setError(null);
      const data = await apiFetch<Competition>(`/api/ctf/competitions/${compId}`, { requestOwner: lease.owner });
      if (!operation.isCurrent(lease)) return;
      setTitle(data.title);
      setSavedTitle(data.title);
      setDescription(data.description || "");
      setSavedDescription(data.description || "");
      setMode(data.mode);
      setTeamContract(data.team_contract ?? null);
      const start = splitDateTime(data.start_at);
      setStartDate(start.date);
      setStartTime(start.time);
      const end = splitDateTime(data.end_at);
      setEndDate(end.date);
      setEndTime(end.time);
      setFreezeMinutes(data.freeze_minutes);
      setSavedSettings({
        mode: data.mode, startDate: start.date, startTime: start.time,
        endDate: end.date, endTime: end.time, freezeMinutes: data.freeze_minutes,
      });
      setBannerUrl(data.banner_url);
      bannerUrlRef.current = data.banner_url;
      setSavedBannerUrl(data.banner_url);
      setIsPublished(data.is_published);
      setParticipantCount(data.participant_count);
      setCompStatus(data.status);
      setLoadedKey(operationKey);
    } catch (err) {
      if (!lease || operation.isCurrent(lease)) {
        setError(err instanceof ApiError ? err.message : tc("error"));
        if (!lease) setLoading(false);
      }
    } finally {
      if (lease && operation.release(lease)) setLoading(false);
    }
  }, [compId, tc, operation, operationKey]);

  useEffect(() => {
    fetchCompetition();
  }, [fetchCompetition]);

  const handleSave = async () => {
    if (!formReady || loading || operation.busy) return;
    if (!title.trim() || !startDate || !startTime || !endDate || !endTime) return;
    const start = new Date(`${startDate}T${startTime}`);
    const end = new Date(`${endDate}T${endTime}`);
    if (end <= start) {
      setError(t("dateInversionError"));
      return;
    }
    let lease;
    try {
      lease = operation.claim("save");
      if (!lease) return;
      setError(null);
      const submitted = {
        title, description, mode, startDate, startTime, endDate, endTime, freezeMinutes, bannerUrl: bannerUrlRef.current,
      };
      await apiFetch(`/api/ctf/competitions/${compId}`, {
        method: "PATCH",
        requestOwner: lease.owner,
        body: {
          title: submitted.title,
          description: submitted.description || null,
          mode: submitted.mode,
          start_at: `${submitted.startDate}T${submitted.startTime}`,
          end_at: `${submitted.endDate}T${submitted.endTime}`,
          freeze_minutes: submitted.freezeMinutes,
          banner_url: submitted.bannerUrl,
        },
      });
      if (!operation.isCurrent(lease)) return;
      setSavedTitle(submitted.title);
      setSavedDescription(submitted.description);
      setSavedBannerUrl(submitted.bannerUrl);
      setSavedSettings({
        mode: submitted.mode, startDate: submitted.startDate, startTime: submitted.startTime,
        endDate: submitted.endDate, endTime: submitted.endTime, freezeMinutes: submitted.freezeMinutes,
      });
      toast("success", t("competitionSaved"));
    } catch (err) {
      if (!lease || operation.isCurrent(lease)) setError(err instanceof ApiError ? err.message : tc("errorOccurred"));
    } finally {
      if (lease) operation.release(lease);
    }
  };

  const isDirty = formReady && (title !== savedTitle || description !== savedDescription || bannerUrl !== savedBannerUrl
    || mode !== savedSettings.mode || startDate !== savedSettings.startDate || startTime !== savedSettings.startTime
    || endDate !== savedSettings.endDate || endTime !== savedSettings.endTime || freezeMinutes !== savedSettings.freezeMinutes);
  const leaveGuard = useUnsavedChangesGuard(isDirty, { busy });

  const handleTogglePublish = async () => {
    if (!formReady || operation.busy) return;
    let lease;
    try {
      lease = operation.claim("publish");
      if (!lease) return;
      const res = await apiFetch<{ is_published: boolean }>(
        `/api/ctf/competitions/${compId}/publish`,
        { method: "PATCH", requestOwner: lease.owner },
      );
      if (!operation.isCurrent(lease)) return;
      setIsPublished(res.is_published);
      toast("success", res.is_published ? t("publishBtn") : t("unpublishBtn"));
    } catch (err) {
      if (!lease || operation.isCurrent(lease)) toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      if (lease) operation.release(lease);
    }
  };

  if (loading || (!formReady && !error)) {
    return (
      <div className="mx-auto max-w-3xl space-y-4">
        <div className="h-8 w-48 animate-pulse rounded bg-bg-surface" />
        <div className="h-64 animate-pulse rounded-xl bg-bg-surface" />
      </div>
    );
  }

  if (!formReady) {
    return (
      <div className="mx-auto max-w-3xl py-12 text-center">
        <p className="text-text-muted">{error}</p>
        <button onClick={fetchCompetition} className="mt-2 text-accent-primary hover:underline">{tc("retry")}</button>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <Link href="/admin/competitions" className="text-sm text-text-muted hover:text-text-primary">
        {`\u2190 ${t("competitionManagement")}`}
      </Link>

      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t("editCompetition")}</h1>
        <div className="flex items-center gap-3">
          <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
            isPublished ? "bg-accent-success/10 text-accent-success" : "bg-accent-warning/10 text-accent-warning"
          }`}>
            {isPublished ? t("published") : t("unpublished")}
          </span>
          <button
            disabled={busy}
            onClick={handleTogglePublish}
            className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
              isPublished
                ? "bg-accent-warning/10 text-accent-warning hover:bg-accent-warning/20"
                : "bg-accent-success/10 text-accent-success hover:bg-accent-success/20"
            }`}
          >
            {isPublished ? t("unpublishBtn") : t("publishBtn")}
          </button>
        </div>
      </div>

      {/* 상태 정보 */}
      <div className="flex items-center gap-4 text-sm text-text-muted">
        <span>{tctf(compStatus)}</span>
        <span>{tctf(teamContract === "fixed_roster_v1" ? "registeredTeamCount" : "participantCount", { count: participantCount })}</span>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-accent-error/30 bg-accent-error/10 px-4 py-3 text-sm text-accent-error">
          {error}
        </div>
      )}

      {/* 배너 이미지 */}
      <BannerUploadZone
        bannerUrl={bannerUrl}
        bannerUploading={bannerUploading}
        disabled={busy}
        operation={operation}
        onBannerChange={handleBannerChange}
        onError={setError}
      />

      {/* 제목 */}
      <div>
        <label htmlFor="comp-title" className="mb-1.5 block text-sm font-medium">
          {t("competitionTitleLabel")} <span className="text-accent-error">*</span>
        </label>
        <input
          id="comp-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
        />
      </div>

      {/* 모드 */}
      <div>
        <label className="mb-1.5 block text-sm font-medium">{t("compModeLabel")}</label>
        <div className="flex gap-3">
          {(["individual", "team"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              aria-pressed={mode === m}
              disabled={busy || !competitionModeChangeAllowed(savedSettings.mode, m, compStatus, participantCount, teamContract)}
              className={`rounded-lg px-4 py-2.5 text-sm font-medium transition-colors ${
                mode === m
                  ? "bg-accent-primary text-white"
                  : "border border-border bg-bg-surface text-text-secondary hover:text-text-primary"
              } disabled:cursor-not-allowed disabled:opacity-60`}
            >
              {m === "individual" ? tctf("individualMode") : tctf("teamMode")}
            </button>
          ))}
        </div>
        {teamContract === "fixed_roster_v1" && <p className="mt-2 text-sm text-text-secondary">{tctf("fixedRosterMode")}</p>}
      </div>

      {/* 날짜/시간 */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <label className="mb-1.5 block text-sm font-medium">
            {t("startTime")} <span className="text-accent-error">*</span>
          </label>
          <div className="flex gap-2">
            <input
              type="date"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              className="datetime-input flex-1 rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
            />
            <input
              type="time"
              value={startTime}
              onChange={(e) => setStartTime(e.target.value)}
              className="datetime-input w-28 rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
            />
          </div>
        </div>
        <div className="space-y-2">
          <label className="mb-1.5 block text-sm font-medium">
            {t("endTime")} <span className="text-accent-error">*</span>
          </label>
          <div className="flex gap-2">
            <input
              type="date"
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              className="datetime-input flex-1 rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
            />
            <input
              type="time"
              value={endTime}
              onChange={(e) => setEndTime(e.target.value)}
              className="datetime-input w-28 rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
            />
          </div>
        </div>
      </div>

      {/* 프리즈 */}
      <div>
        <label htmlFor="comp-freeze" className="mb-1.5 block text-sm font-medium">
          {t("freezeDesc")}
        </label>
        <input
          id="comp-freeze"
          type="number"
          value={freezeMinutes}
          onChange={(e) => setFreezeMinutes(parseInt(e.target.value) || 0)}
          className="w-32 rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm text-text-primary"
        />
        <span className="ml-2 text-sm text-text-muted">{t("minuteUnit")}</span>
      </div>

      {/* 설명 */}
      <div>
        <label className="mb-1.5 block text-sm font-medium">{t("competitionDescLabel")}</label>
        <RichTextEditor
          value={description}
          onChange={setDescription}
          placeholder={t("compDescPlaceholder")}
        />
      </div>

      {/* 챌린지 관리 */}
      <CompetitionChallengeManager compId={compId} />

      {/* 저장 */}
      <div className="flex justify-end gap-3">
        <Button variant="ghost" disabled={busy} onClick={() => { if (!operation.busy) leaveGuard.requestDiscard(() => router.back()); }}>
          {tc("cancel")}
        </Button>
        <Button
          onClick={handleSave}
          disabled={busy || !title.trim() || !startDate || !startTime || !endDate || !endTime}
        >
          {saving ? tc("processing") : tc("save")}
        </Button>
      </div>

      <ConfirmModal
        open={leaveGuard.confirmDiscardOpen}
        title={tctf("unsavedChangesTitle")}
        description={tctf("unsavedChangesDesc")}
        confirmLabel={tctf("leaveAnyway")}
        variant="danger"
        onConfirm={() => { if (!operation.busy) leaveGuard.confirmDiscard(); }}
        onCancel={leaveGuard.cancelDiscard}
      />
    </div>
  );
}
