"use client";

import { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";

interface ContainerInstance {
  id: string;
  user_id: string;
  challenge_id: string;
  container_id: string;
  port: string;
  status: string;
  expires_at: string;
  created_at: string;
  extend_count?: string;
}

export default function AdminContainersPage() {
  const t = useTranslations("admin");
  const { toast } = useToast();
  const [instances, setInstances] = useState<ContainerInstance[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stopping, setStopping] = useState<string | null>(null);
  const [forceStopTarget, setForceStopTarget] = useState<string | null>(null);

  const fetchInstances = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<ContainerInstance[]>("/api/env/admin/instances");
      setInstances(data);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("loadStatsFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    fetchInstances();
    const interval = setInterval(fetchInstances, 15000);
    return () => clearInterval(interval);
  }, [fetchInstances]);

  const handleForceStop = async () => {
    if (!forceStopTarget) return;
    const instanceId = forceStopTarget;
    setForceStopTarget(null);
    setStopping(instanceId);
    try {
      await apiFetch(`/api/env/admin/instances/${instanceId}/force-stop`, {
        method: "POST",
      });
      setInstances((prev) => prev.filter((i) => i.id !== instanceId));
      toast("success", t("containerStopped"));
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : t("statusFailed"));
    } finally {
      setStopping(null);
    }
  };

  const formatTime = (unixStr: string) => {
    const ts = Number(unixStr);
    if (!ts) return "-";
    return new Date(ts * 1000).toLocaleString();
  };

  const getTimeRemaining = (expiresAt: string) => {
    const ts = Number(expiresAt);
    if (!ts) return "-";
    const diff = ts * 1000 - Date.now();
    if (diff <= 0) return t("expired");
    const min = Math.floor(diff / 60000);
    const sec = Math.floor((diff % 60000) / 1000);
    return `${min}:${String(sec).padStart(2, "0")}`;
  };

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t("containers")}</h1>
          <p className="mt-1 text-sm text-text-secondary">{t("containersDesc")}</p>
        </div>
        <Button variant="secondary" onClick={fetchInstances}>
          {t("refresh")}
        </Button>
      </div>

      {error && (
        <div className="rounded-lg border border-accent-error/30 bg-accent-error/10 p-4 text-sm text-accent-error">
          {error}
        </div>
      )}

      {instances.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border p-12 text-center">
          <span className="mb-3 text-4xl">{"\u{1F4E6}"}</span>
          <p className="text-sm text-text-secondary">{t("noContainers")}</p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-bg-surface">
              <tr>
                <th className="px-4 py-3 text-left font-medium">{t("colId")}</th>
                <th className="px-4 py-3 text-left font-medium">{t("colUser")}</th>
                <th className="px-4 py-3 text-left font-medium">{t("colChallenge")}</th>
                <th className="px-4 py-3 text-right font-medium">{t("colPort")}</th>
                <th className="px-4 py-3 text-center font-medium">{t("colStatus")}</th>
                <th className="px-4 py-3 text-center font-medium">{t("colExpires")}</th>
                <th className="px-4 py-3 text-center font-medium">{t("colActions")}</th>
              </tr>
            </thead>
            <tbody>
              {instances.map((inst) => (
                <tr key={inst.id} className="border-b border-border last:border-0 hover:bg-bg-surface/50">
                  <td className="px-4 py-3 font-mono text-xs">{inst.id}</td>
                  <td className="px-4 py-3 font-mono text-xs">{inst.user_id}</td>
                  <td className="px-4 py-3 text-center">{inst.challenge_id}</td>
                  <td className="px-4 py-3 text-right font-mono">{inst.port}</td>
                  <td className="px-4 py-3 text-center">
                    <span className="inline-flex items-center gap-1.5 rounded-full bg-accent-success/10 px-2 py-0.5 text-xs font-medium text-accent-success">
                      <span className="inline-block h-1.5 w-1.5 rounded-full bg-accent-success animate-pulse" />
                      {inst.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-center">
                    <span className="font-mono text-xs">{getTimeRemaining(inst.expires_at)}</span>
                  </td>
                  <td className="px-4 py-3 text-center">
                    <button
                      onClick={() => setForceStopTarget(inst.id)}
                      disabled={stopping === inst.id}
                      className="rounded px-3 py-1 text-xs font-medium text-accent-error hover:bg-accent-error/10 disabled:opacity-50"
                    >
                      {stopping === inst.id ? "..." : t("forceStop")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-xs text-text-muted">
        {instances.length} {t("containers").toLowerCase()} | {t("autoRefreshLabel")}
      </p>

      <ConfirmModal
        open={!!forceStopTarget}
        title={t("forceStop")}
        description={t("forceStopConfirm")}
        confirmLabel={t("forceStop")}
        variant="danger"
        onConfirm={handleForceStop}
        onCancel={() => setForceStopTarget(null)}
      />
    </div>
  );
}
