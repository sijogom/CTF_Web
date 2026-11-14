"use client";

import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import { useTranslations, useLocale } from "next-intl";
import { useSearchParams } from "next/navigation";
import { X } from "lucide-react";
import { apiFetch, ApiError } from "@/lib/api-client";
import { ListPagination } from "@/components/ui/ListPagination";
import { readPositiveInt, writeUrlQuery } from "@/lib/url-query";
import { parseListPage } from "@/lib/list-response.mjs";
import { auditInstantFromInputParts, auditTimeInputParts, auditTimeRange, type AuditTimeInputParts } from "@/lib/admin-ledger-state";

interface AuditLog {
  id: string;
  created_at: string;
  actor: string;
  action: string;
  target: string;
  details: Record<string, unknown> | null;
  ip_address: string;
}

interface AuditLogResponse {
  total: number;
  page: number;
  size: number;
  items: AuditLog[];
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

const ACTION_STYLES: Record<string, string> = {
  "auth.login": "bg-accent-success/10 text-accent-success",
  "auth.login_fail": "bg-accent-error/10 text-accent-error",
  "auth.logout": "bg-text-muted/10 text-text-muted",
  "user.delete": "bg-accent-error/10 text-accent-error",
  "admin.challenge_delete": "bg-accent-error/10 text-accent-error",
  "ban.create": "bg-accent-error/10 text-accent-error",
  "ban.lift": "bg-accent-success/10 text-accent-success",
  "ban.appeal_submit": "bg-yellow-500/10 text-yellow-500",
  "ban.appeal_review": "bg-accent-primary/10 text-accent-primary",
  "auth.login_banned": "bg-accent-error/10 text-accent-error",
};

const PAGE_SIZE = 50;
type TimeFilter = "created_from" | "created_to";

export default function AuditLogPage() {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const locale = useLocale();
  const searchParams = useSearchParams();
  const localeMap: Record<string, string> = { ko: "ko-KR", en: "en-US", ja: "ja-JP", zh: "zh-CN" };
  const dateLocale = localeMap[locale] || "ko-KR";
  const [actionFilter, setActionFilter] = useState(() => searchParams.get("action") ?? "");
  const [search, setSearch] = useState(() => searchParams.get("q") ?? "");
  const [debouncedSearch, setDebouncedSearch] = useState(() => searchParams.get("q") ?? "");
  const [page, setPage] = useState(() => readPositiveInt(searchParams.get("page")));
  const [createdFrom, setCreatedFrom] = useState(() => searchParams.get("created_from") ?? "");
  const [createdTo, setCreatedTo] = useState(() => searchParams.get("created_to") ?? "");
  const [timeInputs, setTimeInputs] = useState<Record<TimeFilter, AuditTimeInputParts>>(() => ({
    created_from: auditTimeInputParts(searchParams.get("created_from") ?? ""),
    created_to: auditTimeInputParts(searchParams.get("created_to") ?? ""),
  }));
  const timeInputsRef = useRef(timeInputs);
  timeInputsRef.current = timeInputs;
  const timeRange = useMemo(() => {
    const range = auditTimeRange(createdFrom, createdTo);
    if (auditInstantFromInputParts(timeInputs.created_from) === null) return { ...range, error: "invalid_from" as const };
    if (auditInstantFromInputParts(timeInputs.created_to) === null) return { ...range, error: "invalid_to" as const };
    return range;
  }, [createdFrom, createdTo, timeInputs]);
  const [total, setTotal] = useState(0);
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);
  const fetchScopeRef = useRef({ actionFilter, debouncedSearch, page, createdFrom, createdTo });
  fetchScopeRef.current = { actionFilter, debouncedSearch, page, createdFrom, createdTo };

  const syncFromUrl = useCallback(() => {
    const params = new URLSearchParams(window.location.search);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const nextSearch = params.get("q") ?? "";
    const nextAction = params.get("action") ?? "", nextPage = readPositiveInt(params.get("page")),
      nextFrom = params.get("created_from") ?? "", nextTo = params.get("created_to") ?? "", current = fetchScopeRef.current;
    if (current.actionFilter !== nextAction || current.debouncedSearch !== nextSearch || current.page !== nextPage
      || current.createdFrom !== nextFrom || current.createdTo !== nextTo) {
      controllerRef.current?.abort();
      requestIdRef.current++;
    }
    setActionFilter(nextAction);
    setSearch(nextSearch);
    setDebouncedSearch(nextSearch);
    setPage(nextPage);
    setCreatedFrom(nextFrom);
    setCreatedTo(nextTo);
    const nextInputs = { created_from: auditTimeInputParts(nextFrom), created_to: auditTimeInputParts(nextTo) };
    const oldInputs = timeInputsRef.current;
    if (oldInputs.created_from.dateTime !== nextInputs.created_from.dateTime || oldInputs.created_from.fraction !== nextInputs.created_from.fraction
      || oldInputs.created_to.dateTime !== nextInputs.created_to.dateTime || oldInputs.created_to.fraction !== nextInputs.created_to.fraction) {
      timeInputsRef.current = nextInputs;
      setTimeInputs(nextInputs);
    }
  }, []);

  useEffect(() => {
    window.addEventListener("popstate", syncFromUrl);
    return () => {
      window.removeEventListener("popstate", syncFromUrl);
      if (debounceRef.current) clearTimeout(debounceRef.current);
      controllerRef.current?.abort();
    };
  }, [syncFromUrl]);

  const fetchLogs = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const requestId = ++requestIdRef.current;
    if (timeRange.error) {
      setLogs([]); setTotal(0); setError(null); setLoading(false); return;
    }
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ page: String(page), size: String(PAGE_SIZE) });
      if (actionFilter.endsWith(".")) params.set("action_prefix", actionFilter);
      else if (actionFilter) params.set("action", actionFilter);
      if (debouncedSearch) params.set("search", debouncedSearch);
      if (timeRange.from) params.set("created_from", timeRange.from);
      if (timeRange.to) params.set("created_to", timeRange.to);
      const rawResponse = await apiFetch<unknown>(
        `/api/auth/admin/audit-logs?${params.toString()}`,
        { signal: controller.signal },
      );
      if (controller.signal.aborted || requestId !== requestIdRef.current) return;
      const response = parseListPage<AuditLog>(rawResponse, page, PAGE_SIZE) as AuditLogResponse;
      const responseTotalPages = Math.max(1, Math.ceil(response.total / PAGE_SIZE));
      if (page > responseTotalPages) {
        setPage(responseTotalPages);
        writeUrlQuery({ page: responseTotalPages === 1 ? null : responseTotalPages });
        return;
      }
      setLogs(response.items);
      setTotal(response.total);
    } catch (requestError) {
      if (controller.signal.aborted || requestId !== requestIdRef.current) return;
      setError(requestError instanceof ApiError ? requestError.message : t("loadFailed"));
    } finally {
      if (!controller.signal.aborted && requestId === requestIdRef.current) setLoading(false);
    }
  }, [actionFilter, debouncedSearch, page, t, timeRange]);

  useEffect(() => {
    void fetchLogs();
  }, [fetchLogs]);

  const changeActionFilter = (value: string) => {
    controllerRef.current?.abort(); requestIdRef.current++;
    setActionFilter(value);
    setPage(1);
    writeUrlQuery({ action: value || null, page: null }, "push");
  };

  const handleSearchChange = (value: string) => {
    setSearch(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setDebouncedSearch(value.trim());
      setPage(1);
      writeUrlQuery({ q: value.trim() || null, page: null });
    }, 300);
  };

  const changePage = (value: number) => {
    controllerRef.current?.abort(); requestIdRef.current++;
    setPage(value);
    writeUrlQuery({ page: value === 1 ? null : value }, "push");
  };

  const changeTime = (key: TimeFilter, input: AuditTimeInputParts) => {
    const next = auditInstantFromInputParts(input) ?? "", previous = timeInputsRef.current[key];
    if (previous.dateTime === input.dateTime && previous.fraction === input.fraction
      && (key === "created_from" ? createdFrom : createdTo) === next) return;
    controllerRef.current?.abort(); requestIdRef.current++;
    const inputs = { ...timeInputsRef.current, [key]: input };
    timeInputsRef.current = inputs;
    setTimeInputs(inputs);
    if (key === "created_from") setCreatedFrom(next); else setCreatedTo(next);
    setPage(1);
    writeUrlQuery({ [key]: next || null, page: null }, "push");
  };

  const changeTimePart = (key: TimeFilter, part: keyof AuditTimeInputParts, value: string) => {
    if (part === "fraction" && !/^[0-9]{0,6}$/.test(value)) return;
    changeTime(key, { ...timeInputsRef.current[key], [part]: value });
  };

  const actionLabels = useMemo<Record<string, string>>(() => {
    const labels: Record<string, string> = {};
    for (const [action, key] of Object.entries(ACTION_LABEL_KEYS)) {
      labels[action] = t(key);
    }
    return labels;
  }, [t]);

  const getActionStyle = (action: string) => {
    for (const [key, style] of Object.entries(ACTION_STYLES)) {
      if (action === key) return style;
    }
    return "bg-accent-primary/10 text-accent-primary";
  };

  const displayedError = timeRange.error ? t(`auditTimeError_${timeRange.error}`) : error;

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <h1 className="text-2xl font-bold">{t("auditLog")}</h1>

      {/* 필터 */}
      <div className="flex flex-wrap items-end gap-3">
        <select
          value={actionFilter}
          onChange={(e) => changeActionFilter(e.target.value)}
          aria-label={t("allActions")}
          className="rounded-lg border border-border bg-bg-surface px-3 py-2 text-sm"
        >
          <option value="">{t("allActions")}</option>
          <option value="auth.login">{t("actionLogin")}</option>
          <option value="auth.logout">{t("actionLogout")}</option>
          <option value="user.">{t("auditFilterUser")}</option>
          <option value="settings.">{t("auditFilterSettings")}</option>
          <option value="email.">{t("auditFilterEmail")}</option>
        </select>
        <input
          value={search}
          onChange={(event) => handleSearchChange(event.target.value)}
          aria-label={tc("search")}
          placeholder={`${tc("search")}...`}
          className="w-full rounded-lg border border-border bg-bg-surface px-3 py-2 text-sm sm:max-w-xs"
        />
        {(["created_from", "created_to"] as const).map((key) => {
          const from = key === "created_from", input = timeInputs[key], id = `audit-created-${from ? "from" : "to"}`;
          const invalid = timeRange.error === (from ? "invalid_from" : "invalid_to") || timeRange.error === "reversed";
          const hasValue = Boolean((from ? createdFrom : createdTo) || input.dateTime || input.fraction);
          return <div key={key} className="w-full min-w-0 text-sm sm:max-w-lg">
            <div className="flex min-w-0 flex-wrap items-end gap-2">
              <div className="min-w-0 flex-[1_1_14rem]">
                <label htmlFor={id} className="mb-1 block">{t(from ? "auditCreatedFrom" : "auditCreatedTo")}</label>
                <input id={id} type="datetime-local" step="1" value={input.dateTime}
                  onChange={(event) => changeTimePart(key, "dateTime", event.target.value)}
                  aria-invalid={invalid} aria-describedby={timeRange.error ? "audit-time-error" : undefined}
                  className="min-h-11 w-full min-w-0 rounded-lg border border-border bg-bg-surface px-3 py-2 text-sm" />
              </div>
              <div className="w-24 shrink-0">
                <label htmlFor={`${id}-fraction`} className="mb-1 block break-words text-xs">{t(from ? "auditCreatedFromFraction" : "auditCreatedToFraction")}</label>
                <input id={`${id}-fraction`} type="text" inputMode="numeric" maxLength={6} pattern="[0-9]{0,6}" value={input.fraction}
                  onChange={(event) => changeTimePart(key, "fraction", event.target.value)}
                  aria-invalid={invalid} aria-describedby={timeRange.error ? "audit-time-error" : undefined}
                  className="min-h-11 w-full rounded-lg border border-border bg-bg-surface px-3 py-2 font-mono text-sm" />
              </div>
              {hasValue && <button type="button" onClick={() => changeTime(key, { dateTime: "", fraction: "" })} title={t(from ? "auditClearFrom" : "auditClearTo")} aria-label={t(from ? "auditClearFrom" : "auditClearTo")} className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg border border-border hover:bg-bg-elevated"><X size={18} aria-hidden="true" /></button>}
            </div>
          </div>;
        })}
      </div>

      {displayedError && <div id="audit-time-error" role="alert" className="space-y-2 break-words text-sm text-accent-error">
        <p>{displayedError}</p>
        {!timeRange.error && <button type="button" onClick={() => void fetchLogs()} className="min-h-11 text-accent-primary underline">{tc("retry")}</button>}
      </div>}
      {loading && !timeRange.error && <div className="flex justify-center py-20" role="status" aria-label={tc("loading")}>
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>}

      {/* 테이블 */}
      {!loading && !displayedError && <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full text-sm">
          <thead className="bg-bg-surface">
            <tr>
              <th className="px-4 py-3 text-left font-medium text-text-muted">{t("colTime")}</th>
              <th className="px-4 py-3 text-left font-medium text-text-muted">{t("colAdmin")}</th>
              <th className="px-4 py-3 text-left font-medium text-text-muted">{t("colAction")}</th>
              <th className="px-4 py-3 text-left font-medium text-text-muted">{t("colTarget")}</th>
              <th className="px-4 py-3 text-left font-medium text-text-muted">{t("colIP")}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {logs.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-8 text-center text-text-muted">{t("noLogs")}</td>
              </tr>
            )}
            {logs.map((log) => (
              <tr key={log.id} className="hover:bg-bg-surface/50">
                <td className="px-4 py-3 font-mono text-xs text-text-muted whitespace-nowrap">
                  {new Date(log.created_at).toLocaleString(dateLocale)}
                </td>
                <td className="px-4 py-3">{log.actor ?? "-"}</td>
                <td className="px-4 py-3">
                  <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${getActionStyle(log.action)}`}>
                    {actionLabels[log.action] || log.action}
                  </span>
                </td>
                <td className="px-4 py-3 text-sm text-text-secondary">
                  {log.target || <span className="text-text-muted">-</span>}
                </td>
                <td className="px-4 py-3 font-mono text-xs text-text-muted">
                  {log.ip_address}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>}
      {!loading && !displayedError && <ListPagination
        page={page}
        pageSize={PAGE_SIZE}
        total={total}
        onPageChange={changePage}
      />}
    </div>
  );
}
