"use client";

import { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { apiFetch, ApiError } from "@/lib/api-client";
import type { SettingsSaveResponse, SmtpSettings, SmtpSettingsUpdate } from "@/types/admin";

type TargetType = "user" | "role" | "all";
type TabType = "settings" | "compose" | "history";
type SmtpLoadState = "idle" | "loading" | "ready" | "error";

import type { UserItem } from "@/types/user";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { isRichContentEmpty } from "@/lib/rich-content.mjs";

type EmailUserItem = Pick<UserItem, "id" | "username" | "email">;

interface UsersResponse {
  items: EmailUserItem[];
}

interface EmailLog {
  id: string;
  sender_id: string | null;
  recipients: string[];
  subject: string;
  target_type: string;
  total_count: number;
  success_count: number;
  fail_count: number;
  error_details: Record<string, string> | null;
  created_at: string;
}

interface EmailLogsResponse {
  items: EmailLog[];
  total: number;
}

export default function AdminEmailPage() {
  const t = useTranslations("admin");
  const te = useTranslations("adminEmail");

  // --- 탭 ---
  const [activeTab, setActiveTab] = useState<TabType>("settings");

  // --- 발송 폼 상태 ---
  const [target, setTarget] = useState<TargetType>("all");
  const [role, setRole] = useState("user");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");

  // 사용자 검색/선택
  const [userSearch, setUserSearch] = useState("");
  const [searchResults, setSearchResults] = useState<EmailUserItem[]>([]);
  const [selectedUsers, setSelectedUsers] = useState<EmailUserItem[]>([]);
  const [searching, setSearching] = useState(false);

  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<{ success: number; failed: number } | null>(null);
  const [error, setError] = useState("");
  const [showSendConfirm, setShowSendConfirm] = useState(false);
  const [logsLoading, setLogsLoading] = useState(false);

  // --- SMTP 설정 상태 ---
  const [smtpHost, setSmtpHost] = useState("");
  const [smtpPort, setSmtpPort] = useState(587);
  const [smtpFrom, setSmtpFrom] = useState("");
  const [smtpTls, setSmtpTls] = useState(true);
  const [smtpUser, setSmtpUser] = useState("");
  const [smtpPassword, setSmtpPassword] = useState("");
  const [smtpSaving, setSmtpSaving] = useState(false);
  const [smtpTesting, setSmtpTesting] = useState(false);
  const [smtpLoadState, setSmtpLoadState] = useState<SmtpLoadState>("idle");
  const [smtpMessage, setSmtpMessage] = useState<{ type: "success" | "error"; text: string } | null>(null);

  // --- 미리보기 상태 ---
  const [previewHtml, setPreviewHtml] = useState<string>("");

  // --- 이력 상태 ---
  const [logs, setLogs] = useState<EmailLog[]>([]);
  const [logPage, setLogPage] = useState(1);
  const [logTotal, setLogTotal] = useState(0);
  const LOG_PAGE_SIZE = 10;

  // --- 사용자 검색 ---
  const searchUsers = useCallback(async (q: string) => {
    if (!q.trim()) {
      setSearchResults([]);
      return;
    }
    setSearching(true);
    try {
      const params = new URLSearchParams({ search: q, size: "10" });
      const data = await apiFetch<UsersResponse>(`/api/auth/admin/users?${params}`);
      setSearchResults(data.items);
    } catch {
      setSearchResults([]);
    } finally {
      setSearching(false);
    }
  }, []);

  // 디바운스 검색
  useEffect(() => {
    if (target !== "user") return;
    const timer = setTimeout(() => searchUsers(userSearch), 300);
    return () => clearTimeout(timer);
  }, [userSearch, target, searchUsers]);

  // --- 디바운스 미리보기 (AbortController로 race condition 방지) ---
  useEffect(() => {
    if (!subject && !body) {
      setPreviewHtml("");
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const res = await apiFetch<{ html: string }>("/api/auth/admin/email/preview", {
          method: "POST",
          body: { target: "user", subject, body },
          signal: controller.signal,
        });
        setPreviewHtml(res.html);
      } catch {
        // 미리보기 실패 또는 abort 시 조용히 무시
      }
    }, 500);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [subject, body]);

  // --- 이력 조회 ---
  const fetchLogs = useCallback(async (page: number) => {
    setLogsLoading(true);
    try {
      const res = await apiFetch<EmailLogsResponse>(
        `/api/auth/admin/email/logs?page=${page}&size=${LOG_PAGE_SIZE}`
      );
      setLogs(res.items);
      setLogTotal(res.total);
      setLogPage(page);
    } catch {
      setLogs([]);
    } finally {
      setLogsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeTab === "history") fetchLogs(1);
  }, [activeTab, fetchLogs]);

  // --- SMTP 설정 로드 ---
  const loadSmtpSettings = useCallback(async () => {
    setSmtpLoadState("loading");
    setSmtpMessage(null);
    try {
      const data = await apiFetch<SmtpSettings>("/api/auth/admin/email/smtp-settings");
      setSmtpHost(data.smtp_host ?? "");
      setSmtpPort(data.smtp_port ?? 587);
      setSmtpFrom(data.smtp_from ?? "");
      setSmtpTls(data.smtp_tls ?? true);
      setSmtpUser(data.smtp_user ?? "");
      setSmtpPassword("");
      setSmtpLoadState("ready");
    } catch (err) {
      const text = err instanceof ApiError ? err.message : te("smtpLoadFailed");
      setSmtpMessage({ type: "error", text });
      setSmtpLoadState("error");
    }
  }, [te]);

  useEffect(() => {
    if (activeTab === "settings" && smtpLoadState === "idle") {
      void loadSmtpSettings();
    }
  }, [activeTab, loadSmtpSettings, smtpLoadState]);

  const saveSmtpSettings = async () => {
    if (smtpLoadState !== "ready") return;

    setSmtpSaving(true);
    setSmtpMessage(null);
    try {
      const body: SmtpSettingsUpdate = {
        smtp_host: smtpHost,
        smtp_port: smtpPort,
        smtp_from: smtpFrom,
        smtp_tls: smtpTls,
        smtp_user: smtpUser,
        smtp_password: smtpPassword,
      };
      const response = await apiFetch<SettingsSaveResponse>("/api/auth/admin/email/smtp-settings", {
        method: "PUT",
        body,
      });
      setSmtpPassword("");
      setSmtpMessage({
        type: "success",
        text: response.cache_refresh_pending ? te("smtpSavedCachePending") : te("smtpSaved"),
      });
    } catch (err) {
      const text = err instanceof ApiError ? err.message : te("smtpSaveFailed");
      setSmtpMessage({ type: "error", text });
    } finally {
      setSmtpSaving(false);
    }
  };

  const testSmtp = async () => {
    if (smtpLoadState !== "ready") return;

    setSmtpTesting(true);
    setSmtpMessage(null);
    try {
      await apiFetch<{ message: string }>("/api/auth/admin/email/test-smtp", { method: "POST" });
      setSmtpMessage({ type: "success", text: te("smtpTestRequested") });
    } catch (err) {
      const text = err instanceof ApiError ? err.message : te("smtpTestFailed");
      setSmtpMessage({ type: "error", text });
    } finally {
      setSmtpTesting(false);
    }
  };

  const toggleSelectUser = (user: EmailUserItem) => {
    setSelectedUsers((prev) => {
      const exists = prev.find((u) => u.id === user.id);
      if (exists) return prev.filter((u) => u.id !== user.id);
      return [...prev, user];
    });
  };

  const handleSend = async () => {
    setSending(true);
    setResult(null);
    setError("");
    try {
      const payload: Record<string, unknown> = { target, subject, body };
      if (target === "user") {
        payload.user_ids = selectedUsers.map((u) => u.id);
      } else if (target === "role") {
        payload.role = role;
      }

      const res = await apiFetch<{ success: number; failed: number }>(
        "/api/auth/admin/email/send",
        { method: "POST", body: payload }
      );
      setResult(res);
      // 발송 성공 시 폼 초기화
      setSubject("");
      setBody("");
      setSelectedUsers([]);
      setUserSearch("");
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : t("emailSendFailed");
      setError(msg);
    } finally {
      setSending(false);
    }
  };

  const canSend =
    subject.trim() &&
    !isRichContentEmpty(body) &&
    (target !== "user" || selectedUsers.length > 0);

  const inputClass =
    "w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none";

  const totalPages = Math.ceil(logTotal / LOG_PAGE_SIZE);

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <h1 className="text-2xl font-bold">{t("emailSendTitle")}</h1>

      {/* 탭 전환 */}
      <div className="flex gap-1 rounded-lg border border-border bg-bg-primary p-1">
        <button
          onClick={() => setActiveTab("settings")}
          className={`flex-1 rounded-md px-4 py-2 text-sm font-medium transition-colors ${
            activeTab === "settings"
              ? "bg-accent-primary text-white"
              : "text-text-secondary hover:text-text-primary"
          }`}
        >
          {te("tabSettings")}
        </button>
        <button
          onClick={() => setActiveTab("compose")}
          className={`flex-1 rounded-md px-4 py-2 text-sm font-medium transition-colors ${
            activeTab === "compose"
              ? "bg-accent-primary text-white"
              : "text-text-secondary hover:text-text-primary"
          }`}
        >
          {te("tabCompose")}
        </button>
        <button
          onClick={() => setActiveTab("history")}
          className={`flex-1 rounded-md px-4 py-2 text-sm font-medium transition-colors ${
            activeTab === "history"
              ? "bg-accent-primary text-white"
              : "text-text-secondary hover:text-text-primary"
          }`}
        >
          {te("tabHistory")}
        </button>
      </div>

      {/* ===== SMTP 설정 탭 ===== */}
      {activeTab === "settings" && (
        <div className="rounded-xl border border-border bg-bg-surface p-6">
          <h2 className="mb-2 text-lg font-semibold">{te("smtpSettings")}</h2>
          <p className="mb-6 text-sm text-text-muted">{te("smtpSettingsDescription")}</p>

          {smtpMessage && (
            <div
              className={`mb-4 rounded-lg border p-3 text-sm ${
                smtpMessage.type === "success"
                  ? "border-accent-success/30 bg-accent-success/10 text-accent-success"
                  : "border-accent-error/30 bg-accent-error/10 text-accent-error"
              }`}
            >
              {smtpMessage.text}
            </div>
          )}

          {smtpLoadState === "error" ? (
            <button
              type="button"
              onClick={() => void loadSmtpSettings()}
              className="rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-bg-primary"
            >
              {te("retry")}
            </button>
          ) : smtpLoadState !== "ready" ? (
            <p className="text-sm text-text-muted">{t("loadingData")}</p>
          ) : (
            <div className="max-w-xl space-y-4">
              <div>
                <label className="mb-1 block text-sm font-medium">{te("smtpHost")}</label>
                <input
                  value={smtpHost}
                  onChange={(event) => setSmtpHost(event.target.value)}
                  placeholder="smtp.gmail.com"
                  className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm"
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium">{te("smtpPort")}</label>
                <input
                  type="number"
                  min={1}
                  max={65535}
                  value={smtpPort}
                  onChange={(event) => setSmtpPort(Number(event.target.value))}
                  className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm"
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium">{te("smtpFrom")}</label>
                <input
                  value={smtpFrom}
                  onChange={(event) => setSmtpFrom(event.target.value)}
                  placeholder="noreply@example.com"
                  className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm"
                />
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={smtpTls}
                  onChange={(event) => setSmtpTls(event.target.checked)}
                  className="h-4 w-4 rounded border-border accent-accent-primary"
                />
                {te("smtpTls")}
              </label>
              <div>
                <label className="mb-1 block text-sm font-medium">{te("smtpUser")}</label>
                <input
                  value={smtpUser}
                  onChange={(event) => setSmtpUser(event.target.value)}
                  className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm"
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium">{te("smtpPassword")}</label>
                <input
                  type="password"
                  value={smtpPassword}
                  onChange={(event) => setSmtpPassword(event.target.value)}
                  placeholder={te("smtpPasswordHint")}
                  className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm"
                />
              </div>
              <div className="flex gap-3 pt-2">
                <button
                  type="button"
                  onClick={saveSmtpSettings}
                  disabled={smtpSaving}
                  className="rounded-lg bg-accent-primary px-4 py-2 text-sm font-medium text-white hover:bg-accent-primary/90 disabled:opacity-50"
                >
                  {smtpSaving ? te("saving") : te("smtpSave")}
                </button>
                <button
                  type="button"
                  onClick={testSmtp}
                  disabled={smtpTesting}
                  className="rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-bg-primary disabled:opacity-50"
                >
                  {smtpTesting ? te("testing") : te("smtpTest")}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* ===== 발송 탭 ===== */}
      {activeTab === "compose" && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
          {/* 왼쪽: 발송 폼 (3/5) */}
          <div className="space-y-6 lg:col-span-3">
            {/* 결과 메시지 */}
            {result && (
              <div className="rounded-lg border border-accent-success/30 bg-accent-success/10 p-3 text-sm text-accent-success">
                {t("emailSendSuccess", { success: result.success, failed: result.failed })}
              </div>
            )}
            {error && (
              <div className="rounded-lg border border-accent-error/30 bg-accent-error/10 p-3 text-sm text-accent-error">
                {error}
              </div>
            )}

            {/* 대상 선택 */}
            <div className="rounded-xl border border-border bg-bg-surface p-5">
              <h2 className="mb-4 text-lg font-semibold">{t("emailSendTitle")}</h2>
              <div className="space-y-4">
                {/* 라디오 그룹 */}
                <fieldset>
                  <legend className="mb-2 text-sm font-medium">{t("emailSendTitle")}</legend>
                  <div className="flex gap-4">
                    {(["all", "role", "user"] as TargetType[]).map((t_) => (
                      <label key={t_} className="flex cursor-pointer items-center gap-2">
                        <input
                          type="radio"
                          name="target"
                          value={t_}
                          checked={target === t_}
                          onChange={() => setTarget(t_)}
                          className="accent-accent-primary"
                        />
                        <span className="text-sm">
                          {t_ === "all" && t("emailTargetAll")}
                          {t_ === "role" && t("emailTargetRole")}
                          {t_ === "user" && t("emailTargetUser")}
                        </span>
                      </label>
                    ))}
                  </div>
                </fieldset>

                {/* 역할 선택 */}
                {target === "role" && (
                  <div>
                    <select
                      value={role}
                      onChange={(e) => setRole(e.target.value)}
                      aria-label={t("emailTargetRole")}
                      className="rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm"
                    >
                      <option value="user">{t("roleUser")}</option>
                      <option value="moderator">{t("roleModerator")}</option>
                      <option value="admin">{t("roleAdmin")}</option>
                    </select>
                  </div>
                )}

                {/* 사용자 검색 */}
                {target === "user" && (
                  <div className="space-y-3">
                    <input
                      value={userSearch}
                      onChange={(e) => setUserSearch(e.target.value)}
                      placeholder={t("emailSearchUsers")}
                      aria-label={t("emailSearchUsers")}
                      className={inputClass}
                    />

                    {/* 검색 결과 */}
                    {searching && (
                      <p className="text-sm text-text-muted">{t("loadingData")}</p>
                    )}
                    {searchResults.length > 0 && (
                      <div className="max-h-40 space-y-1 overflow-y-auto rounded-lg border border-border p-2">
                        {searchResults.map((u) => {
                          const isSelected = selectedUsers.some((s) => s.id === u.id);
                          return (
                            <label
                              key={u.id}
                              className={`flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-bg-elevated ${
                                isSelected ? "bg-accent-primary/10" : ""
                              }`}
                            >
                              <input
                                type="checkbox"
                                checked={isSelected}
                                onChange={() => toggleSelectUser(u)}
                                className="accent-accent-primary"
                              />
                              <span className="font-medium">{u.username}</span>
                              <span className="text-text-muted">{u.email}</span>
                            </label>
                          );
                        })}
                      </div>
                    )}

                    {/* 선택된 사용자 */}
                    {selectedUsers.length > 0 && (
                      <div className="flex flex-wrap gap-2">
                        {selectedUsers.map((u) => (
                          <span
                            key={u.id}
                            className="inline-flex items-center gap-1 rounded-full bg-accent-primary/10 px-3 py-1 text-xs text-accent-primary"
                          >
                            {u.username}
                            <button
                              onClick={() => toggleSelectUser(u)}
                              className="ml-1 hover:text-accent-error"
                              aria-label={te("removeUser", { name: u.username })}
                            >
                              &times;
                            </button>
                          </span>
                        ))}
                      </div>
                    )}
                    {target === "user" && selectedUsers.length === 0 && (
                      <p className="text-sm text-text-muted">{t("emailNoSelectedUsers")}</p>
                    )}
                  </div>
                )}
              </div>
            </div>

            {/* 제목 & 본문 */}
            <div className="rounded-xl border border-border bg-bg-surface p-5 space-y-4">
              <div>
                <label htmlFor="email-subject" className="mb-1.5 block text-sm font-medium">
                  {t("emailSubject")}
                </label>
                <input
                  id="email-subject"
                  value={subject}
                  onChange={(e) => setSubject(e.target.value)}
                  className={inputClass}
                />
              </div>
              <div>
                <label htmlFor="email-body" className="mb-1.5 block text-sm font-medium">
                  {t("emailBody")}
                </label>
                <RichTextEditor
                  value={body}
                  onChange={setBody}
                  ariaLabel={t("emailBody")}
                />
              </div>
            </div>

            <div className="flex justify-end">
              <Button onClick={() => setShowSendConfirm(true)} disabled={sending || !canSend}>
                {sending ? t("emailSending") : t("emailSend")}
              </Button>
            </div>

            {/* 발송 확인 모달 */}
            <ConfirmModal
              open={showSendConfirm}
              title={te("confirmSendTitle")}
              description={te("confirmSendDesc", {
                target: target === "all" ? t("emailTargetAll") : target === "role" ? role : `${selectedUsers.length}${te("confirmSendUsers")}`,
              })}
              confirmLabel={t("emailSend")}
              variant="danger"
              onConfirm={() => { setShowSendConfirm(false); handleSend(); }}
              onCancel={() => setShowSendConfirm(false)}
            />
          </div>

          {/* 오른쪽: 미리보기 패널 (2/5) */}
          <div className="lg:col-span-2">
            <div className="sticky top-4 rounded-xl border border-border bg-bg-surface p-4">
              <h3 className="mb-3 text-sm font-medium text-text-secondary">
                {te("preview")}
              </h3>
              {previewHtml ? (
                <iframe
                  srcDoc={previewHtml}
                  className="w-full rounded-lg border border-border bg-white"
                  style={{ height: "24rem" }}
                  sandbox=""
                  title="Email Preview"
                />
              ) : (
                <div className="flex h-60 items-center justify-center rounded-lg border border-dashed border-border">
                  <p className="text-sm text-text-muted">{te("previewEmpty")}</p>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ===== 이력 탭 ===== */}
      {activeTab === "history" && (
        <div className="rounded-xl border border-border bg-bg-surface">
          <div
            role="region"
            aria-label={te("tabHistory")}
            tabIndex={0}
            className="overflow-x-auto focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent-primary"
          >
            <table className="w-full min-w-min text-sm">
              <thead>
                <tr className="border-b border-border text-left text-text-secondary">
                  <th className="whitespace-nowrap px-4 py-3 font-medium">{te("logDate")}</th>
                  <th className="whitespace-nowrap px-4 py-3 font-medium">{te("logTarget")}</th>
                  <th className="whitespace-nowrap px-4 py-3 font-medium">{te("logSubject")}</th>
                  <th className="whitespace-nowrap px-4 py-3 font-medium">{te("logResult")}</th>
                </tr>
              </thead>
              <tbody>
                {logsLoading ? (
                  <tr>
                    <td colSpan={4} className="px-4 py-8 text-center text-text-muted">
                      <div className="inline-block h-5 w-5 animate-spin rounded-full border-2 border-current border-t-transparent" />
                    </td>
                  </tr>
                ) : logs.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="px-4 py-8 text-center text-text-muted">
                      {te("logNoData")}
                    </td>
                  </tr>
                ) : (
                  logs.map((log) => (
                    <tr key={log.id} className="border-b border-border last:border-0 hover:bg-bg-elevated/50">
                      <td className="whitespace-nowrap px-4 py-3 text-text-secondary">
                        {new Date(log.created_at).toLocaleString()}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <span className="rounded-full bg-accent-primary/10 px-2 py-0.5 text-xs text-accent-primary">
                          {log.target_type}
                        </span>
                      </td>
                      <td className="max-w-xs truncate px-4 py-3" title={log.subject}>
                        {log.subject}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <span className="text-accent-success">{log.success_count}</span>
                        {log.fail_count > 0 && (
                          <>
                            {" / "}
                            <span className="text-accent-error">{log.fail_count}</span>
                          </>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {/* 페이지네이션 */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between border-t border-border px-4 py-3">
              <button
                onClick={() => fetchLogs(logPage - 1)}
                disabled={logPage <= 1}
                className="rounded-lg px-3 py-1.5 text-sm text-text-secondary hover:bg-bg-elevated disabled:opacity-40"
              >
                {te("logPrev")}
              </button>
              <span className="text-sm text-text-muted">
                {logPage} / {totalPages}
              </span>
              <button
                onClick={() => fetchLogs(logPage + 1)}
                disabled={logPage >= totalPages}
                className="rounded-lg px-3 py-1.5 text-sm text-text-secondary hover:bg-bg-elevated disabled:opacity-40"
              >
                {te("logNext")}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
