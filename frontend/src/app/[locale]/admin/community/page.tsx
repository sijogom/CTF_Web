"use client";

import { useState, useEffect, useCallback } from "react";
import { useTranslations, useLocale } from "next-intl";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import {
  ModerationActionDialog,
  type ModerationTarget,
} from "@/components/admin/ModerationActionDialog";

interface Report {
  id: string;
  reporter_id: string;
  target_type: string;
  target_id: string;
  reason: string;
  detail: string | null;
  status: string;
  reviewed_by: string | null;
  created_at: string;
  target_title: string | null;
  target_preview: string | null;
  target_author: string | null;
  target_deleted: boolean;
  target_hidden: boolean;
}

interface ReportsResponse {
  items: Report[];
  total: number;
  page: number;
  size: number;
}

const REASON_KEYS: Record<string, string> = {
  spam: "reasonSpam",
  abuse: "reasonAbuse",
  inappropriate: "reasonInappropriate",
  other: "reasonOther",
};

const STATUS_STYLES: Record<string, string> = {
  pending: "bg-accent-warning/10 text-accent-warning",
  reviewed: "bg-accent-success/10 text-accent-success",
  dismissed: "bg-text-muted/10 text-text-muted",
};

const STATUS_KEYS: Record<string, string> = {
  pending: "reportPending",
  reviewed: "reportReviewed",
  dismissed: "reportDismissed",
};

const TARGET_TYPE_KEYS: Record<string, string> = {
  post: "reportTargetPost",
  comment: "reportTargetComment",
  marketplace_item: "reportTargetMarketItem",
  marketplace_comment: "reportTargetMarketComment",
};

export default function AdminCommunityPage() {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const { toast } = useToast();
  const locale = useLocale();
  const localeMap: Record<string, string> = { ko: "ko-KR", en: "en-US", ja: "ja-JP", zh: "zh-CN" };
  const dateLocale = localeMap[locale] || "ko-KR";

  const [reports, setReports] = useState<Report[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("pending");
  const [page, setPage] = useState(1);
  const [processing, setProcessing] = useState<string | null>(null);
  const [moderationTarget, setModerationTarget] = useState<Report | null>(null);

  const fetchReports = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ page: String(page), size: "20" });
      if (statusFilter) params.set("status_filter", statusFilter);
      const data = await apiFetch<ReportsResponse>(`/api/community/reports?${params}`);
      setReports(data.items);
      setTotal(data.total);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("loadReportsFailed"));
    } finally {
      setLoading(false);
    }
  }, [page, statusFilter, t]);

  useEffect(() => {
    fetchReports();
  }, [fetchReports]);

  const handleStatusChange = async (reportId: string, newStatus: string) => {
    setProcessing(reportId);
    try {
      await apiFetch(`/api/community/reports/${reportId}`, {
        method: "PATCH",
        body: { status: newStatus },
      });
      await fetchReports();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : t("reportStatusFailed"));
    } finally {
      setProcessing(null);
    }
  };

  const handleReview = async (report: Report) => {
    if (report.target_deleted || report.target_hidden) {
      await handleStatusChange(report.id, "reviewed");
      return;
    }
    setModerationTarget(report);
  };

  const handleModerationCompleted = async () => {
    if (!moderationTarget) return;
    setProcessing(moderationTarget.id);
    try {
      await apiFetch(`/api/community/reports/${moderationTarget.id}`, {
        method: "PATCH",
        body: { status: "reviewed" },
      });
      setModerationTarget(null);
      await fetchReports();
      toast("success", t("moderationHideSuccess"));
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : t("reportStatusFailed"));
    } finally {
      setProcessing(null);
    }
  };

  const moderationDialogTarget: ModerationTarget | null = moderationTarget
    ? {
        type: moderationTarget.target_type as ModerationTarget["type"],
        id: moderationTarget.target_id,
      }
    : null;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{t("communityManagement")}</h1>

      {/* 필터 */}
      <div className="flex gap-2">
        {[
          { value: "pending", i18nKey: "reportPending" },
          { value: "reviewed", i18nKey: "reportReviewed" },
          { value: "dismissed", i18nKey: "reportDismissed" },
          { value: "", i18nKey: "filterAll" },
        ].map((f) => (
          <button
            key={f.value}
            onClick={() => { setStatusFilter(f.value); setPage(1); }}
            className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
              statusFilter === f.value
                ? "bg-accent-primary text-white"
                : "bg-bg-surface text-text-secondary hover:text-text-primary"
            }`}
          >
            {t(f.i18nKey)}
          </button>
        ))}
      </div>

      {error && (
        <div className="rounded-lg border border-status-error/30 bg-status-error/10 px-4 py-3 text-sm text-status-error">
          {error}
        </div>
      )}

      {/* 신고 목록 */}
      <div className="rounded-xl border border-border bg-bg-surface">
        {loading ? (
          <div className="flex justify-center p-12">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
          </div>
        ) : reports.length === 0 ? (
          <div className="flex flex-col items-center justify-center p-12 text-center">
            <span className="mb-3 text-4xl">✅</span>
            <p className="text-sm text-text-secondary">{t("noReports")}</p>
          </div>
        ) : (
          <div className="divide-y divide-border">
            {reports.map((report) => (
              <div key={report.id} className="flex items-center justify-between px-6 py-4">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className={`inline-flex rounded-md px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[report.status] || ""}`}>
                      {STATUS_KEYS[report.status] ? t(STATUS_KEYS[report.status]) : report.status}
                    </span>
                    <span className="text-xs text-text-muted">
                      {TARGET_TYPE_KEYS[report.target_type] ? t(TARGET_TYPE_KEYS[report.target_type]) : report.target_type}
                    </span>
                    <span className="text-xs text-text-muted">
                      {REASON_KEYS[report.reason] ? t(REASON_KEYS[report.reason]) : report.reason}
                    </span>
                    {report.target_author && (
                      <span className="text-xs text-text-muted">
                        — {report.target_author}
                      </span>
                    )}
                  </div>

                  {/* 신고 대상 콘텐츠 미리보기 */}
                  {report.target_deleted ? (
                    <p className="mt-1 text-xs italic text-text-muted">{t("reportTargetDeleted")}</p>
                  ) : (report.target_title || report.target_preview) ? (
                    <div className="mt-1 rounded-md border border-border/50 bg-bg-primary/50 px-3 py-2">
                      {report.target_title && (
                        <p className="text-sm font-medium text-text-primary truncate">{report.target_title}</p>
                      )}
                      {report.target_preview && (
                        <p className="text-xs text-text-secondary truncate">{report.target_preview}</p>
                      )}
                    </div>
                  ) : null}

                  {report.detail && (
                    <p className="mt-1 truncate text-sm text-text-secondary">
                      <span className="text-xs text-text-muted">{t("reportDetail")}:</span> {report.detail}
                    </p>
                  )}
                  <p className="mt-1 text-xs text-text-muted">
                    {new Date(report.created_at).toLocaleString(dateLocale)}
                  </p>
                </div>

                {report.status === "pending" && (
                  <div className="ml-4 flex shrink-0 gap-2">
                    <button
                      onClick={() => void handleReview(report)}
                      disabled={processing === report.id}
                      className="rounded-lg bg-accent-success/10 px-3 py-1.5 text-xs font-medium text-accent-success hover:bg-accent-success/20 disabled:opacity-50"
                    >
                      {t("reportReviewed")}
                    </button>
                    <button
                      onClick={() => handleStatusChange(report.id, "dismissed")}
                      disabled={processing === report.id}
                      className="rounded-lg bg-text-muted/10 px-3 py-1.5 text-xs font-medium text-text-muted hover:bg-text-muted/20 disabled:opacity-50"
                    >
                      {t("reportDismissed")}
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 페이지네이션 */}
      {total > 20 && (
        <div className="flex justify-center gap-2">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="rounded-lg bg-bg-surface px-3 py-1.5 text-sm disabled:opacity-50"
          >
            {t("prev")}
          </button>
          <span className="px-3 py-1.5 text-sm text-text-muted">
            {page} / {Math.ceil(total / 20)}
          </span>
          <button
            onClick={() => setPage((p) => p + 1)}
            disabled={page >= Math.ceil(total / 20)}
            className="rounded-lg bg-bg-surface px-3 py-1.5 text-sm disabled:opacity-50"
          >
            {t("next")}
          </button>
        </div>
      )}

      <ModerationActionDialog
        open={moderationTarget !== null}
        target={moderationDialogTarget}
        action="hide"
        labels={{
          title: t("moderationHideTitle"),
          restoreTitle: t("moderationRestoreTitle"),
          resourceLabel: t("moderationResourceLabel"),
          impactTitle: t("moderationImpactTitle"),
          impactLoading: t("moderationImpactLoading"),
          impactEmpty: t("moderationImpactEmpty"),
          reasonLabel: t("moderationReasonLabel"),
          reasonPlaceholder: t("moderationReasonPlaceholder"),
          targetNameLabel: t("moderationTargetNameLabel"),
          targetNameHelp: t("moderationTargetNameHelp"),
          next: t("deletionNext"),
          cancel: tc("cancel"),
          execute: t("moderationHideExecute"),
          restore: t("moderationRestoreExecute"),
          executing: t("moderationExecuting"),
          stageLabel: t("deletionStage"),
          scopeLabel: t("moderationScopeLabel"),
          deleted: t("reportTargetDeleted"),
        }}
        impactLabels={{ comments: t("moderationImpactComments") }}
        onClose={() => setModerationTarget(null)}
        onCompleted={handleModerationCompleted}
      />
    </div>
  );
}
