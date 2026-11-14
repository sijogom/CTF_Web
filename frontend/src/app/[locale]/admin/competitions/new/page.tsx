"use client";

import { useState, useRef } from "react";
import { useRouter } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { apiFetch, ApiError } from "@/lib/api-client";
import type { CompetitionMode } from "@/types/competition";
import { BannerUploadZone } from "@/components/admin/competitions/BannerUploadZone";
import { useImageEditOperation } from "@/lib/use-image-edit-operation";
import { useUnsavedChangesGuard } from "@/lib/use-unsaved-changes-guard";

export default function NewCompetitionPage() {
  const t = useTranslations("admin");
  const tctf = useTranslations("ctf");
  const tc = useTranslations("common");
  const router = useRouter();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState<CompetitionMode>("individual");
  const [startDate, setStartDate] = useState("");
  const [startTime, setStartTime] = useState("09:00");
  const [endDate, setEndDate] = useState("");
  const [endTime, setEndTime] = useState("18:00");
  const [freezeMinutes, setFreezeMinutes] = useState(60);
  const [bannerUrl, setBannerUrl] = useState<string | null>(null);
  const bannerUrlRef = useRef(bannerUrl);
  const { operation, busy, uploading: bannerUploading, kind } = useImageEditOperation("competition:new");
  const loading = kind === "save";
  const [error, setError] = useState<string | null>(null);
  const isDirty = title.trim() !== "" || description.trim() !== "" || bannerUrl !== null
    || mode !== "individual" || startDate !== "" || startTime !== "09:00"
    || endDate !== "" || endTime !== "18:00" || freezeMinutes !== 60;
  const leaveGuard = useUnsavedChangesGuard(isDirty, { busy });
  const handleBannerChange = (url: string) => {
    bannerUrlRef.current = url;
    setBannerUrl(url);
  };

  const handleSubmit = async () => {
    if (operation.busy) return;
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
      await apiFetch<{ id: string }>("/api/ctf/competitions", {
        method: "POST",
        requestOwner: lease.owner,
        body: {
          title,
          description,
          mode,
          start_at: `${startDate}T${startTime}`,
          end_at: `${endDate}T${endTime}`,
          freeze_minutes: parseInt(String(freezeMinutes)) || 0,
          banner_url: bannerUrlRef.current,
        },
      });
      if (operation.isCurrent(lease)) router.push(`/admin/competitions`);
    } catch (err) {
      if (!lease || operation.isCurrent(lease)) setError(err instanceof ApiError ? err.message : tc("errorOccurred"));
    } finally {
      if (lease) operation.release(lease);
    }
  };

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <h1 className="text-2xl font-bold">{t("createCompetitionTitle")}</h1>

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

      <div>
        <label htmlFor="comp-title" className="mb-1.5 block text-sm font-medium">
          {t("competitionTitleLabel")} <span className="text-accent-error">*</span>
        </label>
        <input
          id="comp-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder={t("compPlaceholder")}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
        />
      </div>

      <div>
        <label className="mb-1.5 block text-sm font-medium">{t("compModeLabel")}</label>
        <div className="flex gap-3">
          {(["individual", "team"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              className={`rounded-lg px-4 py-2.5 text-sm font-medium transition-colors ${
                mode === m
                  ? "bg-accent-primary text-white"
                  : "border border-border bg-bg-surface text-text-secondary hover:text-text-primary"
              }`}
            >
              {m === "individual" ? tctf("individualMode") : tctf("teamMode")}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <label className="mb-1.5 block text-sm font-medium">
            {t("startTime")} <span className="text-accent-error">*</span>
          </label>
          <div className="flex gap-2">
            <input
              id="comp-start-date"
              type="date"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              className="datetime-input flex-1 rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
            />
            <input
              id="comp-start-time"
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
              id="comp-end-date"
              type="date"
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              className="datetime-input flex-1 rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
            />
            <input
              id="comp-end-time"
              type="time"
              value={endTime}
              onChange={(e) => setEndTime(e.target.value)}
              className="datetime-input w-28 rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
            />
          </div>
        </div>
      </div>

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

      <div>
        <label className="mb-1.5 block text-sm font-medium">{t("competitionDescLabel")}</label>
        <RichTextEditor
          value={description}
          onChange={setDescription}
          placeholder={t("compDescPlaceholder")}
        />
      </div>

      <div className="rounded-lg border border-accent-primary/20 bg-accent-primary/5 p-4 text-sm text-text-secondary">
        <p>{t("compCreateGuide")}</p>
      </div>

      <div className="flex justify-end gap-3">
        <Button variant="ghost" disabled={busy} onClick={() => { if (!operation.busy) leaveGuard.requestDiscard(() => router.back()); }}>
          {tc("cancel")}
        </Button>
        <Button onClick={handleSubmit} disabled={busy || !title.trim() || !startDate || !startTime || !endDate || !endTime}>
          {loading ? tc("processing") : t("createCompetitionBtn")}
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
