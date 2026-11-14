"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { StatCard } from "@/components/admin/StatCard";
import { apiFetch, ApiError } from "@/lib/api-client";

interface DashboardStats {
  total_users: number;
  active_users: number;
  total_challenges: number;
  active_contests: number;
  active_containers: number;
}

interface AuditLogItem {
  id: string;
  actor: string;
  action: string;
  target: string;
  details: Record<string, unknown> | null;
  ip_address: string | null;
  created_at: string;
}

interface AuditLogResponse {
  total: number;
  page: number;
  size: number;
  items: AuditLogItem[];
}

const ACTION_LABEL_KEYS: Record<string, string> = {
  "auth.login": "auditActionLogin",
  "auth.login_fail": "auditActionLoginFail",
  "auth.logout": "auditActionLogout",
  "user.role_change": "auditActionRoleChange",
  "user.toggle_active": "auditActionToggleActive",
  "user.delete": "auditActionUserDelete",
  "user.resend_verification": "auditActionResendVerify",
  "user.manual_verify": "auditActionManualVerify",
  "settings.update": "auditActionSettingsUpdate",
  "email.test": "auditActionEmailTest",
  "admin.user_role_change": "auditActionRoleChange",
  "admin.user_deactivate": "auditActionToggleActive",
  "admin.challenge_delete": "auditActionChallengeDelete",
  "ban.create": "auditActionBanCreate",
  "ban.lift": "auditActionBanLift",
  "ban.appeal_submit": "auditActionBanAppealSubmit",
  "ban.appeal_review": "auditActionBanAppealReview",
  "auth.login_banned": "auditActionLoginBanned",
};

const ACTION_ICONS: Record<string, string> = {
  "ban.create": "🛡️",
  "ban.lift": "✅",
  "ban.appeal_submit": "📝",
  "ban.appeal_review": "🔎",
  "auth.login_banned": "⛔",
  "auth.login_fail": "🚫",
  "auth.login": "🔑",
  "auth.logout": "👋",
  "user.role_change": "🛡️",
  "user.toggle_active": "🔄",
  "user.delete": "🗑️",
  "user.resend_verification": "📧",
  "user.manual_verify": "✅",
  "settings.update": "⚙️",
  "email.test": "📨",
  "admin.challenge_delete": "🗑️",
};

function getActionIcon(action: string): string {
  if (action.startsWith("auth.login_fail")) return ACTION_ICONS["auth.login_fail"];
  if (action.startsWith("auth.login")) return ACTION_ICONS["auth.login"];
  return ACTION_ICONS[action] || "📋";
}

export default function AdminOverviewPage() {
  const t = useTranslations("admin");
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [logs, setLogs] = useState<AuditLogItem[]>([]);
  const [error, setError] = useState("");
  const [currentTime, setCurrentTime] = useState(0);

  function formatTimeAgo(dateStr: string): string {
    const diff = currentTime - new Date(dateStr).getTime();
    const minutes = Math.floor(diff / 60000);
    if (minutes < 1) return t("justNow");
    if (minutes < 60) return t("minutesAgo", { n: minutes });
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return t("hoursAgo", { n: hours });
    const days = Math.floor(hours / 24);
    return t("daysAgo", { n: days });
  }

  function getActionDescription(log: AuditLogItem): string {
    const labelKey = ACTION_LABEL_KEYS[log.action];
    const label = labelKey ? t(labelKey) : log.action;
    const details = log.details as Record<string, string> | null;
    const target = log.target;

    switch (log.action) {
      case "user.role_change":
      case "admin.user_role_change":
        return t("auditRoleChangeDesc", { user: target || details?.username || t("unknownUser"), old: details?.old_role ?? "", new_role: details?.new_role ?? "" });
      case "user.toggle_active":
      case "admin.user_deactivate":
        return t("auditToggleActiveDesc", { user: target || details?.username || t("unknownUser"), status: details?.is_active ? t("activated") : t("deactivated") });
      case "user.delete":
        return t("auditUserDeleteDesc", { user: target || details?.username || t("unknownUser") });
      case "auth.login":
        return t("auditLoginDesc", { email: details?.email || t("unknownUser") });
      case "auth.login_fail":
        return t("auditLoginFailDesc", { email: details?.email || t("unknownEmail"), reason: details?.reason === "wrong_password" ? t("wrongPassword") : t("accountNotFound") });
      case "settings.update":
        return t("auditSettingsUpdateDesc", { keys: Array.isArray(details?.changed_keys) ? details.changed_keys.join(", ") : "" });
      default:
        return target ? `${label} — ${target}` : label;
    }
  }

  useEffect(() => {
    async function fetchData() {
      try {
        const [statsData, logsData] = await Promise.all([
          apiFetch<DashboardStats>("/api/auth/admin/stats"),
          apiFetch<AuditLogResponse>("/api/auth/admin/audit-logs?size=10"),
        ]);
        setStats(statsData);
        setLogs(logsData.items);
        setCurrentTime(Date.now());
      } catch (err) {
        const msg = err instanceof ApiError ? err.message : t("loadStatsFailed");
        setError(msg);
      }
    }
    fetchData();
    const interval = setInterval(fetchData, 15_000);
    return () => clearInterval(interval);
  }, [t]);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{t("dashboard")}</h1>

      {error && (
        <div className="rounded-lg border border-accent-error/30 bg-accent-error/10 px-4 py-3 text-sm text-accent-error">
          {error}
        </div>
      )}

      {/* 핵심 통계 */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          icon="👥"
          label={t("totalUsers")}
          value={stats?.total_users ?? "-"}
          change={stats ? t("activeUsers", { count: stats.active_users }) : undefined}
        />
        <StatCard icon="🏴" label={t("totalChallenges")} value={stats?.total_challenges ?? "-"} />
        <StatCard icon="🏆" label={t("activeContests")} value={stats?.active_contests ?? "-"} />
        <StatCard icon="🐳" label={t("activeContainers")} value={stats?.active_containers ?? "-"} />
      </div>

      {/* 최근 활동 */}
      <div className="rounded-xl border border-border bg-bg-surface p-5">
        <h2 className="mb-4 text-lg font-semibold">{t("recentActivity")}</h2>
        {logs.length === 0 ? (
          <p className="text-sm text-text-muted">{t("recentActivityDesc")}</p>
        ) : (
          <div className="space-y-3">
            {logs.map((log) => (
              <div key={log.id} className="flex items-start gap-3 rounded-lg border border-border/50 bg-bg-base p-3">
                <span className="mt-0.5 text-lg" aria-hidden="true">
                  {getActionIcon(log.action)}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm">
                    <span className="font-medium text-accent-primary">{log.actor}</span>
                    {" · "}
                    {getActionDescription(log)}
                  </p>
                  <p className="mt-1 text-xs text-text-muted">
                    {formatTimeAgo(log.created_at)}
                    {log.ip_address && ` · ${log.ip_address}`}
                  </p>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
