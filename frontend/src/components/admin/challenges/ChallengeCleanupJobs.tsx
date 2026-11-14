"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { RotateCw } from "lucide-react";
import { useTranslations } from "next-intl";
import {
  apiFetch,
  ApiError,
  captureApiRequestOwner,
  isCurrentApiRequestOwner,
  type ApiRequestOwner,
} from "@/lib/api-client";
import { useAuth } from "@/providers/AuthProvider";
import { useToast } from "@/providers/ToastProvider";

interface CleanupJob {
  challenge_id: number;
  job_id: string;
  status: "pending" | "processing" | "failed" | "completed";
  attempt_count: number;
  last_error_code: string | null;
}

interface ChallengeCleanupJobsProps {
  refreshVersion: number;
}

export function ChallengeCleanupJobs(props: ChallengeCleanupJobsProps) {
  const { user, sessionStatus, isSuperAdmin } = useAuth();
  if (!user || sessionStatus !== "authenticated" || !isSuperAdmin) return null;
  return <OwnedChallengeCleanupJobs key={user.id} ownerId={user.id} {...props} />;
}

function OwnedChallengeCleanupJobs({
  ownerId,
  refreshVersion,
}: ChallengeCleanupJobsProps & { ownerId: string }) {
  const t = useTranslations("admin");
  const { toast } = useToast();
  const [jobs, setJobs] = useState<CleanupJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [retryingIds, setRetryingIds] = useState<string[]>([]);
  const mountedRef = useRef(false);
  const generationRef = useRef(0);
  const listSequenceRef = useRef(0);
  const listControllerRef = useRef<AbortController | null>(null);
  const retryControllersRef = useRef(new Set<AbortController>());
  const retryPendingRef = useRef(new Set<string>());
  const refreshVersionRef = useRef(refreshVersion);

  const fetchJobs = useCallback(async (replacePending = true) => {
    if (!mountedRef.current || (!replacePending && listControllerRef.current)) return;
    listControllerRef.current?.abort();
    const controller = new AbortController();
    listControllerRef.current = controller;
    const sequence = ++listSequenceRef.current;
    const generation = generationRef.current;
    let requestOwner: ApiRequestOwner | null = null;
    setLoading(true);
    try {
      const owner = captureApiRequestOwner(ownerId);
      requestOwner = owner;
      const current = () => mountedRef.current && !controller.signal.aborted
        && generation === generationRef.current && sequence === listSequenceRef.current
        && isCurrentApiRequestOwner(owner);
      try {
        const response = await apiFetch<{ items: CleanupJob[]; total: number }>(
          "/api/learn/challenge-cleanup-jobs",
          { signal: controller.signal, requestOwner: owner },
        );
        if (!current()) return;
        if (!Array.isArray(response.items)) throw new Error("Invalid cleanup jobs response");
        setJobs(response.items.filter((job) => job.status !== "completed"));
        setError(null);
      } catch {
        if (current()) setError(t("cleanupLoadFailed"));
      }
    } catch {
      if (mountedRef.current && !controller.signal.aborted
        && generation === generationRef.current && sequence === listSequenceRef.current) {
        setError(t("cleanupLoadFailed"));
      }
    } finally {
      if (listControllerRef.current === controller) {
        listControllerRef.current = null;
        if (mountedRef.current && generation === generationRef.current
          && (!requestOwner || isCurrentApiRequestOwner(requestOwner))) setLoading(false);
      }
    }
  }, [ownerId, t]);

  useEffect(() => {
    mountedRef.current = true;
    setRetryingIds([]);
    const retryControllers = retryControllersRef.current;
    const retryPending = retryPendingRef.current;
    void fetchJobs();
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
      listControllerRef.current?.abort();
      listControllerRef.current = null;
      for (const controller of retryControllers) controller.abort();
      retryControllers.clear();
      retryPending.clear();
    };
  }, [fetchJobs]);

  useEffect(() => {
    if (refreshVersion === refreshVersionRef.current) return;
    refreshVersionRef.current = refreshVersion;
    void fetchJobs();
  }, [fetchJobs, refreshVersion]);

  const hasActiveJobs = jobs.some((job) => job.status === "pending" || job.status === "processing");
  useEffect(() => {
    if (!hasActiveJobs) return;
    const timer = setInterval(() => { void fetchJobs(false); }, 5000);
    return () => clearInterval(timer);
  }, [fetchJobs, hasActiveJobs]);

  const retryJob = useCallback(async (job: CleanupJob) => {
    if (!mountedRef.current || job.status !== "failed" || retryPendingRef.current.has(job.job_id)) return;
    retryPendingRef.current.add(job.job_id);
    setRetryingIds([...retryPendingRef.current]);
    const controller = new AbortController();
    retryControllersRef.current.add(controller);
    const generation = generationRef.current;
    let requestOwner: ApiRequestOwner | null = null;
    try {
      const owner = captureApiRequestOwner(ownerId);
      requestOwner = owner;
      const current = () => mountedRef.current && !controller.signal.aborted
        && generation === generationRef.current && isCurrentApiRequestOwner(owner);
      try {
        const result = await apiFetch<CleanupJob>(
          `/api/learn/challenges/${job.challenge_id}/cleanup/retry?job_id=${encodeURIComponent(job.job_id)}`,
          { method: "POST", signal: controller.signal, requestOwner: owner },
        );
        if (!current()) return;
        if (!result || result.job_id !== job.job_id || result.challenge_id !== job.challenge_id
          || !["pending", "processing", "failed", "completed"].includes(result.status)) {
          throw new Error("Mismatched cleanup retry response");
        }
        setJobs((previous) => previous.flatMap((item) => item.job_id !== job.job_id
          ? [item] : result.status === "completed" ? [] : [result]));
        void fetchJobs();
      } catch (failure) {
        if (!current()) return;
        toast("error", failure instanceof ApiError ? failure.message : t("cleanupRetryFailed"));
        void fetchJobs();
      }
    } catch {
      if (mountedRef.current && !controller.signal.aborted && generation === generationRef.current) {
        toast("error", t("cleanupRetryFailed"));
      }
    } finally {
      retryControllersRef.current.delete(controller);
      if (mountedRef.current && generation === generationRef.current
        && (!requestOwner || isCurrentApiRequestOwner(requestOwner))) {
        retryPendingRef.current.delete(job.job_id);
        setRetryingIds([...retryPendingRef.current]);
      }
    }
  }, [fetchJobs, ownerId, t, toast]);

  if (jobs.length === 0 && !error) return null;

  return (
    <section className="min-w-0 border-t border-border pt-5" aria-label={t("cleanupTitle")}>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-base font-semibold">{t("cleanupTitle")}</h2>
        <button
          type="button"
          onClick={() => { void fetchJobs(); }}
          disabled={loading}
          title={t("cleanupRefresh")}
          aria-label={t("cleanupRefresh")}
          className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded text-text-secondary hover:bg-bg-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary disabled:opacity-50"
        >
          <RotateCw size={16} className={loading ? "animate-spin" : undefined} aria-hidden="true" />
        </button>
      </div>
      {error && <p role="alert" className="mb-3 text-sm text-status-error">{error}</p>}
      {jobs.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[20rem] text-sm">
            <thead className="border-b border-border text-text-secondary">
              <tr>
                <th scope="col" className="px-2 py-2 text-left font-medium">{t("cleanupChallenge")}</th>
                <th scope="col" className="px-2 py-2 text-left font-medium">{t("colStatus")}</th>
                <th scope="col" className="px-2 py-2 text-right font-medium">{t("cleanupAttempts")}</th>
                <th scope="col" className="px-2 py-2 text-right font-medium">{t("colActions")}</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.job_id} className="border-b border-border last:border-0">
                  <td className="px-2 py-3 font-mono">#{job.challenge_id}</td>
                  <td className={`whitespace-nowrap px-2 py-3 ${job.status === "failed" ? "text-status-error" : "text-text-secondary"}`}>
                    {t(`cleanupStatus_${job.status}`)}
                  </td>
                  <td className="whitespace-nowrap px-2 py-3 text-right font-mono">{job.attempt_count}</td>
                  <td className="px-2 py-3 text-right">
                    {job.status === "failed" && (
                      <button
                        type="button"
                        onClick={() => { void retryJob(job); }}
                        disabled={retryingIds.includes(job.job_id)}
                        title={t("cleanupRetry", { id: job.challenge_id })}
                        aria-label={t("cleanupRetry", { id: job.challenge_id })}
                        className="inline-flex h-8 w-8 items-center justify-center rounded text-accent-primary hover:bg-accent-primary/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary disabled:opacity-50"
                      >
                        <RotateCw size={16} className={retryingIds.includes(job.job_id) ? "animate-spin" : undefined} aria-hidden="true" />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
