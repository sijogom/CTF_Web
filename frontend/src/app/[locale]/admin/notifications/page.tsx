"use client";

import { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import { useAuth } from "@/providers/AuthProvider";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { isRichContentEmpty, richContentToPlainText } from "@/lib/rich-content.mjs";

interface NotificationItem {
  id: string;
  title: string;
  content: string;
  type: string;
  created_at: string;
}

interface NotificationListResponse {
  items: NotificationItem[];
  total: number;
  page: number;
  size: number;
}

export default function AdminNotificationsPage() {
  const t = useTranslations("admin");
  const tn = useTranslations("notification");
  const tc = useTranslations("common");
  const { toast } = useToast();
  const { user } = useAuth();
  const canDelete = user?.role === "admin";
  const [deleting, setDeleting] = useState<string | null>(null);

  const [items, setItems] = useState<NotificationItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(true);

  /* 생성 폼 */
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [type, setType] = useState<"toast" | "popup" | "alert">("toast");
  const [creating, setCreating] = useState(false);

  const pageSize = 20;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  const fetchList = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiFetch<NotificationListResponse>(
        `/api/auth/admin/notifications?page=${page}&size=${pageSize}`,
      );
      setItems(data.items);
      setTotal(data.total);
    } catch {
      toast("error", tn("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [page, toast, tn]);

  useEffect(() => {
    void refreshKey;
    fetchList();
  }, [fetchList, refreshKey]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    setCreating(true);
    try {
      await apiFetch("/api/auth/admin/notifications", {
        method: "POST",
        body: { title: title.trim(), content: isRichContentEmpty(content) ? "" : content, type },
      });
      toast("success", tn("createSuccess"));
      setTitle("");
      setContent("");
      setType("toast");
      setPage(1);
      // page가 이미 1이어도 refreshKey 변경으로 useEffect가 재트리거됨
      setRefreshKey((k) => k + 1);
    } catch {
      toast("error", tn("createFailed"));
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (!canDelete || deleting) return;
    setDeleting(id);
    try {
      await apiFetch(`/api/auth/admin/notifications/${id}`, {
        method: "DELETE",
      });
      toast("success", tn("deleteSuccess"));
      setRefreshKey((k) => k + 1);
    } catch {
      toast("error", tn("deleteFailed"));
    } finally {
      setDeleting(null);
    }
  };

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-bold text-text-primary">
        {t("notificationManagement")}
      </h1>

      {/* 알림 생성 폼 */}
      <form
        onSubmit={handleCreate}
        className="rounded-lg border border-border bg-bg-surface p-5 space-y-4"
      >
        <h2 className="text-sm font-semibold text-text-primary">
          {tn("createNotification")}
        </h2>

        <div>
          <label className="mb-1 block text-sm text-text-secondary">
            {tn("titleLabel")}
          </label>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={200}
            required
            className="w-full rounded-md border border-border bg-bg-primary px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-primary focus:outline-none"
            placeholder={tn("titlePlaceholder")}
          />
        </div>

        <div>
          <label className="mb-1 block text-sm text-text-secondary">
            {tn("contentLabel")}
          </label>
          <RichTextEditor
            value={content}
            onChange={setContent}
            placeholder={tn("contentPlaceholder")}
            ariaLabel={tn("contentLabel")}
          />
        </div>

        <div>
          <label className="mb-1 block text-sm text-text-secondary">
            {tn("typeLabel")}
          </label>
          <div className="flex gap-3">
            {(["toast", "popup", "alert"] as const).map((t_) => (
              <label key={t_} className="flex items-center gap-1.5 text-sm text-text-secondary">
                <input
                  type="radio"
                  name="type"
                  value={t_}
                  checked={type === t_}
                  onChange={() => setType(t_)}
                  className="accent-accent-primary"
                />
                {t_ === "toast" ? tn("typeToast") : t_ === "popup" ? tn("typePopup") : tn("typeAlert")}
              </label>
            ))}
          </div>
        </div>

        <button
          type="submit"
          disabled={creating || !title.trim()}
          className="rounded-md bg-accent-primary px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-accent-primary-hover disabled:opacity-50"
        >
          {creating ? tn("creating") : tn("createBtn")}
        </button>
      </form>

      {/* 알림 목록 */}
      <div className="rounded-lg border border-border bg-bg-surface">
        <div className="border-b border-border px-5 py-3">
          <h2 className="text-sm font-semibold text-text-primary">
            {tn("listTitle")} ({total})
          </h2>
        </div>

        {loading ? (
          <div className="px-5 py-8 text-center text-sm text-text-muted">
            {t("loadingData")}
          </div>
        ) : items.length === 0 ? (
          <div className="px-5 py-8 text-center text-sm text-text-muted">
            {tn("noNotifications")}
          </div>
        ) : (
          <div className="divide-y divide-border">
            {items.map((n) => (
              <div
                key={n.id}
                className="flex items-start justify-between gap-4 px-5 py-3"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <p className="min-w-0 break-all text-sm font-medium text-text-primary">
                      {n.title}
                    </p>
                    <span className="shrink-0 rounded bg-bg-elevated px-1.5 py-0.5 text-[10px] font-medium text-text-muted">
                      {n.type}
                    </span>
                  </div>
                  {n.content && (
                    <p className="mt-0.5 line-clamp-2 text-xs text-text-secondary">
                      {richContentToPlainText(n.content)}
                    </p>
                  )}
                  <p className="mt-0.5 text-[11px] text-text-muted">
                    {new Date(n.created_at).toLocaleString()}
                  </p>
                </div>
                {canDelete && <button
                  onClick={() => handleDelete(n.id)}
                  disabled={deleting !== null}
                  className="shrink-0 rounded px-2 py-1 text-xs text-accent-error transition-colors hover:bg-accent-error/10"
                >
                  {tc("delete")}
                </button>}
              </div>
            ))}
          </div>
        )}

        {/* 페이징 */}
        {totalPages > 1 && (
          <div className="flex items-center justify-center gap-2 border-t border-border px-5 py-3">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1}
              className="rounded px-3 py-1 text-sm text-text-secondary hover:bg-bg-elevated disabled:opacity-40"
            >
              {t("prev")}
            </button>
            <span className="text-xs text-text-muted">
              {t("page", { current: page, total: totalPages })}
            </span>
            <button
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
              className="rounded px-3 py-1 text-sm text-text-secondary hover:bg-bg-elevated disabled:opacity-40"
            >
              {t("next")}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
