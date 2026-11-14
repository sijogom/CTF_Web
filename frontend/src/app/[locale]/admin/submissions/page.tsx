"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import { useAuth } from "@/providers/AuthProvider";
import { isCurrentFlagReveal, normalizeRevealReason, parseFlagRevealResponse, type FlagRevealScope } from "@/lib/admin-ledger-state";

interface SubmissionLog {
  id: string;
  user_id: string;
  username: string;
  challenge_id: number;
  challenge_name: string;
  challenge_type: "wargame" | "competition";
  submitted_flag_recorded: boolean;
  submitted_flag_length: number;
  submitted_flag_masked: string | null;
  submitted_flag_revealable: boolean;
  is_correct: boolean;
  competition_id: string | null;
  submission_context: "ctf" | "wargame";
  submitted_at: string | null;
}

interface SubmissionLogsResponse {
  items: SubmissionLog[];
  total: number;
  page: number;
  size: number;
}

interface ChallengeSolveMetric {
  challenge_id: number;
  challenge_name: string;
  challenge_type: "wargame" | "competition";
  is_published: boolean;
  attempting_user_count: number;
  solver_count: number;
  solve_rate: number;
  latest_solved_at: string | null;
}

interface ChallengeSolveMetricsResponse {
  items: ChallengeSolveMetric[];
  total: number;
  page: number;
  size: number;
  submission_context: "all" | "wargame" | "ctf";
}

type ResultFilter = "all" | "correct" | "incorrect";
type ContextFilter = "all" | "wargame" | "ctf";
type SubmissionSort =
  | "submitted_at_desc"
  | "submitted_at_asc"
  | "challenge_name_asc"
  | "result_desc";

const PAGE_SIZE = 20;
const METRIC_PAGE_SIZE = 10;

function addDateParam(params: URLSearchParams, key: string, value: string) {
  if (!value) return;

  const date = new Date(value);
  if (!Number.isNaN(date.getTime())) {
    params.set(key, date.toISOString());
  }
}

function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

export default function AdminSubmissionsPage() {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const { toast } = useToast();
  const { user, sessionStatus } = useAuth();
  const ownerId = sessionStatus === "authenticated" && user?.role === "admin" ? user.id : null;
  const ownerRef = useRef(ownerId);
  ownerRef.current = ownerId;

  const [logs, setLogs] = useState<SubmissionLog[]>([]);
  const [metrics, setMetrics] = useState<ChallengeSolveMetric[]>([]);
  const [total, setTotal] = useState(0);
  const [metricTotal, setMetricTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [metricPage, setMetricPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [logError, setLogError] = useState("");
  const [metricError, setMetricError] = useState("");
  const [revealedFlags, setRevealedFlags] = useState<Record<string, string>>({});
  const flagsOwnerRef = useRef<string | null>(null);
  const [revealTarget, setRevealTarget] = useState<{ log: SubmissionLog; scope: FlagRevealScope } | null>(null);
  const revealTargetRef = useRef<typeof revealTarget>(null);
  const [revealReason, setRevealReason] = useState("");
  const [revealError, setRevealError] = useState("");
  const [revealPending, setRevealPending] = useState(false);
  const revealPendingRef = useRef(false);
  const revealSequenceRef = useRef(0);
  const revealControllerRef = useRef<AbortController | null>(null);
  const revealDialogRef = useRef<HTMLDialogElement>(null);
  const reasonInputRef = useRef<HTMLTextAreaElement>(null);

  const [resultFilter, setResultFilter] = useState<ResultFilter>("all");
  const [contextFilter, setContextFilter] = useState<ContextFilter>("all");
  const [usernameSearch, setUsernameSearch] = useState("");
  const [challengeSearch, setChallengeSearch] = useState("");
  const [competitionSearch, setCompetitionSearch] = useState("");
  const [submittedFrom, setSubmittedFrom] = useState("");
  const [submittedTo, setSubmittedTo] = useState("");
  const [sort, setSort] = useState<SubmissionSort>("submitted_at_desc");
  const requestGenerationRef = useRef(0);
  const requestControllerRef = useRef<AbortController | null>(null);

  const resetRevealDialog = useCallback(() => {
    revealControllerRef.current?.abort();
    revealControllerRef.current = null;
    revealTargetRef.current = null;
    revealPendingRef.current = false;
    setRevealTarget(null);
    setRevealReason("");
    setRevealError("");
    setRevealPending(false);
  }, []);
  const clearRevealScope = useCallback(() => {
    resetRevealDialog();
    flagsOwnerRef.current = null;
    setRevealedFlags({});
  }, [resetRevealDialog]);
  const dialogActive = revealTarget !== null && revealTarget.scope.ownerId === ownerId
    && revealTarget.scope.generation === requestGenerationRef.current;
  useEffect(() => {
    const dialog = revealDialogRef.current;
    if (!dialog) return;
    if (dialogActive && !dialog.open) { dialog.showModal(); reasonInputRef.current?.focus(); }
    else if (!dialogActive && dialog.open) dialog.close();
  }, [dialogActive]);

  const fetchLogs = useCallback(async () => {
    requestControllerRef.current?.abort();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    const requestGeneration = requestGenerationRef.current + 1;
    requestGenerationRef.current = requestGeneration;

    setLoading(true);
    setLogError("");
    setMetricError("");
    setLogs([]);
    setMetrics([]);
    setTotal(0);
    setMetricTotal(0);
    clearRevealScope();

    const logParams = new URLSearchParams({
      page: String(page),
      size: String(PAGE_SIZE),
      submission_context: contextFilter,
      sort,
    });
    if (resultFilter === "correct") logParams.set("is_correct", "true");
    if (resultFilter === "incorrect") logParams.set("is_correct", "false");
    if (usernameSearch.trim()) logParams.set("username", usernameSearch.trim());
    if (challengeSearch.trim()) logParams.set("challenge_search", challengeSearch.trim());
    if (competitionSearch.trim()) logParams.set("competition_id", competitionSearch.trim());
    addDateParam(logParams, "submitted_from", submittedFrom);
    addDateParam(logParams, "submitted_to", submittedTo);

    const metricParams = new URLSearchParams({
      page: String(metricPage),
      size: String(METRIC_PAGE_SIZE),
      submission_context: contextFilter,
      sort: "latest_solved_at_desc",
    });
    if (challengeSearch.trim()) metricParams.set("challenge_search", challengeSearch.trim());
    if (competitionSearch.trim()) metricParams.set("competition_id", competitionSearch.trim());
    addDateParam(metricParams, "submitted_from", submittedFrom);
    addDateParam(metricParams, "submitted_to", submittedTo);

    const [logResult, metricResult] = await Promise.allSettled([
      apiFetch<SubmissionLogsResponse>(`/api/learn/submissions/logs?${logParams}`, {
        signal: controller.signal,
      }),
      apiFetch<ChallengeSolveMetricsResponse>(
        `/api/learn/admin/challenges/solve-metrics?${metricParams}`,
        { signal: controller.signal },
      ),
    ]);

    if (
      controller.signal.aborted
      || requestGenerationRef.current !== requestGeneration
    ) return;

    const failures: string[] = [];
    if (logResult.status === "fulfilled") {
      const logData = logResult.value;
      setLogs(logData.items);
      setTotal(logData.total);
      const lastLogPage = Math.max(1, Math.ceil(logData.total / PAGE_SIZE));
      if (page > lastLogPage) setPage(lastLogPage);
    } else {
      const message = logResult.reason instanceof ApiError ? logResult.reason.message : tc("error");
      setLogError(message);
      failures.push(message);
    }

    if (metricResult.status === "fulfilled") {
      const metricData = metricResult.value;
      setMetrics(metricData.items);
      setMetricTotal(metricData.total);
      const lastMetricPage = Math.max(1, Math.ceil(metricData.total / METRIC_PAGE_SIZE));
      if (metricPage > lastMetricPage) setMetricPage(lastMetricPage);
    } else {
      const message = metricResult.reason instanceof ApiError ? metricResult.reason.message : tc("error");
      setMetricError(message);
      failures.push(message);
    }

    if (failures.length > 0) {
      toast("error", [...new Set(failures)].join(" / "));
    }

    if (requestGenerationRef.current === requestGeneration) {
      setLoading(false);
    }
  }, [
    challengeSearch,
    clearRevealScope,
    competitionSearch,
    contextFilter,
    metricPage,
    page,
    resultFilter,
    sort,
    submittedFrom,
    submittedTo,
    tc,
    toast,
    usernameSearch,
  ]);

  useEffect(() => {
    void fetchLogs();
  }, [fetchLogs, ownerId]);

  useEffect(() => () => {
    requestGenerationRef.current++;
    requestControllerRef.current?.abort();
    revealControllerRef.current?.abort();
    revealTargetRef.current = null;
  }, []);

  const invalidateListScope = () => {
    requestGenerationRef.current++;
    requestControllerRef.current?.abort();
    clearRevealScope();
  };

  const updateFilter = (update: () => void) => {
    invalidateListScope();
    update();
    setPage(1);
    setMetricPage(1);
  };

  const openReveal = (log: SubmissionLog) => {
    if (!ownerId || revealPendingRef.current) return;
    const target = { log, scope: { ownerId, submissionId: log.id, generation: requestGenerationRef.current, requestId: ++revealSequenceRef.current } };
    revealTargetRef.current = target;
    setRevealTarget(target);
    setRevealReason("");
    setRevealError("");
  };
  const cancelReveal = () => { if (!revealPendingRef.current) resetRevealDialog(); };
  const revealFlag = async () => {
    const target = revealTargetRef.current;
    if (!target || revealPendingRef.current || !isCurrentFlagReveal(target.scope, target.scope, ownerRef.current, requestGenerationRef.current)) return;
    const normalized = normalizeRevealReason(revealReason);
    if (normalized.error) { setRevealError(t(`revealReasonError_${normalized.error}`)); reasonInputRef.current?.focus(); return; }
    const controller = new AbortController();
    revealControllerRef.current = controller;
    revealPendingRef.current = true;
    setRevealPending(true);
    setRevealError("");
    const current = () => !controller.signal.aborted && isCurrentFlagReveal(target.scope, revealTargetRef.current?.scope ?? null, ownerRef.current, requestGenerationRef.current);
    try {
      const payload = await apiFetch<unknown>(
        `/api/learn/submissions/logs/${target.scope.submissionId}/flag`,
        { method: "POST", body: { reason: normalized.reason }, signal: controller.signal },
      );
      if (current()) {
        const flag = parseFlagRevealResponse(payload, target.scope.submissionId);
        flagsOwnerRef.current = target.scope.ownerId;
        setRevealedFlags((values) => ({ ...values, [target.scope.submissionId]: flag }));
        resetRevealDialog();
      }
    } catch (err) {
      if (current()) {
        const message = err instanceof ApiError ? err.message : tc("error");
        setRevealError(message);
      }
    } finally {
      if (current()) { revealPendingRef.current = false; setRevealPending(false); revealControllerRef.current = null; }
    }
  };
  const visibleFlags: Record<string, string> = flagsOwnerRef.current === ownerId && ownerId !== null ? revealedFlags : {};

  const totalPages = Math.ceil(total / PAGE_SIZE);
  const metricTotalPages = Math.ceil(metricTotal / METRIC_PAGE_SIZE);
  const filterTabs: { key: ResultFilter; label: string }[] = [
    { key: "all", label: t("filterAll") },
    { key: "correct", label: t("filterCorrect") },
    { key: "incorrect", label: t("filterIncorrect") },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">{t("submissionLogs")}</h1>
          <p className="mt-1 text-sm text-text-muted">
            {t("submissionRevealPolicy")}
          </p>
        </div>
        <span className="text-sm text-text-muted">
          {t("submissionTotalCount", { count: total })}
        </span>
      </div>

      <section className="space-y-3 rounded-xl border border-border bg-bg-surface p-4">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex rounded-lg border border-border">
            {filterTabs.map((tab) => (
              <button
                key={tab.key}
                type="button"
                onClick={() => updateFilter(() => setResultFilter(tab.key))}
                className={`px-4 py-2 text-sm font-medium transition-colors first:rounded-l-lg last:rounded-r-lg ${
                  resultFilter === tab.key
                    ? "bg-accent-primary text-white"
                    : "bg-bg-primary text-text-secondary hover:text-text-primary"
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          <select
            value={contextFilter}
            onChange={(event) => updateFilter(() => setContextFilter(event.target.value as ContextFilter))}
            aria-label="제출 구분"
            className="rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm"
          >
            <option value="all">전체 구분</option>
            <option value="wargame">워게임</option>
            <option value="ctf">CTF 대회</option>
          </select>

          <select
            value={sort}
            onChange={(event) => updateFilter(() => setSort(event.target.value as SubmissionSort))}
            aria-label="정렬"
            className="rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm"
          >
            <option value="submitted_at_desc">최신 제출순</option>
            <option value="submitted_at_asc">오래된 제출순</option>
            <option value="challenge_name_asc">문제명순</option>
            <option value="result_desc">정답 우선</option>
          </select>
        </div>

        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <input
            value={usernameSearch}
            onChange={(event) => updateFilter(() => setUsernameSearch(event.target.value))}
            placeholder="사용자명 검색"
            aria-label="사용자명 검색"
            className="rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
          />
          <input
            value={challengeSearch}
            onChange={(event) => updateFilter(() => setChallengeSearch(event.target.value))}
            placeholder="문제명 검색"
            aria-label="문제명 검색"
            className="rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
          />
          <input
            value={competitionSearch}
            onChange={(event) => updateFilter(() => setCompetitionSearch(event.target.value))}
            placeholder="대회 ID (정확히 일치)"
            aria-label="대회 ID 정확히 일치"
            className="rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
          />
          <div className="grid grid-cols-2 gap-2">
            <input
              type="datetime-local"
              value={submittedFrom}
              onChange={(event) => updateFilter(() => setSubmittedFrom(event.target.value))}
              aria-label="제출 시작 시각"
              className="min-w-0 rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
            />
            <input
              type="datetime-local"
              value={submittedTo}
              onChange={(event) => updateFilter(() => setSubmittedTo(event.target.value))}
              aria-label="제출 종료 시각"
              className="min-w-0 rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
            />
          </div>
        </div>
      </section>

      {(logError || metricError) && (
        <div className="rounded-lg border border-accent-error/30 bg-accent-error/10 px-4 py-3 text-sm text-accent-error">
          {logError && <p>제출 감사 조회: {logError}</p>}
          {metricError && <p>문제 통계 조회: {metricError}</p>}
          <button type="button" onClick={() => void fetchLogs()} className="mt-1 underline">
            {t("retryAction")}
          </button>
        </div>
      )}

      {loading ? (
        <div className="flex justify-center py-20">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
        </div>
      ) : (
        <>
          <section className="overflow-x-auto rounded-xl border border-border">
            <div className="flex items-center justify-between border-b border-border bg-bg-surface px-4 py-3">
              <div>
                <h2 className="font-semibold">문제별 풀이 통계</h2>
                <p className="text-xs text-text-muted">
                  풀이율 = 해당 조건에서 정답을 맞힌 고유 사용자 ÷ 시도한 고유 사용자
                </p>
              </div>
              <span className="text-xs text-text-muted">총 {metricTotal}개 문제</span>
            </div>
            {metrics.length === 0 ? (
              <p className="px-4 py-8 text-center text-sm text-text-muted">표시할 문제 통계가 없습니다.</p>
            ) : (
              <table className="w-full min-w-[56rem] text-sm">
                <thead className="border-b border-border bg-bg-primary text-left text-text-secondary">
                  <tr>
                    <th className="px-4 py-3 font-medium">문제</th>
                    <th className="px-4 py-3 font-medium">구분</th>
                    <th className="px-4 py-3 font-medium">풀이자</th>
                    <th className="px-4 py-3 font-medium">풀이율</th>
                    <th className="px-4 py-3 font-medium">최근 풀이</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {metrics.map((metric) => (
                    <tr key={metric.challenge_id} className="bg-bg-surface text-text-primary">
                      <td className="px-4 py-3">
                        {metric.challenge_name}
                        {!metric.is_published && <span className="ml-2 text-xs text-text-muted">비공개</span>}
                      </td>
                      <td className="px-4 py-3">{metric.challenge_type === "competition" ? "CTF 문제" : "워게임"}</td>
                      <td className="px-4 py-3">{metric.solver_count} / {metric.attempting_user_count}</td>
                      <td className="px-4 py-3">{metric.solve_rate.toFixed(2)}%</td>
                      <td className="whitespace-nowrap px-4 py-3 text-xs text-text-muted">
                        {formatDate(metric.latest_solved_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {metricTotalPages > 1 && (
              <div className="flex items-center justify-center gap-2 border-t border-border bg-bg-surface px-4 py-3">
                <button
                  type="button"
                  onClick={() => { invalidateListScope(); setMetricPage((currentPage) => Math.max(1, currentPage - 1)); }}
                  disabled={metricPage === 1}
                  className="rounded-lg border border-border px-3 py-1.5 text-sm disabled:opacity-40"
                >
                  {t("prev")}
                </button>
                <span className="text-sm text-text-muted">{metricPage} / {metricTotalPages}</span>
                <button
                  type="button"
                  onClick={() => { invalidateListScope(); setMetricPage((currentPage) => Math.min(metricTotalPages, currentPage + 1)); }}
                  disabled={metricPage === metricTotalPages}
                  className="rounded-lg border border-border px-3 py-1.5 text-sm disabled:opacity-40"
                >
                  {t("next")}
                </button>
              </div>
            )}
          </section>

          {logs.length === 0 ? (
            <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border p-12 text-center">
              <h3 className="mb-1 text-lg font-semibold text-text-secondary">{t("submissionLogs")}</h3>
              <p className="text-sm text-text-muted">{tc("noData")}</p>
            </div>
          ) : (
            <div className="overflow-x-auto rounded-xl border border-border">
              <table className="w-full min-w-[56rem] text-sm">
                <thead className="border-b border-border bg-bg-surface text-left text-text-secondary">
                  <tr>
                    <th className="px-4 py-3 font-medium">{t("submissionUser")}</th>
                    <th className="px-4 py-3 font-medium">{t("submissionChallenge")}</th>
                    <th className="px-4 py-3 font-medium">구분</th>
                    <th className="px-4 py-3 font-medium">{t("submissionFlag")}</th>
                    <th className="px-4 py-3 font-medium">{t("submissionResult")}</th>
                    <th className="px-4 py-3 font-medium">{t("submissionTime")}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {logs.map((log) => (
                    <tr
                      key={log.id}
                      className="bg-bg-surface text-text-primary transition-colors hover:bg-bg-primary"
                    >
                      <td className="px-4 py-3 text-sm">{log.username}</td>
                      <td className="px-4 py-3">{log.challenge_name}</td>
                      <td className="px-4 py-3">
                        {log.submission_context === "ctf" ? "CTF" : "워게임"}
                        {log.submission_context === "wargame" && log.challenge_type === "competition" && (
                          <span className="ml-1 text-xs text-accent-warning">문제 구분 불일치</span>
                        )}
                      </td>
                      <td className="max-w-[240px] px-4 py-3 font-mono text-xs text-text-secondary">
                        {visibleFlags[log.id] !== undefined ? (
                          <div className="break-all text-accent-primary">{visibleFlags[log.id]}</div>
                        ) : (
                          <div className="space-y-1.5">
                            <div>{log.submitted_flag_masked ?? (log.submitted_flag_recorded ? "[redacted]" : t("submissionNoOriginal"))}</div>
                            {ownerId !== null && log.submitted_flag_revealable && (
                              <button
                                type="button"
                                onClick={() => openReveal(log)}
                                disabled={revealPending}
                                className="min-h-11 rounded border border-accent-primary px-2 py-1 text-xs text-accent-primary hover:bg-accent-primary hover:text-white disabled:opacity-50"
                              >
                                {t("submissionReveal")}
                              </button>
                            )}
                          </div>
                        )}
                        {log.submitted_flag_recorded && (
                          <div className="mt-1 text-[11px] text-text-muted">{t("submissionLength", { count: log.submitted_flag_length })}</div>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-semibold ${
                            log.is_correct
                              ? "bg-accent-success/10 text-accent-success"
                              : "bg-accent-error/10 text-accent-error"
                          }`}
                        >
                          {log.is_correct ? t("filterCorrect") : t("filterIncorrect")}
                        </span>
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-xs text-text-muted">
                        {formatDate(log.submitted_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2">
          <button
            type="button"
            onClick={() => { invalidateListScope(); setPage((currentPage) => Math.max(1, currentPage - 1)); }}
            disabled={page === 1}
            className="rounded-lg border border-border px-3 py-1.5 text-sm disabled:opacity-40"
          >
            {t("prev")}
          </button>
          <span className="text-sm text-text-muted">{page} / {totalPages}</span>
          <button
            type="button"
            onClick={() => { invalidateListScope(); setPage((currentPage) => Math.min(totalPages, currentPage + 1)); }}
            disabled={page === totalPages}
            className="rounded-lg border border-border px-3 py-1.5 text-sm disabled:opacity-40"
          >
            {t("next")}
          </button>
        </div>
      )}
      <dialog
        ref={revealDialogRef}
        aria-labelledby="submission-reveal-title"
        aria-describedby="submission-reveal-target"
        aria-busy={revealPending}
        onCancel={(event) => { event.preventDefault(); cancelReveal(); }}
        className="fixed inset-0 z-[100] m-auto max-h-[calc(100dvh-24px)] w-[calc(100vw-24px)] max-w-md overflow-y-auto rounded-lg border border-border bg-bg-surface p-4 text-text-primary shadow-2xl backdrop:bg-black/50 sm:p-6"
      >
        <form onSubmit={(event) => { event.preventDefault(); void revealFlag(); }} className="min-w-0 space-y-4">
          <h2 id="submission-reveal-title" className="break-words text-lg font-semibold">{t("submissionReveal")}</h2>
          <p id="submission-reveal-target" className="break-words text-sm text-text-secondary">
            {dialogActive && `${revealTarget.log.username} · ${revealTarget.log.challenge_name}`}
          </p>
          <label htmlFor="submission-reveal-reason" className="block text-sm font-medium">{t("revealReasonLabel")}</label>
          <textarea
            ref={reasonInputRef}
            id="submission-reveal-reason"
            value={dialogActive ? revealReason : ""}
            onChange={(event) => { setRevealReason(event.target.value); setRevealError(""); }}
            disabled={revealPending}
            required
            rows={4}
            aria-invalid={Boolean(revealError)}
            aria-describedby={revealError ? "submission-reveal-error" : undefined}
            className="block w-full min-w-0 resize-y rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none disabled:opacity-60"
          />
          {revealError && dialogActive && <p id="submission-reveal-error" role="alert" className="break-words text-sm text-accent-error">{revealError}</p>}
          <div className="flex flex-wrap justify-end gap-2">
            <button type="button" onClick={cancelReveal} disabled={revealPending} className="min-h-11 rounded-lg border border-border px-4 py-2 text-sm disabled:opacity-50">{tc("cancel")}</button>
            <button type="submit" disabled={revealPending || !dialogActive} className="min-h-11 rounded-lg bg-accent-primary px-4 py-2 text-sm text-white disabled:opacity-50">{revealPending ? t("submissionRevealing") : t("submissionRevealConfirm")}</button>
          </div>
        </form>
      </dialog>
    </div>
  );
}
