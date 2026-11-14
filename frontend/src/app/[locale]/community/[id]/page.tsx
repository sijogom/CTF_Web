"use client";

import { useState, useEffect, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { Button } from "@/components/ui/Button";
import { Link } from "@/i18n/navigation";
import { useTranslations, useLocale } from "next-intl";
import { apiFetch, ApiError } from "@/lib/api-client";
import { isRichContentEmpty } from "@/lib/rich-content.mjs";
import { useToast } from "@/providers/ToastProvider";
import { useAuth } from "@/providers/AuthProvider";
import { isAdminRole } from "@/lib/roles";
import { ReportButton } from "@/components/community/ReportButton";
import { DeleteConfirmModal } from "@/components/ui/DeleteConfirmModal";
import {
  ModerationActionDialog,
  type ModerationTarget,
} from "@/components/admin/ModerationActionDialog";

interface Post {
  id: string;
  title: string;
  content: string;
  board_type: string;
  author_id: string;
  author_name: string;
  language: string;
  like_count: number;
  dislike_count: number;
  comment_count: number;
  view_count: number;
  is_pinned: boolean;
  is_anonymous: boolean;
  created_at: string;
  updated_at: string | null;
  liked: boolean;
  disliked: boolean;
  bookmarked: boolean;
}

interface Comment {
  id: string;
  author_id: string | null;
  author_name: string;
  content: string;
  parent_id: string | null;
  is_anonymous: boolean;
  is_deleted: boolean;
  created_at: string;
  updated_at: string | null;
}

export default function PostDetailPage() {
  const params = useParams();
  const router = useRouter();
  const t = useTranslations("community");
  const tc = useTranslations("common");
  const ta = useTranslations("admin");
  const locale = useLocale();
  const { toast } = useToast();
  const postId = params.id as string;

  const { user } = useAuth();
  const canPin = isAdminRole(user?.role);

  const [post, setPost] = useState<Post | null>(null);
  const [comments, setComments] = useState<Comment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [liked, setLiked] = useState(false);
  const [likeCount, setLikeCount] = useState(0);
  const [disliked, setDisliked] = useState(false);
  const [dislikeCount, setDislikeCount] = useState(0);
  const [bookmarked, setBookmarked] = useState(false);
  const [newComment, setNewComment] = useState("");
  const [commentAnonymous, setCommentAnonymous] = useState(false);
  const [commentLoading, setCommentLoading] = useState(false);
  const [likeLoading, setLikeLoading] = useState(false);
  const [dislikeLoading, setDislikeLoading] = useState(false);
  const [bookmarkLoading, setBookmarkLoading] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [pinLoading, setPinLoading] = useState(false);

  // 게시글 수정
  const [editingPost, setEditingPost] = useState(false);
  const [editTitle, setEditTitle] = useState("");
  const [editContent, setEditContent] = useState("");
  const [editSaving, setEditSaving] = useState(false);

  // 삭제 (3단계 확인)
  const [deleteStep, setDeleteStep] = useState(0);
  const [deleteTarget, setDeleteTarget] = useState<{ type: "post" | "comment"; id: string } | null>(null);
  const [deleteLoading, setDeleteLoading] = useState(false);
  const [moderationTarget, setModerationTarget] = useState<ModerationTarget | null>(null);

  // 댓글 수정
  const [editingCommentId, setEditingCommentId] = useState<string | null>(null);
  const [editCommentContent, setEditCommentContent] = useState("");
  const [editCommentSaving, setEditCommentSaving] = useState(false);

  const fetchPost = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await apiFetch<Post & { comments?: Comment[] }>(
        `/api/community/posts/${postId}?lang=${locale}`,
      );
      setPost(data);
      setLiked(data.liked);
      setLikeCount(data.like_count);
      setDisliked(data.disliked);
      setDislikeCount(data.dislike_count);
      setBookmarked(data.bookmarked);
      setPinned(data.is_pinned);
      if (data.comments) {
        setComments(data.comments);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [postId, locale, t]);

  useEffect(() => {
    fetchPost();
  }, [fetchPost]);

  const toggleLike = async () => {
    if (likeLoading) return;
    setLikeLoading(true);

    // 낙관적 업데이트
    const prevLiked = liked;
    const prevCount = likeCount;
    const prevDisliked = disliked;
    const prevDislikeCount = dislikeCount;
    setLiked(!liked);
    setLikeCount((c) => (liked ? c - 1 : c + 1));
    // 좋아요 누를 때 비추천 해제 (상호배제)
    if (!liked && disliked) {
      setDisliked(false);
      setDislikeCount((c) => Math.max(0, c - 1));
    }

    try {
      const res = await apiFetch<{ liked: boolean; like_count: number; dislike_count: number }>(
        `/api/community/posts/${postId}/like`, { method: "POST" },
      );
      setLiked(res.liked);
      setLikeCount(res.like_count);
      setDislikeCount(res.dislike_count);
      if (res.liked) setDisliked(false);
    } catch {
      setLiked(prevLiked);
      setLikeCount(prevCount);
      setDisliked(prevDisliked);
      setDislikeCount(prevDislikeCount);
    } finally {
      setLikeLoading(false);
    }
  };

  const toggleDislike = async () => {
    if (dislikeLoading) return;
    setDislikeLoading(true);

    const prevDisliked = disliked;
    const prevDislikeCount = dislikeCount;
    const prevLiked = liked;
    const prevLikeCount = likeCount;
    setDisliked(!disliked);
    setDislikeCount((c) => (disliked ? c - 1 : c + 1));
    // 비추천 누를 때 좋아요 해제 (상호배제)
    if (!disliked && liked) {
      setLiked(false);
      setLikeCount((c) => Math.max(0, c - 1));
    }

    try {
      const res = await apiFetch<{ disliked: boolean; like_count: number; dislike_count: number }>(
        `/api/community/posts/${postId}/dislike`, { method: "POST" },
      );
      setDisliked(res.disliked);
      setLikeCount(res.like_count);
      setDislikeCount(res.dislike_count);
      if (res.disliked) setLiked(false);
    } catch {
      setDisliked(prevDisliked);
      setDislikeCount(prevDislikeCount);
      setLiked(prevLiked);
      setLikeCount(prevLikeCount);
    } finally {
      setDislikeLoading(false);
    }
  };

  const toggleBookmark = async () => {
    if (bookmarkLoading) return;
    setBookmarkLoading(true);

    const prevBookmarked = bookmarked;
    setBookmarked(!bookmarked);

    try {
      await apiFetch(`/api/community/posts/${postId}/bookmark`, { method: "POST" });
    } catch {
      setBookmarked(prevBookmarked);
    } finally {
      setBookmarkLoading(false);
    }
  };

  const togglePin = async () => {
    if (pinLoading) return;
    setPinLoading(true);
    const prev = pinned;
    setPinned(!pinned);
    try {
      const res = await apiFetch<{ is_pinned: boolean }>(
        `/api/community/posts/${postId}/pin`, { method: "PATCH" },
      );
      setPinned(res.is_pinned);
      toast("success", res.is_pinned ? t("pinned") : t("unpinned"));
    } catch {
      setPinned(prev);
      toast("error", t("pinFailed"));
    } finally {
      setPinLoading(false);
    }
  };

  const submitComment = async () => {
    if (isRichContentEmpty(newComment) || commentLoading) return;
    setCommentLoading(true);

    try {
      const created = await apiFetch<Comment>(
        `/api/community/posts/${postId}/comments`,
        { method: "POST", body: { content: newComment, is_anonymous: commentAnonymous } },
      );
      setComments((prev) => [...prev, created]);
      setNewComment("");
    } catch (err) {
      const message = err instanceof ApiError ? err.message : t("commentFailed");
      toast("error", message);
    } finally {
      setCommentLoading(false);
    }
  };

  // --- 게시글 수정 ---
  const startEditPost = () => {
    if (!post) return;
    setEditTitle(post.title);
    setEditContent(post.content);
    setEditingPost(true);
  };

  const savePostEdit = async () => {
    if (editSaving || !post || !editTitle.trim() || isRichContentEmpty(editContent)) return;
    setEditSaving(true);
    try {
      await apiFetch(`/api/community/posts/${postId}`, {
        method: "PATCH",
        body: { title: editTitle, content: editContent },
      });
      toast("success", t("postUpdated"));
      setEditingPost(false);
      fetchPost();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("errorOccurred"));
    } finally {
      setEditSaving(false);
    }
  };

  // --- 삭제 (3단계 확인) ---
  const startDelete = (type: "post" | "comment", id: string) => {
    setDeleteTarget({ type, id });
    setDeleteStep(1);
  };

  const requestContentRemoval = (type: "post" | "comment", id: string) => {
    if (canPin) {
      setModerationTarget({ type, id });
      return;
    }
    startDelete(type, id);
  };

  const handleModerationCompleted = async () => {
    const target = moderationTarget;
    if (!target) return;
    setModerationTarget(null);
    toast("success", ta("moderationHideSuccess"));
    if (target.type === "post") {
      router.push("/community");
      return;
    }
    await fetchPost();
  };

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;
    if (deleteStep < 3) {
      setDeleteStep(deleteStep + 1);
      return;
    }
    // step 3에서 실제 삭제
    setDeleteLoading(true);
    try {
      if (deleteTarget.type === "post") {
        await apiFetch(`/api/community/posts/${postId}`, { method: "DELETE" });
        toast("success", t("postDeleted"));
        router.push("/community");
      } else {
        await apiFetch(`/api/community/posts/${postId}/comments/${deleteTarget.id}`, { method: "DELETE" });
        toast("success", t("commentDeleted"));
        fetchPost();
      }
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("errorOccurred"));
    } finally {
      setDeleteLoading(false);
      setDeleteStep(0);
      setDeleteTarget(null);
    }
  };

  const cancelDelete = () => {
    setDeleteStep(0);
    setDeleteTarget(null);
  };

  // --- 댓글 수정 ---
  const startEditComment = (comment: Comment) => {
    setEditingCommentId(comment.id);
    setEditCommentContent(comment.content);
  };

  const saveCommentEdit = async () => {
    if (editCommentSaving || !editingCommentId || isRichContentEmpty(editCommentContent)) return;
    setEditCommentSaving(true);
    try {
      await apiFetch(`/api/community/posts/${postId}/comments/${editingCommentId}`, {
        method: "PATCH",
        body: { content: editCommentContent },
      });
      toast("success", t("commentUpdated"));
      setEditingCommentId(null);
      fetchPost();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("errorOccurred"));
    } finally {
      setEditCommentSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="mx-auto max-w-4xl space-y-6">
        <div className="animate-pulse space-y-4">
          <div className="h-6 w-32 rounded bg-bg-surface" />
          <div className="h-8 w-3/4 rounded bg-bg-surface" />
          <div className="h-64 rounded-xl bg-bg-surface" />
        </div>
      </div>
    );
  }

  if (error || !post) {
    return (
      <div className="mx-auto max-w-4xl">
        <div className="rounded-xl border border-accent-error/30 bg-accent-error/5 p-6 text-center">
          <p className="text-sm text-accent-error">{error ?? t("postNotFound")}</p>
          <Button variant="secondary" className="mt-4" onClick={fetchPost}>
            {tc("retry")}
          </Button>
        </div>
      </div>
    );
  }

  const date = new Date(post.created_at);
  const localeMap: Record<string, string> = { ko: "ko-KR", en: "en-US", ja: "ja-JP", zh: "zh-CN" };
  const dateLocale = localeMap[locale] || "ko-KR";

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      {/* 헤더 */}
      <div>
        <Link href="/community" className="text-sm text-text-muted hover:text-text-primary">
          {`← ${t("backToBoard")}`}
        </Link>
        <h1 className="mt-3 text-2xl font-bold">{post.title}</h1>
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-text-muted">
          <span>{post.is_anonymous ? t("anonymous") : post.author_name}</span>
          <span>
            {date.toLocaleDateString(dateLocale)} {date.toLocaleTimeString(dateLocale, { hour: "2-digit", minute: "2-digit" })}
            {post.updated_at && <span className="ml-1 text-text-muted">{t("edited")}</span>}
          </span>
          <span>{t("views", { count: post.view_count })}</span>
        </div>
      </div>

      {/* 본문 */}
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        {editingPost ? (
          <div className="space-y-4">
            <div>
              <label className="mb-1 block text-sm font-medium">{t("titleLabel")}</label>
              <input
                type="text"
                value={editTitle}
                onChange={(e) => setEditTitle(e.target.value)}
                className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2 text-sm focus:border-accent-primary focus:outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium">{t("contentLabel")}</label>
              <RichTextEditor
                value={editContent}
                onChange={setEditContent}
                placeholder={t("contentPlaceholder")}
              />
            </div>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setEditingPost(false)}
                className="rounded-lg border border-border px-4 py-2 text-sm hover:bg-bg-primary"
              >
                {t("cancel")}
              </button>
              <button
                onClick={savePostEdit}
                disabled={editSaving || !editTitle.trim() || !editContent.trim()}
                className="rounded-lg bg-accent-primary px-4 py-2 text-sm font-medium text-white hover:bg-accent-primary/90 disabled:opacity-50"
              >
                {editSaving ? t("saving") : t("saveChanges")}
              </button>
            </div>
          </div>
        ) : (
          <MarkdownRenderer content={post.content} />
        )}
      </div>

      {/* 액션 버튼 */}
      <div className="flex items-center gap-3">
        <button
          onClick={toggleLike}
          disabled={likeLoading}
          className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
            liked ? "bg-accent-error/10 text-accent-error" : "bg-bg-surface text-text-muted hover:text-text-primary"
          }`}
        >
          ▲ {likeCount}
        </button>
        <button
          onClick={toggleDislike}
          disabled={dislikeLoading}
          className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
            disliked ? "bg-blue-500/10 text-blue-500" : "bg-bg-surface text-text-muted hover:text-text-primary"
          }`}
        >
          ▼ {dislikeCount}
        </button>
        <button
          onClick={toggleBookmark}
          disabled={bookmarkLoading}
          className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
            bookmarked ? "bg-accent-primary/10 text-accent-primary" : "bg-bg-surface text-text-muted hover:text-text-primary"
          }`}
        >
          {bookmarked ? `🔖 ${t("bookmarked")}` : `🔖 ${t("bookmark")}`}
        </button>
        {/* 본인 게시글: 수정/삭제 */}
        {user && user.id === post.author_id && (
          <>
            <button
              onClick={startEditPost}
              className="rounded-lg bg-bg-surface px-3 py-1.5 text-sm font-medium text-text-muted transition-colors hover:text-text-primary"
            >
              {t("editPost")}
            </button>
            <button
              onClick={() => requestContentRemoval("post", postId)}
              className="rounded-lg bg-bg-surface px-3 py-1.5 text-sm font-medium text-accent-error/70 transition-colors hover:text-accent-error"
            >
              {canPin ? ta("moderationHideTitle") : t("deletePost")}
            </button>
          </>
        )}
        {canPin && (
          <>
            <button
              onClick={togglePin}
              disabled={pinLoading}
              className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                pinned ? "bg-accent-warning/10 text-accent-warning" : "bg-bg-surface text-text-muted hover:text-text-primary"
              }`}
            >
              {pinned ? `📌 ${t("unpinPost")}` : `📌 ${t("pinPost")}`}
            </button>
            {/* 관리자: 타인 게시글도 삭제 가능 */}
            {user && user.id !== post.author_id && (
              <button
                onClick={() => requestContentRemoval("post", postId)}
                className="rounded-lg bg-bg-surface px-3 py-1.5 text-sm font-medium text-accent-error/70 transition-colors hover:text-accent-error"
              >
                {ta("moderationHideTitle")}
              </button>
            )}
          </>
        )}
        {user && user.id !== post.author_id && (
          <ReportButton targetType="post" targetId={post.id} />
        )}
      </div>

      {/* 댓글 */}
      <div className="space-y-4">
        <h2 className="text-lg font-semibold">{t("commentCount", { count: comments.length })}</h2>

        {comments.map((comment) => (
          <div
            key={comment.id}
            className={`rounded-lg border border-border bg-bg-surface p-4 ${
              comment.parent_id ? "ml-8" : ""
            }`}
          >
            {comment.is_deleted ? (
              <p className="text-sm italic text-text-muted">{t("deletedComment")}</p>
            ) : (
              <>
                <div className="mb-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
                  <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm text-text-muted">
                    <span className="min-w-0 max-w-full font-medium text-text-primary">
                      {comment.is_anonymous ? t("anonymous") : comment.author_name}
                    </span>
                    <span className="whitespace-nowrap">
                      {new Date(comment.created_at).toLocaleDateString(dateLocale)}
                      {comment.updated_at && <span className="ml-1">{t("edited")}</span>}
                    </span>
                  </div>
                  <div className="ml-auto flex shrink-0 flex-wrap items-center gap-2">
                    {/* 본인 댓글: 수정/삭제 */}
                    {user && user.id === comment.author_id && (
                      <>
                        <button
                          onClick={() => startEditComment(comment)}
                          className="shrink-0 whitespace-nowrap text-sm text-text-muted transition-colors hover:text-text-primary"
                        >
                          {t("editComment")}
                        </button>
                        <button
                          onClick={() => requestContentRemoval("comment", comment.id)}
                          className="shrink-0 whitespace-nowrap text-sm text-accent-error/70 transition-colors hover:text-accent-error"
                        >
                          {canPin ? ta("moderationHideTitle") : t("deleteComment")}
                        </button>
                      </>
                    )}
                    {/* 관리자: 타인 댓글 삭제 */}
                    {canPin && user && user.id !== comment.author_id && (
                      <button
                        onClick={() => requestContentRemoval("comment", comment.id)}
                        className="shrink-0 whitespace-nowrap text-sm text-accent-error/70 transition-colors hover:text-accent-error"
                      >
                        {ta("moderationHideTitle")}
                      </button>
                    )}
                    {/* 타인 댓글 신고 */}
                    {user && user.id !== comment.author_id && (
                      <ReportButton targetType="comment" targetId={comment.id} />
                    )}
                  </div>
                </div>
                {editingCommentId === comment.id ? (
                  <div className="space-y-2">
                    <RichTextEditor
                      value={editCommentContent}
                      onChange={setEditCommentContent}
                      placeholder={t("commentPlaceholder")}
                    />
                    <div className="flex justify-end gap-2">
                      <button
                        onClick={() => setEditingCommentId(null)}
                        className="rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-bg-primary"
                      >
                        {t("cancel")}
                      </button>
                      <button
                        onClick={saveCommentEdit}
                        disabled={editCommentSaving || isRichContentEmpty(editCommentContent)}
                        className="rounded-lg bg-accent-primary px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-primary/90 disabled:opacity-50"
                      >
                        {editCommentSaving ? t("saving") : t("saveChanges")}
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

        {/* 댓글 작성 */}
        <div className="rounded-lg border border-border bg-bg-surface p-4">
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
            <Button
              onClick={submitComment}
              size="sm"
              disabled={isRichContentEmpty(newComment) || commentLoading}
            >
              {commentLoading ? t("commentWriting") : t("commentSubmit")}
            </Button>
          </div>
        </div>
      </div>

      {/* 삭제 확인 모달 */}
      <DeleteConfirmModal
        isOpen={deleteStep > 0}
        step={deleteStep}
        targetName={deleteTarget?.type === "post" ? (post?.title ?? "") : t("comments")}
        messages={{
          title: t("confirmDeleteTitle"),
          step1: t("confirmDeleteStep1"),
          step2: t("confirmDeleteStep2"),
          step3: t("confirmDeleteStep3"),
          cancel: t("cancel"),
          confirm: t("confirm"),
          deleting: t("deleting"),
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
    </div>
  );
}
