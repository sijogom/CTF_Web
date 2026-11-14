"use client";

import { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";
import { useParams } from "next/navigation";
import { Download } from "lucide-react";
import { Link, useRouter } from "@/i18n/navigation";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { apiFetch, ApiError } from "@/lib/api-client";
import { isRichContentEmpty, richContentToPlainText } from "@/lib/rich-content.mjs";
import { useAuth } from "@/providers/AuthProvider";
import { isAdminRole } from "@/lib/roles";
import { useMarketplacePurchase } from "@/lib/use-marketplace-purchase";
import { ReportButton } from "@/components/community/ReportButton";
import { DeleteConfirmModal } from "@/components/ui/DeleteConfirmModal";
import {
  ModerationActionDialog,
  type ModerationTarget,
} from "@/components/admin/ModerationActionDialog";

interface MarketItem {
  id: string;
  title: string;
  description: string;
  price: number;
  category: string;
  seller_id: string;
  seller_name: string;
  status: string;
  download_count: number;
  original_filename: string | null;
  created_at: string;
}

interface MarketComment {
  id: string;
  author_id: string;
  author_name: string;
  content: string;
  is_anonymous: boolean;
  is_deleted?: boolean;
  updated_at?: string | null;
  created_at: string;
}

export default function MarketItemDetailPage() {
  const t = useTranslations("market");
  const tc = useTranslations("common");
  const tCom = useTranslations("community");
  const ta = useTranslations("admin");
  const params = useParams();
  const router = useRouter();
  const itemId = params.id as string;
  const { user } = useAuth();

  const isAdmin = isAdminRole(user?.role);

  const [item, setItem] = useState<MarketItem | null>(null);
  const [loading, setLoading] = useState(true);
  const { purchasing, purchased, settlement, myPoints, purchaseError, refreshStatus, purchase } = useMarketplacePurchase(itemId, user?.id, t("purchaseStatusFailed"));
  const [showConfirm, setShowConfirm] = useState(false);
  const [message, setMessage] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [comments, setComments] = useState<MarketComment[]>([]);
  const [newComment, setNewComment] = useState("");
  const [commentAnonymous, setCommentAnonymous] = useState(false);
  const [commentLoading, setCommentLoading] = useState(false);

  // 삭제 (3단계 확인)
  const [deleteStep, setDeleteStep] = useState(0);
  const [deleteTarget, setDeleteTarget] = useState<{ type: "item" | "comment"; id: string } | null>(null);
  const [deleteLoading, setDeleteLoading] = useState(false);
  const [moderationTarget, setModerationTarget] = useState<ModerationTarget | null>(null);

  // 댓글 수정
  const [editingCommentId, setEditingCommentId] = useState<string | null>(null);
  const [editCommentContent, setEditCommentContent] = useState("");
  const [editCommentSaving, setEditCommentSaving] = useState(false);

  const fetchItem = useCallback(async () => {
    try {
      const data = await apiFetch<MarketItem>(
        `/api/community/marketplace/items/${itemId}`
      );
      setItem(data);
    } catch {
      setMessage({ type: "error", text: t("loadFailed") });
    } finally {
      setLoading(false);
    }
  }, [itemId, t]);

  useEffect(() => {
    fetchItem();
  }, [fetchItem]);

  // 댓글 목록 조회
  const fetchComments = useCallback(async () => {
    try {
      const data = await apiFetch<MarketComment[]>(
        `/api/community/marketplace/items/${itemId}/comments`
      );
      setComments(data);
    } catch {
      // 댓글 로드 실패는 무시 (아이템 자체 표시에 영향 없음)
    }
  }, [itemId]);

  useEffect(() => {
    fetchComments();
  }, [fetchComments]);

  // 댓글 작성
  const submitComment = async () => {
    if (isRichContentEmpty(newComment) || commentLoading) return;
    setCommentLoading(true);
    try {
      const created = await apiFetch<MarketComment>(
        `/api/community/marketplace/items/${itemId}/comments`,
        { method: "POST", body: { content: newComment, is_anonymous: commentAnonymous } }
      );
      setComments((prev) => [...prev, created]);
      setNewComment("");
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : t("commentFailed");
      setMessage({ type: "error", text: msg });
    } finally {
      setCommentLoading(false);
    }
  };

  const handlePurchase = async () => {
    if (!item) return;
    setMessage(null);
    const result = await purchase(item.price);
    setShowConfirm(false);
    if (result?.status === "completed") {
      setMessage({ type: "success", text: t("purchaseSuccess") });
    }
  };

  const handleDownload = async () => {
    try {
      // 백엔드가 StreamingResponse로 직접 파일을 반환
      const response = await fetch(
        `/ctf/api/community/marketplace/items/${itemId}/download`,
        { credentials: "include" },
      );
      if (!response.ok) {
        const err = await response.json().catch(() => null);
        throw new Error(err?.detail || t("downloadFailed"));
      }
      const blob = await response.blob();
      const disposition = response.headers.get("Content-Disposition") || "";
      const match = disposition.match(/filename\*?=(?:UTF-8'')?(.+)/);
      const filename = match ? decodeURIComponent(match[1]) : (item?.original_filename || "download");

      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      const msg = err instanceof Error ? err.message : t("downloadFailed");
      setMessage({ type: "error", text: msg });
    }
  };

  // ── 삭제 3단계 ──

  const startDelete = (type: "item" | "comment", id: string) => {
    setDeleteTarget({ type, id });
    setDeleteStep(1);
  };

  const requestContentRemoval = (type: "item" | "comment", id: string) => {
    if (isAdmin) {
      setModerationTarget({
        type: type === "item" ? "marketplace_item" : "marketplace_comment",
        id,
      });
      return;
    }
    startDelete(type, id);
  };

  const handleModerationCompleted = async () => {
    const target = moderationTarget;
    if (!target) return;
    setModerationTarget(null);
    setMessage({ type: "success", text: ta("moderationHideSuccess") });
    if (target.type === "marketplace_item") {
      router.push("/market");
      return;
    }
    await fetchComments();
  };

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;

    // step 1,2 → 다음 단계로
    if (deleteStep < 3) {
      setDeleteStep((s) => s + 1);
      return;
    }

    // step 3 → 실제 삭제 실행
    setDeleteLoading(true);
    try {
      if (deleteTarget.type === "item") {
        await apiFetch(`/api/community/marketplace/items/${itemId}`, {
          method: "DELETE",
        });
        setMessage({ type: "success", text: t("itemDeleted") });
        cancelDelete();
        router.push("/market");
        return;
      }

      // comment 삭제
      await apiFetch(
        `/api/community/marketplace/items/${itemId}/comments/${deleteTarget.id}`,
        { method: "DELETE" }
      );
      setComments((prev) =>
        prev.map((c) =>
          c.id === deleteTarget.id
            ? { ...c, is_deleted: true, content: t("deletedComment") }
            : c
        )
      );
      setMessage({ type: "success", text: t("commentDeleted") });
      cancelDelete();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : tc("error");
      setMessage({ type: "error", text: msg });
      cancelDelete();
    } finally {
      setDeleteLoading(false);
    }
  };

  const cancelDelete = () => {
    setDeleteStep(0);
    setDeleteTarget(null);
    setDeleteLoading(false);
  };

  // ── 댓글 수정 ──

  const startEditComment = (comment: MarketComment) => {
    setEditingCommentId(comment.id);
    setEditCommentContent(comment.content);
  };

  const saveCommentEdit = async () => {
    if (!editingCommentId || isRichContentEmpty(editCommentContent)) return;
    setEditCommentSaving(true);
    try {
      const updated = await apiFetch<MarketComment>(
        `/api/community/marketplace/items/${itemId}/comments/${editingCommentId}`,
        { method: "PATCH", body: { content: editCommentContent } }
      );
      setComments((prev) =>
        prev.map((c) => (c.id === editingCommentId ? { ...c, ...updated } : c))
      );
      setMessage({ type: "success", text: t("commentUpdated") });
      setEditingCommentId(null);
      setEditCommentContent("");
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : tc("error");
      setMessage({ type: "error", text: msg });
    } finally {
      setEditCommentSaving(false);
    }
  };

  const categoryLabel = (cat: string) => {
    const map: Record<string, string> = {
      exam: t("categoryExam"),
      material: t("categoryMaterial"),
      etc: t("categoryEtc"),
    };
    return map[cat] ?? cat;
  };

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }

  if (!item) {
    return (
      <div className="py-20 text-center text-text-muted">{t("loadFailed")}</div>
    );
  }

  const isOwner = user?.id === item.seller_id;
  const canDelete = isOwner || isAdmin;

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      {/* 뒤로가기 */}
      <Link
        href="/market"
        className="mb-4 inline-flex items-center gap-1 text-sm text-text-secondary hover:text-text-primary"
      >
        <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M19 12H5M12 19l-7-7 7-7" />
        </svg>
        {tc("back")}
      </Link>

      {/* 메시지 */}
      {message && (
        <div
          className={`mb-4 rounded-lg border p-3 text-sm ${
            message.type === "success"
              ? "border-accent-success/30 bg-accent-success/10 text-accent-success"
              : "border-accent-error/30 bg-accent-error/10 text-accent-error"
          }`}
        >
          {message.text}
        </div>
      )}

      {/* 아이템 카드 */}
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <div className="mb-4 flex items-start justify-between">
          <span className="rounded-md bg-bg-elevated px-2 py-0.5 text-xs font-medium text-text-muted">
            {categoryLabel(item.category)}
          </span>
          <div className="flex items-center gap-3">
            {user && !isOwner && (
              <ReportButton targetType="marketplace_item" targetId={item.id} />
            )}
            {canDelete && (
              <button
                onClick={() => requestContentRemoval("item", item.id)}
                className="rounded-md px-2 py-1 text-xs font-medium text-accent-error hover:bg-accent-error/10"
              >
                {isAdmin ? ta("moderationHideTitle") : t("deleteItem")}
              </button>
            )}
            <span className="text-2xl font-bold text-accent-warning">
              {item.price.toLocaleString()}P
            </span>
          </div>
        </div>

        <h1 className="mb-4 text-xl font-bold text-text-primary">{item.title}</h1>

        <div className="mb-6 text-sm text-text-secondary">
          <MarkdownRenderer content={item.description} />
        </div>

        {/* 메타정보 */}
        <div className="mb-6 grid grid-cols-1 gap-3 rounded-lg bg-bg-primary p-4 text-sm sm:grid-cols-2">
          <div>
            <span className="text-text-muted">{t("seller")}</span>
            <p className="font-medium text-text-primary">{item.seller_name}</p>
          </div>
          <div>
            <span className="text-text-muted">{t("downloads", { count: item.download_count })}</span>
          </div>
          <div>
            <span className="text-text-muted">{t("sellerEarns")}</span>
          </div>
          <div>
            <span className="text-text-muted">{t("serverReturns")}</span>
          </div>
        </div>

        {/* 포인트 잔액 */}
        {myPoints !== null && (
          <div className="mb-4 text-sm text-text-secondary">
            {t("myPoints")}: <span className="font-semibold text-accent-warning">{myPoints.toLocaleString()}P</span>
          </div>
        )}

        {/* 구매 버튼 */}
        {user && !isOwner && !purchased && settlement !== null && settlement.status !== "processing" && (
          <button
            onClick={() => setShowConfirm(true)}
            disabled={purchasing}
            className="w-full rounded-lg bg-accent-primary py-3 text-sm font-semibold text-white transition-colors hover:bg-accent-primary-hover disabled:opacity-50"
          >
            {t("buy")} — {item.price.toLocaleString()}P
          </button>
        )}

        {user && !isOwner && !purchased && (settlement === null || settlement.status === "processing") && (
          <div className="space-y-3 rounded-lg border border-accent-warning/30 bg-accent-warning/10 p-4" role="status" aria-live="polite">
            <p className="text-sm text-text-secondary">{settlement?.status === "processing" ? t(settlement.retryable ? "purchaseProcessing" : "purchaseReviewRequired") : t("purchaseChecking")}</p>
            <div className="flex flex-wrap gap-3">
              <button type="button" onClick={() => void refreshStatus()} disabled={purchasing} className="text-sm font-medium text-accent-primary hover:underline disabled:opacity-50">{t("purchaseCheckStatus")}</button>
              {settlement?.retryable && <button type="button" onClick={() => void handlePurchase()} disabled={purchasing} className="text-sm font-medium text-accent-primary hover:underline disabled:opacity-50">{purchasing ? tc("loading") : t("purchaseRetry")}</button>}
            </div>
          </div>
        )}
        {purchaseError && <p role="alert" className="mt-3 text-sm text-accent-error">{purchaseError}</p>}
        {!purchaseError && settlement?.status === "rejected" && <p role="alert" className="mt-3 text-sm text-accent-error">{t(settlement.failureCode === "insufficient_points" ? "insufficientPoints" : "purchaseFailed")}</p>}

        {purchased && (
          <div className="space-y-3">
            <div className="rounded-lg border border-accent-success/30 bg-accent-success/10 py-3 text-center text-sm font-medium text-accent-success">
              {t("purchaseSuccess")}
            </div>
            {item.original_filename && (
              <button
                onClick={handleDownload}
                className="flex min-w-0 w-full items-center justify-center gap-2 rounded-lg bg-accent-primary px-3 py-3 text-sm font-semibold text-white transition-colors hover:bg-accent-primary-hover"
              >
                <Download className="h-4 w-4 shrink-0" aria-hidden="true" />
                <span className="min-w-0 [overflow-wrap:anywhere]">{t("downloadFile")} — {item.original_filename}</span>
              </button>
            )}
          </div>
        )}

        {isOwner && (
          <div className="space-y-3">
            <div className="rounded-lg border border-border bg-bg-elevated py-3 text-center text-sm text-text-muted">
              {t("cannotBuyOwn")}
            </div>
            {item.original_filename && (
              <button
                onClick={handleDownload}
                className="flex min-w-0 w-full items-center justify-center gap-2 rounded-lg border border-border px-3 py-3 text-sm font-medium text-text-secondary transition-colors hover:bg-bg-elevated"
              >
                <Download className="h-4 w-4 shrink-0" aria-hidden="true" />
                <span className="min-w-0 [overflow-wrap:anywhere]">{t("downloadFile")} — {item.original_filename}</span>
              </button>
            )}
          </div>
        )}

        {!user && (
          <Link
            href="/login"
            className="block rounded-lg bg-accent-primary py-3 text-center text-sm font-semibold text-white transition-colors hover:bg-accent-primary-hover"
          >
            {t("loginToBuy")}
          </Link>
        )}
      </div>

      {/* 구매 확인 모달 */}
      {showConfirm && (
        <>
          <div
            className="fixed inset-0 z-50 bg-black/50 backdrop-blur-sm"
            onClick={() => setShowConfirm(false)}
          />
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="purchase-confirm-title"
            className="fixed left-1/2 top-1/2 z-50 w-[90vw] max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-xl border border-border bg-bg-surface p-6 shadow-2xl"
          >
            <h3 id="purchase-confirm-title" className="mb-2 text-lg font-bold text-text-primary">
              {t("purchaseConfirm")}
            </h3>
            <p className="mb-4 text-sm text-text-secondary">
              {t("purchaseConfirmMsg", { price: item.price.toLocaleString() })}
            </p>
            <div className="flex gap-3">
              <button
                onClick={() => setShowConfirm(false)}
                className="flex-1 rounded-lg border border-border py-2 text-sm font-medium text-text-secondary hover:bg-bg-elevated"
              >
                {tc("cancel")}
              </button>
              <button
                onClick={handlePurchase}
                disabled={purchasing}
                className="flex-1 rounded-lg bg-accent-primary py-2 text-sm font-semibold text-white transition-colors hover:bg-accent-primary-hover disabled:opacity-50"
              >
                {purchasing ? tc("loading") : tc("confirm")}
              </button>
            </div>
          </div>
        </>
      )}

      {/* 삭제 확인 모달 (3단계) */}
      <DeleteConfirmModal
        isOpen={deleteStep > 0}
        step={deleteStep}
        targetName={
          deleteTarget?.type === "item"
            ? item.title
            : richContentToPlainText(comments.find((c) => c.id === deleteTarget?.id)?.content ?? "", 50)
        }
        messages={{
          title: tCom("confirmDeleteTitle"),
          step1: tCom("confirmDeleteStep1"),
          step2: tCom("confirmDeleteStep2"),
          step3: tCom("confirmDeleteStep3"),
          cancel: tCom("cancel"),
          confirm: tCom("confirm"),
          deleting: tCom("deleting"),
        }}
        onConfirm={handleDeleteConfirm}
        onCancel={cancelDelete}
        loading={deleteLoading}
      />

      <ModerationActionDialog
        open={moderationTarget !== null}
        target={moderationTarget}
        action="hide"
        labels={{
          title: ta("moderationHideTitle"),
          restoreTitle: ta("moderationRestoreTitle"),
          resourceLabel: ta("moderationResourceLabel"),
          impactTitle: ta("moderationImpactTitle"),
          impactLoading: ta("moderationImpactLoading"),
          impactEmpty: ta("moderationImpactEmpty"),
          reasonLabel: ta("moderationReasonLabel"),
          reasonPlaceholder: ta("moderationReasonPlaceholder"),
          targetNameLabel: ta("moderationTargetNameLabel"),
          targetNameHelp: ta("moderationTargetNameHelp"),
          next: ta("deletionNext"),
          cancel: tc("cancel"),
          execute: ta("moderationHideExecute"),
          restore: ta("moderationRestoreExecute"),
          executing: ta("moderationExecuting"),
          stageLabel: ta("deletionStage"),
          scopeLabel: ta("moderationScopeLabel"),
          deleted: t("deletedComment"),
        }}
        impactLabels={{ comments: ta("moderationImpactComments") }}
        onClose={() => setModerationTarget(null)}
        onCompleted={handleModerationCompleted}
      />

      {/* 댓글 섹션 */}
      <div className="mt-8 rounded-xl border border-border bg-bg-surface p-6">
        <h2 className="mb-4 text-lg font-semibold text-text-primary">
          {t("commentCount", { count: comments.length })}
        </h2>

        {comments.length === 0 ? (
          <p className="py-4 text-center text-sm text-text-muted">{t("noComments")}</p>
        ) : (
          <div className="space-y-3">
            {comments.map((comment) => (
              <div
                key={comment.id}
                className="rounded-lg border border-border bg-bg-primary p-4"
              >
                {comment.is_deleted ? (
                  /* 삭제된 댓글 */
                  <p className="text-sm italic text-text-muted">{t("deletedComment")}</p>
                ) : (
                  <>
                    <div className="mb-2 flex items-center justify-between">
                      <div className="flex items-center gap-2 text-sm text-text-muted">
                        <span className="font-medium text-text-primary">
                          {comment.is_anonymous ? t("anonymous") : comment.author_name}
                        </span>
                        <span>
                          {new Date(comment.created_at).toLocaleDateString()}
                        </span>
                        {comment.updated_at && (
                          <span className="text-xs text-text-muted">{t("edited")}</span>
                        )}
                      </div>
                      <div className="flex items-center gap-2">
                        {/* 본인 댓글: 수정 + 삭제 */}
                        {user && user.id === comment.author_id && (
                          <>
                            <button
                              onClick={() => startEditComment(comment)}
                              className="text-xs text-text-muted hover:text-text-primary"
                            >
                              {t("editComment")}
                            </button>
                            <button
                              onClick={() => requestContentRemoval("comment", comment.id)}
                              className="text-xs text-accent-error hover:text-accent-error/80"
                            >
                              {isAdmin ? ta("moderationHideTitle") : t("deleteComment")}
                            </button>
                          </>
                        )}
                        {/* 관리자: 삭제만 (본인 댓글이 아닌 경우) */}
                        {isAdmin && user && user.id !== comment.author_id && (
                          <button
                            onClick={() => requestContentRemoval("comment", comment.id)}
                            className="text-xs text-accent-error hover:text-accent-error/80"
                          >
                            {ta("moderationHideTitle")}
                          </button>
                        )}
                        {/* 신고 버튼 (타인 댓글, 비관리자 or 관리자 모두) */}
                        {user && user.id !== comment.author_id && (
                          <ReportButton targetType="marketplace_comment" targetId={comment.id} />
                        )}
                      </div>
                    </div>

                    {/* 댓글 내용 또는 수정 폼 */}
                    {editingCommentId === comment.id ? (
                      <div className="space-y-2">
                        <RichTextEditor
                          value={editCommentContent}
                          onChange={setEditCommentContent}
                          placeholder={t("commentPlaceholder")}
                        />
                        <div className="flex justify-end gap-2">
                          <button
                            onClick={() => {
                              setEditingCommentId(null);
                              setEditCommentContent("");
                            }}
                            className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-text-secondary hover:bg-bg-elevated"
                          >
                            {tCom("cancel")}
                          </button>
                          <button
                            onClick={saveCommentEdit}
                            disabled={isRichContentEmpty(editCommentContent) || editCommentSaving}
                            className="rounded-lg bg-accent-primary px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-accent-primary-hover disabled:opacity-50"
                          >
                            {editCommentSaving ? t("saving") : t("save")}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <MarkdownRenderer content={comment.content} />
                    )}
                  </>
                )}
              </div>
            ))}
          </div>
        )}

        {/* 댓글 작성 폼 */}
        {user && (
          <div className="mt-4 border-t border-border pt-4">
            <RichTextEditor
              value={newComment}
              onChange={setNewComment}
              placeholder={t("commentPlaceholder")}
            />
            <div className="mt-2 flex items-center justify-between">
              <label className="flex cursor-pointer items-center gap-2 text-sm text-text-secondary">
                <input
                  type="checkbox"
                  checked={commentAnonymous}
                  onChange={(e) => setCommentAnonymous(e.target.checked)}
                  className="h-4 w-4 rounded border-border accent-accent-primary"
                />
                {t("postAsAnonymous")}
              </label>
              <button
                onClick={submitComment}
                disabled={isRichContentEmpty(newComment) || commentLoading}
                className="rounded-lg bg-accent-primary px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent-primary-hover disabled:opacity-50"
              >
                {commentLoading ? t("commentWriting") : t("commentSubmit")}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
