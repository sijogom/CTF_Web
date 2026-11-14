"use client";

import { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";
import { useLocale } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import {
  RetentionDeletionDialog,
  type RetentionDeletionImpact,
} from "@/components/admin/RetentionDeletionDialog";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import { useAuth } from "@/providers/AuthProvider";
import { isFixedRosterCompetition } from "@/lib/competition-presentation";
import type { CompetitionTeamContract } from "@/types/competition";

interface Competition {
  id: string;
  title: string;
  description: string | null;
  mode: string;
  team_contract?: CompetitionTeamContract;
  start_at: string;
  end_at: string;
  freeze_minutes: number;
  banner_url: string | null;
  is_published: boolean;
  is_deleted: boolean;
  participant_count: number;
  status: string;
}

const FILTER_KEYS = {
  all: "filterAll",
  upcoming: "filterUpcoming",
  ongoing: "filterOngoing",
  ended: "filterEnded",
} as const;

const STATUS_BADGE: Record<string, string> = {
  upcoming: "bg-accent-primary/10 text-accent-primary",
  ongoing: "bg-accent-success/10 text-accent-success",
  ended: "bg-bg-surface text-text-muted",
};

export default function AdminCompetitionsPage() {
  const t = useTranslations("admin");
  const tctf = useTranslations("ctf");
  const tc = useTranslations("common");
  const locale = useLocale();
  const { isSuperAdmin } = useAuth();

  const [competitions, setCompetitions] = useState<Competition[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState<"all" | "upcoming" | "ongoing" | "ended">("all");
  const [showArchived, setShowArchived] = useState(false);
  const [publishLoading, setPublishLoading] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const [deleteImpact, setDeleteImpact] = useState<RetentionDeletionImpact | null>(null);
  const [deleteImpactLoading, setDeleteImpactLoading] = useState(false);
  const [deleteSubmitting, setDeleteSubmitting] = useState(false);
  const { toast } = useToast();

  const fetchCompetitions = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams();
      params.set("all", "true");
      if (filter !== "all") params.set("status", filter);
      if (showArchived) params.set("include_deleted", "true");
      const url = `/api/ctf/competitions?${params.toString()}`;
      const res = await apiFetch<Competition[]>(url);
      setCompetitions(res);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setLoading(false);
    }
  }, [filter, showArchived, tc]);

  useEffect(() => {
    fetchCompetitions();
  }, [fetchCompetitions]);

  const handleTogglePublish = async (compId: string) => {
    setPublishLoading(compId);
    try {
      await apiFetch(`/api/ctf/competitions/${compId}/publish`, { method: "PATCH" });
      await fetchCompetitions();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setPublishLoading(null);
    }
  };

  const handleRestore = async (compId: string) => {
    if (!isSuperAdmin) return;
    setPublishLoading(compId);
    try {
      await apiFetch(`/api/ctf/competitions/${compId}/restore`, { method: "POST" });
      toast("success", t("deletionRestoreSuccess"));
      await fetchCompetitions();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setPublishLoading(null);
    }
  };

  const openDeletionDialog = async (competition: Competition) => {
    if (!isSuperAdmin) return;
    setDeleteTarget({ id: competition.id, name: competition.title });
    setDeleteImpact(null);
    setDeleteImpactLoading(true);
    try {
      const impact = await apiFetch<RetentionDeletionImpact>(
        `/api/ctf/competitions/${competition.id}/deletion-impact`,
      );
      setDeleteImpact(impact);
    } catch (err) {
      setDeleteTarget(null);
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setDeleteImpactLoading(false);
    }
  };

  const closeDeletionDialog = () => {
    if (deleteSubmitting) return;
    setDeleteTarget(null);
    setDeleteImpact(null);
  };

  const handleDelete = async ({
    reason,
    targetName,
  }: {
    reason: string;
    targetName: string;
  }) => {
    if (!isSuperAdmin || !deleteTarget) return;
    setDeleteSubmitting(true);
    try {
      const confirmation = await apiFetch<{ confirmation_token: string }>(
        `/api/ctf/competitions/${deleteTarget.id}/deletion-confirmations`,
        {
          method: "POST",
          body: { reason, target_name: targetName },
        },
      );
      await apiFetch(`/api/ctf/competitions/${deleteTarget.id}`, {
        method: "DELETE",
        body: { confirmation_token: confirmation.confirmation_token },
      });
      toast("success", t("deleteSuccess"));
      setDeleteTarget(null);
      setDeleteImpact(null);
      await fetchCompetitions();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setDeleteSubmitting(false);
    }
  };

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    return d.toLocaleDateString(locale, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t("competitionManagement")}</h1>
        <Link href="/admin/competitions/new">
          <Button>{t("newCompetition")}</Button>
        </Link>
      </div>

      {/* 필터 탭 */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex gap-2">
          {(["all", "upcoming", "ongoing", "ended"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
                filter === f
                  ? "bg-accent-primary text-white"
                  : "text-text-secondary hover:bg-bg-surface"
              }`}
            >
              {t(FILTER_KEYS[f])}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-2 text-sm text-text-secondary">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(event) => setShowArchived(event.target.checked)}
            className="h-4 w-4 rounded border-border accent-accent-primary"
          />
          {t("deletionShowArchived")}
        </label>
      </div>

      {/* 로딩 */}
      {loading && (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-20 animate-pulse rounded-lg border border-border bg-bg-surface" />
          ))}
        </div>
      )}

      {/* 에러 */}
      {!loading && error && (
        <div className="py-8 text-center text-text-muted">
          <p>{error}</p>
          <button onClick={fetchCompetitions} className="mt-2 text-accent-primary hover:underline">{tc("retry")}</button>
        </div>
      )}

      {/* 빈 상태 */}
      {!loading && !error && competitions.length === 0 && (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border p-12 text-center">
          <h3 className="mb-1 text-lg font-semibold">{t("noCompetitions")}</h3>
          <p className="mb-4 text-sm text-text-secondary">{t("noCompetitionsDesc")}</p>
          <Link href="/admin/competitions/new">
            <Button>{t("newCompetition")}</Button>
          </Link>
        </div>
      )}

      {/* 대회 카드 목록 */}
      {!loading && !error && competitions.length > 0 && (
        <div className="grid gap-4">
          {competitions.map((comp) => (
            <div
              key={comp.id}
              className="flex items-center justify-between rounded-xl border border-border bg-bg-surface p-5 transition-colors hover:bg-bg-surface/80"
            >
              <div className="flex items-center gap-4">
                {comp.banner_url && (
                  <img
                    src={comp.banner_url}
                    alt={comp.title}
                    loading="lazy"
                    className="h-14 w-20 flex-shrink-0 rounded-lg object-cover"
                  />
                )}
              <div className="space-y-1">
                <div className="flex items-center gap-3">
                  <h3 className="font-semibold">{comp.title}</h3>
                  {comp.is_deleted ? (
                    <span className="rounded-full bg-accent-error/10 px-2.5 py-0.5 text-xs font-medium text-accent-error">
                      {t("deletionArchived")}
                    </span>
                  ) : (
                    <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_BADGE[comp.status] ?? ""}`}>
                      {tctf(comp.status)}
                    </span>
                  )}
                  <span className="rounded-full bg-bg-primary px-2.5 py-0.5 text-xs font-medium text-text-muted">
                    {comp.mode === "team" ? tctf("teamMode") : tctf("individualMode")}
                  </span>
                  {!comp.is_deleted && (
                    <button
                      onClick={(e) => { e.stopPropagation(); void handleTogglePublish(comp.id); }}
                      disabled={publishLoading === comp.id}
                      className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                        comp.is_published
                          ? "bg-accent-success/10 text-accent-success hover:bg-accent-success/20"
                          : "bg-text-muted/10 text-text-muted hover:bg-text-muted/20"
                      }`}
                    >
                      {publishLoading === comp.id ? "..." : comp.is_published ? t("published") : t("unpublished")}
                    </button>
                  )}
                </div>
                <div className="flex items-center gap-4 text-xs text-text-muted">
                  <span>{formatDate(comp.start_at)} ~ {formatDate(comp.end_at)}</span>
                  <span>{tctf(isFixedRosterCompetition(comp) ? "registeredTeamCount" : "participantCount", { count: comp.participant_count })}</span>
                </div>
              </div>
              </div>
              <div className="flex items-center gap-2">
                {comp.is_deleted ? isSuperAdmin && (
                  <button
                    onClick={() => { void handleRestore(comp.id); }}
                    disabled={publishLoading === comp.id}
                    className="rounded-lg bg-accent-success/10 px-3 py-1.5 text-xs font-medium text-accent-success hover:bg-accent-success/20 disabled:opacity-50"
                  >
                    {publishLoading === comp.id ? tc("loading") : t("deletionRestore")}
                  </button>
                ) : (
                  <>
                    <Link href={`/admin/competitions/${comp.id}`}>
                      <Button variant="secondary" size="sm">{t("chalEdit")}</Button>
                    </Link>
                    {isSuperAdmin && <button
                      onClick={() => { void openDeletionDialog(comp); }}
                      className="rounded-lg bg-accent-error/10 px-3 py-1.5 text-xs font-medium text-accent-error hover:bg-accent-error/20"
                    >
                      {t("deleteCompetition")}
                    </button>}
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      <RetentionDeletionDialog
        open={isSuperAdmin && deleteTarget !== null}
        target={deleteTarget}
        impact={deleteImpact}
        loadingImpact={deleteImpactLoading}
        submitting={deleteSubmitting}
        labels={{
          title: t("deleteCompetition"),
          resourceLabel: t("deletionResourceCompetition"),
          retainedDescription: t("deletionRetainedDescription"),
          impactTitle: t("deletionImpactTitle"),
          impactLoading: t("deletionImpactLoading"),
          impactEmpty: t("deletionImpactEmpty"),
          reasonLabel: t("deletionReasonLabel"),
          reasonPlaceholder: t("deletionReasonPlaceholder"),
          targetNameLabel: t("deletionTargetNameLabel"),
          targetNameHelp: t("deletionTargetNameHelp"),
          next: t("deletionNext"),
          cancel: tc("cancel"),
          execute: t("deletionExecute"),
          executing: t("deletionExecuting"),
          stageLabel: t("deletionStage", { stage: "{stage}" }),
        }}
        impactLabels={{
          challenge_mappings: t("deletionImpactChallengeMappings"),
          assignments: t("deletionImpactAssignments"),
          participants: t("deletionImpactParticipants"),
          solves: t("deletionImpactSolves"),
          registered_teams: t("deletionImpactRegisteredTeams"),
          team_members: t("deletionImpactTeamMembers"),
          team_assignments: t("deletionImpactTeamAssignments"),
          team_solves: t("deletionImpactTeamSolves"),
        }}
        onClose={closeDeletionDialog}
        onSubmit={handleDelete}
      />
    </div>
  );
}
