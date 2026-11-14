"use client";

import { useState, useEffect, useLayoutEffect, useCallback, useRef } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { apiFetch, ApiError, captureApiRequestOwner, isCurrentApiRequestOwner } from "@/lib/api-client";
import { isRichContentEmpty } from "@/lib/rich-content.mjs";
import { useToast } from "@/providers/ToastProvider";
import { useAuth } from "@/providers/AuthProvider";

/* ── 타입 ── */

interface InquirySummary {
  id: string;
  user_id: string | null;
  email: string;
  name: string;
  subject: string;
  status: string;
  created_at: string | null;
}

interface InquiryReply {
  id: string;
  admin_name: string;
  content: string;
  created_at: string | null;
}

interface InquiryEvent {
  actor_type: string;
  action: string;
  created_at: string | null;
}

interface InquiryDetail {
  id: string;
  user_id: string | null;
  email: string;
  name: string;
  subject: string;
  content: string;
  status: string;
  created_at: string | null;
  replies: InquiryReply[];
  events?: InquiryEvent[];
  notification_summary?: {
    queued: number;
    sent: number;
    skipped: number;
  };
}

interface InquiryReplyResponse {
  message: string;
  status: string;
  notification: {
    state: "queued";
    event_id: string;
  };
}

interface InquirySelection {
  id: string | null;
  generation: number;
}

type InquiryAction = "reply" | "close";

/* ── 상수 ── */

const STATUS_FILTERS = ["all", "open", "answered", "closed"] as const;
type StatusFilter = (typeof STATUS_FILTERS)[number];

const STATUS_BADGE_CLASSES: Record<string, string> = {
  open: "bg-accent-warning/15 text-accent-warning",
  answered: "bg-accent-success/15 text-accent-success",
  closed: "bg-text-muted/15 text-text-muted",
};

/* ── 페이지 ── */

export default function AdminInquiriesPage() {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const { toast } = useToast();
  const { user } = useAuth();

  /* 목록 상태 */
  const [inquiries, setInquiries] = useState<InquirySummary[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [loading, setLoading] = useState(true);
  const listQueryKey = `${page}:${statusFilter}`;
  const listQueryRef = useRef({ key: listQueryKey, page, statusFilter });
  const latestFetchListRef = useRef<(() => Promise<void>) | null>(null);

  /* 상세/답변 패널 */
  const [selection, setSelection] = useState<InquirySelection>({ id: null, generation: 0 });
  const selectedId = selection.id;
  const [detailState, setDetailState] = useState<{ selection: InquirySelection; data: InquiryDetail } | null>(null);
  const loadedDetailRef = useRef<{ selection: InquirySelection; data: InquiryDetail } | null>(null);
  const detail = detailState?.selection === selection ? detailState.data : null;
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [replyContent, setReplyContent] = useState("");
  const [pendingActions, setPendingActions] = useState<Record<string, InquiryAction>>({});
  const selectionRef = useRef(selection);
  const replyDraftsRef = useRef(new Map<string, string>());
  const pendingActionsRef = useRef(new Map<string, { action: InquiryAction; controller: AbortController }>());
  const detailControllerRef = useRef<AbortController | null>(null);
  const detailRequestRef = useRef(0);
  const listControllerRef = useRef<AbortController | null>(null);
  const listRequestRef = useRef(0);
  const mountedRef = useRef(true);
  const replying = selectedId !== null && pendingActions[selectedId] === "reply";
  const closing = selectedId !== null && pendingActions[selectedId] === "close";
  const detailReady = detail?.id === selectedId && !detailLoading;

  const isCurrentSelection = useCallback((target: InquirySelection) => (
    mountedRef.current && selectionRef.current === target
  ), []);

  useEffect(() => {
    mountedRef.current = true;
    const pending = pendingActionsRef.current;
    return () => {
      mountedRef.current = false;
      loadedDetailRef.current = null;
      detailControllerRef.current?.abort();
      listControllerRef.current?.abort();
      for (const operation of pending.values()) operation.controller.abort();
      pending.clear();
    };
  }, []);

  const PAGE_SIZE = 20;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  /* ── 목록 조회 ── */

  const fetchList = useCallback(async () => {
    if (!mountedRef.current || listQueryRef.current.key !== listQueryKey) return;
    listControllerRef.current?.abort();
    const controller = new AbortController();
    listControllerRef.current = controller;
    const requestId = ++listRequestRef.current;
    const isCurrent = () => mountedRef.current && !controller.signal.aborted
      && listRequestRef.current === requestId && listQueryRef.current.key === listQueryKey;
    setLoading(true);
    try {
      const params = new URLSearchParams({
        page: String(page),
        size: String(PAGE_SIZE),
      });
      if (statusFilter !== "all") params.set("status", statusFilter);

      const data = await apiFetch<{ items: InquirySummary[]; total: number }>(
        `/api/community/inquiries?${params}`,
        { signal: controller.signal },
      );
      if (!isCurrent()) return;
      setInquiries(data.items);
      setTotal(data.total);
    } catch {
      if (!isCurrent()) return;
      toast("error", t("loadFailed"));
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [listQueryKey, page, statusFilter, t, toast]);

  useLayoutEffect(() => {
    latestFetchListRef.current = fetchList;
  }, [fetchList]);

  useEffect(() => {
    void fetchList();
  }, [fetchList]);

  /* ── 상세 조회 ── */

  const fetchDetail = useCallback(
    async (target: InquirySelection) => {
      if (!target.id || !isCurrentSelection(target)) return;
      detailControllerRef.current?.abort();
      const controller = new AbortController();
      detailControllerRef.current = controller;
      const requestId = ++detailRequestRef.current;
      const isCurrent = () => isCurrentSelection(target) && !controller.signal.aborted && detailRequestRef.current === requestId;
      loadedDetailRef.current = null;
      setDetailState(null);
      setDetailLoading(true);
      setDetailError(null);
      try {
        const data = await apiFetch<InquiryDetail>(
          `/api/community/inquiries/${target.id}`,
          { signal: controller.signal },
        );
        if (!isCurrent()) return;
        if (data.id !== target.id) throw new Error("inquiry_identity_mismatch");
        const loaded = { selection: target, data };
        loadedDetailRef.current = loaded;
        setDetailState(loaded);
      } catch (error) {
        if (!isCurrent()) return;
        setDetailError(error instanceof ApiError ? error.message : t("loadFailed"));
      } finally {
        if (isCurrent()) setDetailLoading(false);
      }
    },
    [isCurrentSelection, t],
  );

  const selectInquiry = (id: string | null) => {
    const target = { id, generation: selectionRef.current.generation + 1 };
    selectionRef.current = target;
    loadedDetailRef.current = null;
    detailControllerRef.current?.abort();
    detailRequestRef.current += 1;
    setSelection(target);
    setDetailState(null);
    setDetailError(null);
    setDetailLoading(false);
    setReplyContent(id ? replyDraftsRef.current.get(id) ?? "" : "");
    if (id) void fetchDetail(target);
  };

  const handleSelect = (id: string) => {
    selectInquiry(selectionRef.current.id === id ? null : id);
  };

  const handleReplyChange = (content: string) => {
    if (!selection.id || !isCurrentSelection(selection)) return;
    replyDraftsRef.current.set(selection.id, content);
    setReplyContent(content);
  };

  /* 선택과 초안을 함께 캡처하여 이전 화면의 callback도 전송 전에 차단한다. */
  const submitInquiryAction = async (action: InquiryAction) => {
    const target = selection;
    const targetId = target.id;
    const loaded = loadedDetailRef.current;
    if (!targetId || !isCurrentSelection(target) || !detailReady || detail?.id !== targetId
      || loaded?.selection !== target || loaded.data.id !== targetId || loaded.data.status === "closed"
      || detail.status === "closed" || pendingActionsRef.current.has(targetId)) return;
    const submittedContent = replyContent;
    if (action === "reply" && isRichContentEmpty(submittedContent)) return;

    const operation = { action, controller: new AbortController() };
    let owner: ReturnType<typeof captureApiRequestOwner> | null = null;
    try {
      owner = captureApiRequestOwner(user?.id ?? "");
      pendingActionsRef.current.set(targetId, operation);
      setPendingActions((current) => ({ ...current, [targetId]: action }));
      if (action === "reply") {
        const result = await apiFetch<InquiryReplyResponse>(`/api/community/inquiries/${targetId}/reply`, {
          method: "POST", body: { content: submittedContent }, requestOwner: owner, signal: operation.controller.signal,
        });
        if (!isCurrentApiRequestOwner(owner)) return;
        const submittedDraftUnchanged = replyDraftsRef.current.get(targetId) === submittedContent;
        if (submittedDraftUnchanged) {
          replyDraftsRef.current.delete(targetId);
        }
        if (submittedDraftUnchanged && mountedRef.current && selectionRef.current.id === targetId) {
          setReplyContent((current) => current === submittedContent ? "" : current);
        }
        if (!isCurrentSelection(target)) {
          const currentSelection = selectionRef.current;
          if (mountedRef.current && currentSelection.id === targetId) {
            void fetchDetail(currentSelection);
            void latestFetchListRef.current?.();
          }
          return;
        }
        toast("success", result.notification.state === "queued" ? t("inquiryNotificationQueued") : t("inquirySend"));
      } else {
        await apiFetch(`/api/community/inquiries/${targetId}/status?status=closed`, {
          method: "PATCH", requestOwner: owner, signal: operation.controller.signal,
        });
        if (!isCurrentSelection(target) || !isCurrentApiRequestOwner(owner)) return;
        toast("success", t("inquiryStatusClosed"));
      }
      void fetchDetail(target);
      void latestFetchListRef.current?.();
    } catch (err) {
      if (isCurrentSelection(target) && (!owner || isCurrentApiRequestOwner(owner))) {
        toast("error", err instanceof ApiError ? err.message : t("loadFailed"));
      }
    } finally {
      if (pendingActionsRef.current.get(targetId) === operation) {
        pendingActionsRef.current.delete(targetId);
        if (mountedRef.current) setPendingActions((current) => {
          const next = { ...current };
          delete next[targetId];
          return next;
        });
      }
    }
  };

  const handleReply = () => submitInquiryAction("reply");
  const handleClose = () => submitInquiryAction("close");

  /* ── 필터 탭 변경 ── */

  const handleFilterChange = (tab: StatusFilter) => {
    listQueryRef.current = { key: `1:${tab}`, page: 1, statusFilter: tab };
    listControllerRef.current?.abort();
    listRequestRef.current += 1;
    setStatusFilter(tab);
    setPage(1);
    selectInquiry(null);
  };

  const handlePageChange = (nextPage: number) => {
    const nextFilter = listQueryRef.current.statusFilter;
    listQueryRef.current = { key: `${nextPage}:${nextFilter}`, page: nextPage, statusFilter: nextFilter };
    listControllerRef.current?.abort();
    listRequestRef.current += 1;
    setPage(nextPage);
  };

  /* ── 상태 뱃지 ── */

  const statusLabel = (status: string): string => {
    const labels: Record<string, string> = {
      open: t("inquiryStatusOpen"),
      answered: t("inquiryStatusAnswered"),
      closed: t("inquiryStatusClosed"),
    };
    return labels[status] ?? status;
  };

  const eventLabel = (action: string): string => {
    const labels: Record<string, string> = {
      "inquiry.created": t("inquiryEventCreated"),
      "inquiry.replied": t("inquiryEventReplied"),
      "inquiry.status_changed": t("inquiryEventStatusChanged"),
      "inquiry.guest_access_requested": t("inquiryEventGuestAccessRequested"),
      "inquiry.guest_access_verified": t("inquiryEventGuestAccessVerified"),
      "inquiry.guest_access_challenge_issued": t("inquiryEventGuestAccessChallengeIssued"),
      "inquiry.notification_enqueued": t("inquiryEventNotificationEnqueued"),
      "inquiry.notification_claimed": t("inquiryEventNotificationClaimed"),
      "inquiry.notification_completed": t("inquiryEventNotificationCompleted"),
      "inquiry.notification_retry": t("inquiryEventNotificationRetry"),
      "inquiry.notification_sent": t("inquiryEventNotificationSent"),
      "inquiry.notification_skipped": t("inquiryEventNotificationSkipped"),
    };
    return labels[action] ?? action;
  };

  const statusBadge = (status: string) => (
    <span
      className={`inline-block shrink-0 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ${
        STATUS_BADGE_CLASSES[status] ?? ""
      }`}
    >
      {statusLabel(status)}
    </span>
  );

  /* ── 날짜 포맷 ── */

  const formatDate = (iso: string | null) => {
    if (!iso) return "-";
    return new Date(iso).toLocaleString();
  };

  const formatDateShort = (iso: string | null) => {
    if (!iso) return "-";
    return new Date(iso).toLocaleDateString();
  };

  /* ── 필터 탭 라벨 ── */

  const filterLabel = (tab: StatusFilter): string => {
    if (tab === "all") return tc("all");
    return statusLabel(tab);
  };

  return (
    <div className="space-y-6">
      {/* 헤더 */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-text-primary">
          {t("inquiryManagement")}
        </h1>
        <span className="text-sm text-text-muted">
          {t("totalCount", { count: total })}
        </span>
      </div>

      {/* 상태 필터 탭 */}
      <div className="flex gap-1 rounded-lg border border-border bg-bg-surface p-1">
        {STATUS_FILTERS.map((tab) => (
          <button
            key={tab}
            onClick={() => handleFilterChange(tab)}
            className={`rounded-md px-4 py-2 text-sm font-medium transition-colors ${
              statusFilter === tab
                ? "bg-accent-primary text-white"
                : "text-text-secondary hover:bg-bg-elevated hover:text-text-primary"
            }`}
          >
            {filterLabel(tab)}
          </button>
        ))}
      </div>

      {/* 본문: 목록 + 상세 패널 */}
      <div className="flex flex-col gap-6 lg:flex-row">
        {/* 좌측: 문의 목록 테이블 */}
        <div
          className={`min-w-0 overflow-hidden rounded-lg border border-border bg-bg-surface transition-all ${
            selectedId ? "w-full lg:w-1/2" : "w-full"
          }`}
        >
          {loading ? (
            <div className="px-5 py-12 text-center text-sm text-text-muted">
              {t("loadingData")}
            </div>
          ) : inquiries.length === 0 ? (
            <div className="px-5 py-12 text-center">
              <p className="text-lg font-medium text-text-primary">
                {t("noInquiries")}
              </p>
              <p className="mt-1 text-sm text-text-secondary">
                {t("noInquiriesDesc")}
              </p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full table-fixed text-sm">
                <thead className="bg-bg-surface text-left text-text-secondary">
                  <tr>
                    <th className="px-4 py-3 font-medium">
                      {t("inquirySubject")}
                    </th>
                    {!selectedId && (
                      <th className="hidden px-4 py-3 font-medium whitespace-nowrap xl:table-cell xl:w-44">
                        {t("colEmail")}
                      </th>
                    )}
                    <th className="w-32 px-4 py-3 font-medium whitespace-nowrap sm:w-28">
                      {t("inquiryStatus")}
                    </th>
                    <th className="hidden px-4 py-3 font-medium whitespace-nowrap md:table-cell md:w-28">
                      {t("colTime")}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {inquiries.map((inq) => (
                    <tr
                      key={inq.id}
                      onClick={() => handleSelect(inq.id)}
                      className={`cursor-pointer transition-colors hover:bg-bg-elevated ${
                        selectedId === inq.id ? "bg-bg-elevated" : ""
                      }`}
                    >
                      <td className="px-4 py-3">
                        <div className="max-w-full truncate font-medium text-text-primary">
                          {inq.subject}
                        </div>
                        <div className="mt-0.5 text-xs text-text-muted">
                          {inq.name}
                          <span className="mt-0.5 block whitespace-nowrap md:hidden">
                            {formatDateShort(inq.created_at)}
                          </span>
                        </div>
                      </td>
                      {!selectedId && (
                        <td className="hidden truncate px-4 py-3 text-text-secondary xl:table-cell">
                          {inq.email}
                        </td>
                      )}
                      <td className="px-4 py-3">{statusBadge(inq.status)}</td>
                      <td className="hidden px-4 py-3 text-xs text-text-muted whitespace-nowrap md:table-cell">
                        {selectedId
                          ? formatDateShort(inq.created_at)
                          : formatDate(inq.created_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* 페이지네이션 */}
          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-2 border-t border-border px-5 py-3">
              <button
                onClick={() => handlePageChange(Math.max(1, page - 1))}
                disabled={page <= 1}
                className="rounded px-3 py-1 text-sm text-text-secondary hover:bg-bg-elevated disabled:opacity-40"
              >
                {t("prev")}
              </button>
              <span className="text-xs text-text-muted">
                {page} / {totalPages}
              </span>
              <button
                onClick={() => handlePageChange(Math.min(totalPages, page + 1))}
                disabled={page >= totalPages}
                className="rounded px-3 py-1 text-sm text-text-secondary hover:bg-bg-elevated disabled:opacity-40"
              >
                {t("next")}
              </button>
            </div>
          )}
        </div>

        {/* 우측: 문의 상세 패널 */}
        {selectedId && (
          <div className="w-full min-w-0 lg:w-1/2">
            <div className="rounded-lg border border-border bg-bg-surface">
              {detailLoading ? (
                <div className="px-5 py-12 text-center text-sm text-text-muted">
                  {t("loadingData")}
                </div>
              ) : detailError ? (
                <div className="space-y-3 px-5 py-6">
                  <p role="alert" className="text-sm text-accent-error">{detailError}</p>
                  <Button variant="secondary" onClick={() => void fetchDetail(selection)}>{tc("retry")}</Button>
                </div>
              ) : detail ? (
                <div className="divide-y divide-border">
                  {/* 헤더 */}
                  <div className="px-5 py-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <h2 className="break-words text-lg font-semibold text-text-primary">
                          {detail.subject}
                        </h2>
                        <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-text-muted">
                          <span className="max-w-full break-words">{detail.name}</span>
                          <span className="max-w-full break-words">{detail.email}</span>
                          <span className="max-w-full break-words">{formatDate(detail.created_at)}</span>
                        </div>
                      </div>
                      {statusBadge(detail.status)}
                    </div>
                  </div>

                  {/* 문의 내용 */}
                  <div className="px-5 py-4">
                    <h3 className="mb-2 text-sm font-medium text-text-secondary">
                      {t("inquiryContent")}
                    </h3>
                    <div className="rounded-lg border border-border bg-bg-primary p-4 text-sm text-text-primary leading-relaxed">
                      <MarkdownRenderer content={detail.content} />
                    </div>
                  </div>

                  {/* 답변 목록 */}
                  {detail.replies.length > 0 && (
                    <div className="px-5 py-4">
                      <h3 className="mb-3 text-sm font-semibold text-text-secondary">
                        {t("inquiryReply")} ({detail.replies.length})
                      </h3>
                      <div className="space-y-3">
                        {detail.replies.map((r) => (
                          <div
                            key={r.id}
                            className="rounded-lg border border-accent-primary/20 bg-accent-primary/5 p-4"
                          >
                            <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2 text-xs text-text-muted">
                              <span className="min-w-0 max-w-full break-words font-medium text-accent-primary">
                                {r.admin_name}
                              </span>
                              <span className="max-w-full break-words">{formatDate(r.created_at)}</span>
                            </div>
                            <MarkdownRenderer content={r.content} />
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* 문의 처리 이력 — 답변·상태 변경의 감사 편의용 */}
                  {detail.events && detail.events.length > 0 && (
                    <div className="px-5 py-4">
                      <h3 className="mb-3 text-sm font-semibold text-text-secondary">
                        {t("inquiryHistory")}
                      </h3>
                      <ol className="space-y-2 border-l border-border pl-4 text-xs text-text-muted">
                        {detail.events.map((event, index) => (
                          <li key={`${event.action}-${event.created_at ?? index}`} className="relative">
                            <span className="absolute -left-[1.05rem] top-1 h-2 w-2 rounded-full bg-accent-primary" aria-hidden="true" />
                            <span className="font-medium text-text-secondary">{eventLabel(event.action)}</span>
                            {event.created_at && <span className="ml-2">{formatDate(event.created_at)}</span>}
                          </li>
                        ))}
                      </ol>
                    </div>
                  )}

                  {detail.notification_summary && detail.notification_summary.queued > 0 && (
                    <div className="px-5 py-3 text-xs text-text-muted">
                      {t("inquiryNotificationsQueuedCount", {
                        count: detail.notification_summary.queued,
                      })}
                    </div>
                  )}

                  {/* 답변 입력 (종료된 문의가 아닌 경우만) */}
                  {detail.status !== "closed" && (
                    <div className="px-5 py-4 space-y-3">
                      <h3 className="text-sm font-medium text-text-secondary">
                        {t("inquiryReply")}
                      </h3>
                      <RichTextEditor
                        value={replyContent}
                        onChange={handleReplyChange}
                        placeholder={t("inquiryReplyPlaceholder")}
                      />
                      <div className="flex flex-wrap items-center gap-3">
                        <Button
                          variant="primary"
                          size="sm"
                          onClick={handleReply}
                          disabled={!detailReady || replying || closing || isRichContentEmpty(replyContent)}
                        >
                          {replying ? tc("processing") : t("inquirySend")}
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={handleClose}
                          disabled={!detailReady || replying || closing}
                        >
                          {closing ? tc("processing") : t("inquiryClose")}
                        </Button>
                      </div>
                    </div>
                  )}
                </div>
              ) : null}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
