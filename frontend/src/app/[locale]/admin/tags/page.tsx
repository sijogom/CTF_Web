"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import { KnowledgeTagDeleteDialog } from "@/components/admin/knowledge/KnowledgeTagDeleteDialog";
import { readTagDeletionPreview, type TagDeletionPreview } from "@/lib/knowledge-tag-deletion.mjs";
import type {
  KnowledgeTag,
  KnowledgeTagReconcileResponse,
  KnowledgeTagUnmappedContent,
} from "@/types/knowledge";

type UnmappedType = "all" | "challenge" | "course" | "lesson";

function managementHref(item: KnowledgeTagUnmappedContent): string {
  if (item.entity_type === "challenge") return `/admin/challenges/${item.entity_id}/edit`;
  if (item.entity_type === "course") return `/admin/courses/${item.entity_id}`;
  return item.course_id ? `/admin/courses/${item.course_id}` : "/admin/courses";
}

export default function TagManagementPage() {
  const t = useTranslations("admin");
  const tt = useTranslations("tagSelector");
  const td = useTranslations("knowledgeAdmin.tagDeletion");
  const { toast } = useToast();

  const [tags, setTags] = useState<KnowledgeTag[]>([]);
  const [loading, setLoading] = useState(true);
  const [unmapped, setUnmapped] = useState<KnowledgeTagUnmappedContent[]>([]);
  const [unmappedLoading, setUnmappedLoading] = useState(true);
  const [unmappedType, setUnmappedType] = useState<UnmappedType>("all");
  const [reconciling, setReconciling] = useState(false);
  const [tagsError, setTagsError] = useState(false);
  const [unmappedError, setUnmappedError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<KnowledgeTag | null>(null);
  const [deletePreview, setDeletePreview] = useState<TagDeletionPreview | null>(null);
  const [deletePreviewLoading, setDeletePreviewLoading] = useState(false);
  const [deletePreviewError, setDeletePreviewError] = useState<string | null>(null);
  const deleteTargetRef = useRef<KnowledgeTag | null>(null);
  const deletePreviewRequest = useRef<AbortController | null>(null);
  const deleteRequestBusy = useRef(false);
  const tagsRequest = useRef<AbortController | null>(null);
  const unmappedRequest = useRef<AbortController | null>(null);
  const mutationBusy = useRef(false);
  const tagHeadingRef = useRef<HTMLHeadingElement>(null);
  const deleteReturnFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => () => deletePreviewRequest.current?.abort(), []);

  // 폼 상태
  const [editingId, setEditingId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [color, setColor] = useState("#6366F1");
  const [description, setDescription] = useState("");
  const [parentId, setParentId] = useState<number | null>(null);

  const fetchTags = useCallback(async () => {
    tagsRequest.current?.abort();
    const controller = new AbortController();
    tagsRequest.current = controller;
    setLoading(true);
    setTagsError(false);
    try {
      const data = await apiFetch<KnowledgeTag[]>("/api/learn/knowledge-tags", { signal: controller.signal });
      if (controller.signal.aborted) return;
      setTags(data);
    } catch {
      if (!controller.signal.aborted) setTagsError(true);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTags();
    return () => tagsRequest.current?.abort();
  }, [fetchTags]);

  const fetchUnmapped = useCallback(async () => {
    unmappedRequest.current?.abort();
    const controller = new AbortController();
    unmappedRequest.current = controller;
    setUnmappedLoading(true);
    setUnmappedError(false);
    try {
      const query = unmappedType === "all" ? "" : `?entity_type=${unmappedType}`;
      const data = await apiFetch<KnowledgeTagUnmappedContent[]>(`/api/learn/knowledge-tags/unmapped${query}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      setUnmapped(data);
    } catch {
      if (!controller.signal.aborted) setUnmappedError(true);
    } finally {
      if (!controller.signal.aborted) setUnmappedLoading(false);
    }
  }, [unmappedType]);

  useEffect(() => {
    fetchUnmapped();
    return () => unmappedRequest.current?.abort();
  }, [fetchUnmapped]);

  const reconcileRuleMappings = async () => {
    if (mutationBusy.current) return;
    mutationBusy.current = true;
    setReconciling(true);
    try {
      const result = await apiFetch<KnowledgeTagReconcileResponse>(
        "/api/learn/knowledge-tags/reconcile",
        { method: "POST", body: {} },
      );
      toast(
        "success",
        `규칙 기반 자동 매핑 ${result.created}건을 추가했습니다. 이미 연결된 ${result.existing}건은 보존했습니다.`,
      );
      await Promise.all([fetchTags(), fetchUnmapped()]);
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : "규칙 기반 자동 매핑에 실패했습니다.");
    } finally {
      setReconciling(false);
      mutationBusy.current = false;
    }
  };

  const resetForm = () => {
    setEditingId(null);
    setName("");
    setSlug("");
    setColor("#6366F1");
    setDescription("");
    setParentId(null);
  };

  const handleSubmit = async () => {
    if (mutationBusy.current) return;
    if (!name.trim() || !slug.trim()) {
      toast("error", "이름과 slug는 필수입니다.");
      return;
    }

    mutationBusy.current = true;
    setSaving(true);
    try {
      if (editingId) {
        await apiFetch(`/api/learn/knowledge-tags/${editingId}`, {
          method: "PUT",
          body: { name, slug, color, description, parent_id: parentId },
        });
        toast("success", "태그가 수정되었습니다.");
      } else {
        await apiFetch("/api/learn/knowledge-tags", {
          method: "POST",
          body: { name, slug, color, description, parent_id: parentId },
        });
        toast("success", "태그가 생성되었습니다.");
      }
      resetForm();
      await Promise.all([fetchTags(), fetchUnmapped()]);
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : "태그 저장에 실패했습니다.");
    } finally {
      mutationBusy.current = false;
      setSaving(false);
    }
  };

  const handleEdit = (tag: KnowledgeTag) => {
    setEditingId(tag.id);
    setName(tag.name);
    setSlug(tag.slug);
    setColor(tag.color ?? "#6366F1");
    setDescription(tag.description ?? "");
    setParentId(tag.parent_id ?? null);
  };

  const closeDelete = () => {
    if (deleteRequestBusy.current) return;
    deletePreviewRequest.current?.abort();
    deleteTargetRef.current = null;
    setDeleteTarget(null);
    setDeletePreview(null);
    setDeletePreviewError(null);
    setDeletePreviewLoading(false);
    mutationBusy.current = false;
  };

  const loadDeletePreview = async (target: KnowledgeTag, priorError: string | null = null) => {
    deletePreviewRequest.current?.abort();
    const controller = new AbortController();
    deletePreviewRequest.current = controller;
    setDeletePreview(null);
    setDeletePreviewLoading(true);
    setDeletePreviewError(priorError);
    try {
      const data = await apiFetch<unknown>(`/api/learn/knowledge-tags/${target.id}/deletion-preview`, { signal: controller.signal });
      if (controller.signal.aborted || deleteTargetRef.current !== target) return;
      setDeletePreview(readTagDeletionPreview(data, target.id));
    } catch (err) {
      if (controller.signal.aborted || deleteTargetRef.current !== target) return;
      if (err instanceof ApiError && err.status === 404) {
        deleteReturnFocusRef.current = tagHeadingRef.current;
        closeDelete();
        toast("info", td("alreadyDeleted"));
        await Promise.all([fetchTags(), fetchUnmapped()]);
      } else {
        setDeletePreviewError(err instanceof ApiError ? err.message : td("failed"));
      }
    } finally {
      if (!controller.signal.aborted && deleteTargetRef.current === target) setDeletePreviewLoading(false);
    }
  };

  const handleDelete = (id: number, trigger: HTMLElement | null = null) => {
    if (mutationBusy.current) return;
    const target = tags.find((tag) => tag.id === id);
    if (!target) return;
    deleteReturnFocusRef.current = trigger ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    mutationBusy.current = true;
    deleteTargetRef.current = target;
    setDeleteTarget(target);
    void loadDeletePreview(target);
  };

  const confirmDelete = async () => {
    const target = deleteTargetRef.current;
    if (!target || deletePreview?.tag.id !== target.id || deleteRequestBusy.current || deletePreviewLoading) return;
    deleteRequestBusy.current = true;
    setDeletingId(target.id);
    try {
      await apiFetch(`/api/learn/knowledge-tags/${target.id}?confirmation=${encodeURIComponent(deletePreview.confirmation)}`, { method: "DELETE" });
      deleteRequestBusy.current = false;
      deleteReturnFocusRef.current = tagHeadingRef.current;
      closeDelete();
      toast("success", td("success"));
      if (editingId === target.id) resetForm();
      await Promise.all([fetchTags(), fetchUnmapped()]);
    } catch (err) {
      deleteRequestBusy.current = false;
      if (deleteTargetRef.current !== target) return;
      // An unknown response must be read back before another explicit attempt.
      await loadDeletePreview(target, err instanceof ApiError && err.status === 409
        ? td("changed") : err instanceof ApiError ? err.message : td("failed"));
    } finally {
      deleteRequestBusy.current = false;
      setDeletingId(null);
    }
  };

  const autoSlug = (value: string) => {
    setName(value);
    if (!editingId) {
      setSlug(
        value
          .toLowerCase()
          .replace(/[^a-z0-9가-힣\s-]/g, "")
          .replace(/\s+/g, "-")
          .replace(/-+/g, "-")
      );
    }
  };

  return (
    <div className="space-y-6">
      {deleteTarget && <KnowledgeTagDeleteDialog name={deleteTarget.name} preview={deletePreview}
        loading={deletePreviewLoading} deleting={deletingId !== null} error={deletePreviewError}
        returnFocusRef={deleteReturnFocusRef}
        onRetry={() => { void loadDeletePreview(deleteTarget); }} onConfirm={() => { void confirmDelete(); }} onCancel={closeDelete} />}
      <h1 ref={tagHeadingRef} tabIndex={-1} className="text-2xl font-bold">{t("tagManagement")}</h1>

      {/* 생성/수정 폼 */}
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <h2 className="mb-4 text-lg font-semibold">
          {editingId ? "태그 수정" : "새 태그 추가"}
        </h2>
        <fieldset disabled={saving || deletingId !== null || reconciling} className="grid gap-4 sm:grid-cols-2">
          <div>
            <label className="mb-1 block text-sm font-medium">이름</label>
            <input
              value={name}
              onChange={(e) => autoSlug(e.target.value)}
              placeholder="예: SQL Injection"
              className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium">Slug</label>
            <input
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="예: sql-injection"
              className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium">색상</label>
            <div className="flex items-center gap-2">
              <input
                type="color"
                value={color}
                onChange={(e) => setColor(e.target.value)}
                className="h-9 w-12 cursor-pointer rounded border border-border"
              />
              <input
                value={color}
                onChange={(e) => setColor(e.target.value)}
                className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
              />
            </div>
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium">설명</label>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="태그 설명 (선택)"
              className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium">부모 지식 태그</label>
            <select
              value={parentId ?? ""}
              onChange={(event) => setParentId(event.target.value ? Number(event.target.value) : null)}
              className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
            >
              <option value="">없음 (최상위 태그)</option>
              {tags
                .filter((tag) => tag.id !== editingId)
                .sort((left, right) => left.name.localeCompare(right.name))
                .map((tag) => (
                  <option key={tag.id} value={tag.id}>
                    {tag.name} ({tag.slug})
                  </option>
                ))}
            </select>
            <p className="mt-1 text-xs text-text-muted">순환 계층은 서버에서 거부됩니다.</p>
          </div>
        </fieldset>
        <div className="mt-4 flex gap-2">
          <button
            onClick={handleSubmit}
            disabled={saving || deletingId !== null || reconciling || loading || tagsError}
            className="rounded-lg bg-accent-primary px-4 py-2 text-sm font-medium text-white hover:bg-accent-primary/90"
          >
            {editingId ? "수정" : "추가"}
          </button>
          {editingId && (
            <button
              onClick={resetForm}
              disabled={saving || deletingId !== null}
              className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-text-muted hover:bg-bg-elevated"
            >
              취소
            </button>
          )}
        </div>
      </div>

      {/* 태그 목록 */}
      <div className="rounded-xl border border-border bg-bg-surface">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[56rem] table-fixed text-left text-sm">
            <colgroup>
              <col className="w-16" />
              <col className="w-[22%]" />
              <col className="w-[20%]" />
              <col className="w-[18%]" />
              <col />
              <col className="w-24" />
            </colgroup>
            <thead>
              <tr className="border-b border-border">
                <th className="whitespace-nowrap px-4 py-3 font-medium text-text-muted">색상</th>
                <th className="whitespace-nowrap px-4 py-3 font-medium text-text-muted">이름</th>
                <th className="whitespace-nowrap px-4 py-3 font-medium text-text-muted">Slug</th>
                <th className="whitespace-nowrap px-4 py-3 font-medium text-text-muted">부모</th>
                <th className="whitespace-nowrap px-4 py-3 font-medium text-text-muted">설명</th>
                <th className="whitespace-nowrap px-4 py-3 font-medium text-text-muted">작업</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-text-muted">
                    로딩 중...
                  </td>
                </tr>
              ) : tagsError ? (
                <tr><td colSpan={6} className="px-4 py-6"><div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-accent-error">{tt("failed")}<button type="button" onClick={() => void fetchTags()} className="text-accent-primary">{tt("retry")}</button></div></td></tr>
              ) : tags.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-text-muted">
                    등록된 태그가 없습니다.
                  </td>
                </tr>
              ) : (
                tags.map((tag) => (
                  <tr key={tag.id} className="border-b border-border last:border-0 hover:bg-bg-elevated">
                    <td className="px-4 py-3">
                      {tag.color && (
                        <span
                          className="inline-block h-4 w-4 rounded-full"
                          style={{ backgroundColor: tag.color }}
                        />
                      )}
                    </td>
                    <td className="whitespace-normal px-4 py-3 font-medium [overflow-wrap:anywhere]">{tag.name}</td>
                    <td className="whitespace-normal px-4 py-3 text-text-muted [overflow-wrap:anywhere]">{tag.slug}</td>
                    <td className="whitespace-normal px-4 py-3 text-text-muted [overflow-wrap:anywhere]">
                      {tag.parent_id ? tags.find((parent) => parent.id === tag.parent_id)?.name ?? `#${tag.parent_id}` : "-"}
                    </td>
                    <td className="whitespace-normal px-4 py-3 text-text-muted [overflow-wrap:anywhere]">{tag.description ?? "-"}</td>
                    <td className="px-4 py-3">
                      <div className="flex flex-nowrap gap-2">
                        <button
                          onClick={() => handleEdit(tag)}
                          disabled={saving || deletingId !== null || reconciling}
                          className="shrink-0 whitespace-nowrap text-xs text-accent-primary hover:underline"
                        >
                          수정
                        </button>
                        <button
                          onClick={(event) => handleDelete(tag.id, event.currentTarget)}
                          disabled={saving || deletingId !== null || reconciling}
                          className="shrink-0 whitespace-nowrap text-xs text-accent-error hover:underline"
                        >
                          삭제
                        </button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      <section className="rounded-xl border border-border bg-bg-surface p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">공개 미분류 콘텐츠</h2>
            <p className="mt-1 text-sm leading-6 text-text-muted">
              실제 공개 원본 중 지식 태그 junction이 없는 항목입니다. 항목을 열어 매핑을 저장하면 다음 그래프 revision에 반영됩니다.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void reconcileRuleMappings()}
              disabled={reconciling || unmappedLoading || saving || deletingId !== null}
              className="rounded-lg bg-accent-primary px-3 py-2 text-sm font-medium text-white hover:bg-accent-primary/90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {reconciling ? "자동 매핑 중..." : "규칙 기반 자동 매핑"}
            </button>
            <select
              value={unmappedType}
              onChange={(event) => setUnmappedType(event.target.value as UnmappedType)}
              className="rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm"
              aria-label="미분류 콘텐츠 유형"
            >
              <option value="all">전체 유형</option>
              <option value="challenge">문제</option>
              <option value="course">코스</option>
              <option value="lesson">레슨</option>
            </select>
            <button
              type="button"
              onClick={() => void fetchUnmapped()}
              className="rounded-lg border border-border px-3 py-2 text-sm font-medium text-text-secondary hover:bg-bg-elevated"
            >
              새로고침
            </button>
          </div>
        </div>
        <div className="mt-4 space-y-2">
          {unmappedLoading ? (
            <p className="rounded-md border border-dashed border-border p-4 text-sm text-text-muted">미분류 목록을 확인하는 중...</p>
          ) : unmappedError ? (
            <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-accent-error">{tt("unmappedFailed")}<button type="button" onClick={() => void fetchUnmapped()} className="text-accent-primary">{tt("retry")}</button></div>
          ) : unmapped.length === 0 ? (
            <p className="rounded-md border border-dashed border-border p-4 text-sm text-text-muted">현재 공개 미분류 콘텐츠가 없습니다.</p>
          ) : (
            unmapped.map((item) => (
              <div key={`${item.entity_type}:${item.entity_id}`} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border/70 bg-bg-primary px-3 py-3">
                <div className="min-w-0">
                  <span className="text-xs font-medium text-text-muted">{item.entity_type}</span>
                  <p className="truncate text-sm font-medium text-text-primary">{item.name}</p>
                  <p className="text-xs text-text-muted">#{item.entity_id}</p>
                </div>
                <Link
                  href={managementHref(item)}
                  className="rounded-lg bg-accent-primary px-3 py-2 text-xs font-medium text-white hover:bg-accent-primary/90"
                >
                  태그 매핑 열기
                </Link>
              </div>
            ))
          )}
        </div>
      </section>
    </div>
  );
}
