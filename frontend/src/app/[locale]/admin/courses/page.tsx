"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import {
  RetentionDeletionDialog,
  type RetentionDeletionImpact,
} from "@/components/admin/RetentionDeletionDialog";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import { useAuth } from "@/providers/AuthProvider";
import { ListPagination } from "@/components/ui/ListPagination";
import { readEnum, readPositiveInt, writeUrlQuery } from "@/lib/url-query";
import { parseListPage } from "@/lib/list-response.mjs";

interface Course {
  id: string;
  title: string;
  category: string;
  difficulty: string;
  estimated_hours: number;
  is_published: boolean;
  is_deleted: boolean;
  status: string;
  lesson_count: number;
  created_at: string;
}

const STATUS_FILTERS = ["all", "draft", "pending", "approved", "rejected"] as const;
const PAGE_SIZE = 20;

interface CourseListResponse {
  items: Course[];
  total: number;
  page: number;
  size: number;
}

const STATUS_BADGE: Record<string, string> = {
  draft: "bg-bg-surface text-text-muted",
  pending: "bg-accent-warning/10 text-accent-warning",
  approved: "bg-accent-success/10 text-accent-success",
  rejected: "bg-accent-error/10 text-accent-error",
};

export default function AdminCoursesPage() {
  const t = useTranslations("admin");
  const ts = useTranslations("status");
  const td = useTranslations("difficulty");
  const tc = useTranslations("common");
  const { toast } = useToast();
  const { isSuperAdmin } = useAuth();
  const searchParams = useSearchParams();

  const [courses, setCourses] = useState<Course[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState(() => searchParams.get("q") ?? "");
  const [debouncedSearch, setDebouncedSearch] = useState(() => searchParams.get("q") ?? "");
  const [statusFilter, setStatusFilter] = useState(() => readEnum(searchParams.get("status"), STATUS_FILTERS, "all"));
  const [showArchived, setShowArchived] = useState(() => searchParams.get("archived") === "1");
  const [page, setPage] = useState(() => readPositiveInt(searchParams.get("page")));
  const [total, setTotal] = useState(0);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [rejectId, setRejectId] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const [deleteImpact, setDeleteImpact] = useState<RetentionDeletionImpact | null>(null);
  const [deleteImpactLoading, setDeleteImpactLoading] = useState(false);
  const [deleteSubmitting, setDeleteSubmitting] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);

  const syncFromUrl = useCallback(() => {
    const params = new URLSearchParams(window.location.search);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const nextSearch = params.get("q") ?? "";
    setSearch(nextSearch);
    setDebouncedSearch(nextSearch);
    setStatusFilter(readEnum(params.get("status"), STATUS_FILTERS, "all"));
    setShowArchived(params.get("archived") === "1");
    setPage(readPositiveInt(params.get("page")));
  }, []);

  useEffect(() => {
    window.addEventListener("popstate", syncFromUrl);
    return () => {
      window.removeEventListener("popstate", syncFromUrl);
      if (debounceRef.current) clearTimeout(debounceRef.current);
      controllerRef.current?.abort();
    };
  }, [syncFromUrl]);

  const handleSearchChange = (value: string) => {
    setSearch(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setDebouncedSearch(value.trim());
      setPage(1);
      writeUrlQuery({ q: value.trim() || null, page: null });
    }, 300);
  };

  const changeStatus = (value: (typeof STATUS_FILTERS)[number]) => {
    setStatusFilter(value);
    setPage(1);
    writeUrlQuery({ status: value === "all" ? null : value, page: null }, "push");
  };

  const changeArchived = (value: boolean) => {
    setShowArchived(value);
    setPage(1);
    writeUrlQuery({ archived: value, page: null }, "push");
  };

  const changePage = (value: number) => {
    setPage(value);
    writeUrlQuery({ page: value === 1 ? null : value }, "push");
  };

  const fetchCourses = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams();
      if (statusFilter !== "all") params.set("status", statusFilter);
      if (debouncedSearch) params.set("search", debouncedSearch);
      if (showArchived) params.set("include_deleted", "true");
      params.set("page", String(page));
      params.set("size", String(PAGE_SIZE));
      params.set("include_pagination", "true");
      const url = `/api/learn/courses?${params.toString()}`;
      const rawResponse = await apiFetch<unknown>(url, { signal: controller.signal });
      if (controller.signal.aborted || requestId !== requestIdRef.current) return;
      const response = parseListPage<Course>(rawResponse, page, PAGE_SIZE) as CourseListResponse;
      const responseTotalPages = Math.max(1, Math.ceil(response.total / PAGE_SIZE));
      if (page > responseTotalPages) {
        setPage(responseTotalPages);
        writeUrlQuery({ page: responseTotalPages === 1 ? null : responseTotalPages });
        return;
      }
      setCourses(response.items);
      setTotal(response.total);
    } catch (err) {
      if (controller.signal.aborted || requestId !== requestIdRef.current) return;
      setError(err instanceof ApiError ? err.message : tc("error"));
    } finally {
      if (!controller.signal.aborted && requestId === requestIdRef.current) setLoading(false);
    }
  }, [statusFilter, debouncedSearch, showArchived, page, tc]);

  useEffect(() => {
    fetchCourses();
  }, [fetchCourses]);

  const handleApprove = async (courseId: string) => {
    setActionLoading(courseId);
    try {
      await apiFetch(`/api/learn/courses/${courseId}/approve`, { method: "PATCH" });
      await fetchCourses();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setActionLoading(null);
    }
  };

  const handleReject = async () => {
    if (!rejectId || !rejectReason.trim()) return;
    setActionLoading(rejectId);
    try {
      await apiFetch(`/api/learn/courses/${rejectId}/reject`, {
        method: "PATCH",
        body: { rejection_reason: rejectReason },
      });
      setRejectId(null);
      setRejectReason("");
      await fetchCourses();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setActionLoading(null);
    }
  };

  const handleRestoreCourse = async (courseId: string) => {
    if (!isSuperAdmin) return;
    setActionLoading(courseId);
    try {
      await apiFetch(`/api/learn/courses/${courseId}/restore`, { method: "POST" });
      toast("success", t("deletionRestoreSuccess"));
      await fetchCourses();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setActionLoading(null);
    }
  };

  const openDeletionDialog = async (course: Course) => {
    if (!isSuperAdmin) return;
    setDeleteTarget({ id: course.id, name: course.title });
    setDeleteImpact(null);
    setDeleteImpactLoading(true);
    try {
      const impact = await apiFetch<RetentionDeletionImpact>(
        `/api/learn/courses/${course.id}/deletion-impact`,
      );
      setDeleteImpact(impact);
    } catch (err) {
      setDeleteTarget(null);
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setDeleteImpactLoading(false);
    }
  };

  const closeDeletionDialog = () => {
    if (deleteSubmitting) return;
    setDeleteTarget(null);
    setDeleteImpact(null);
  };

  const handleDeleteCourse = async ({
    reason,
    targetName,
  }: {
    reason: string;
    targetName: string;
  }) => {
    if (!isSuperAdmin || !deleteTarget) return;
    setDeleteSubmitting(true);
    try {
      const confirmation = await apiFetch<{ confirmation_token: string }>(
        `/api/learn/courses/${deleteTarget.id}/deletion-confirmations`,
        {
          method: "POST",
          body: { reason, target_name: targetName },
        },
      );
      await apiFetch(`/api/learn/courses/${deleteTarget.id}`, {
        method: "DELETE",
        body: { confirmation_token: confirmation.confirmation_token },
      });
      toast("success", t("deleteSuccess"));
      setDeleteTarget(null);
      setDeleteImpact(null);
      await fetchCourses();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setDeleteSubmitting(false);
    }
  };

  const difficultyLabel = (d: string) => {
    return td(d);
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t("courseManagement")}</h1>
        <Link href="/admin/courses/new">
          <Button>{t("newCourse")}</Button>
        </Link>
      </div>

      {/* 상태 필터 탭 */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex gap-2">
          {STATUS_FILTERS.map((f) => (
            <button
              key={f}
              onClick={() => changeStatus(f)}
              className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
                statusFilter === f
                  ? "bg-accent-primary text-white"
                  : "text-text-secondary hover:bg-bg-surface"
              }`}
            >
              {f === "all" ? t("filterAll") : ts(f)}
            </button>
          ))}
        </div>
        <div className="flex w-full flex-col gap-3 sm:w-auto sm:flex-row sm:items-center">
          <label className="flex items-center gap-2 text-sm text-text-secondary">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(event) => changeArchived(event.target.checked)}
              className="h-4 w-4 rounded border-border accent-accent-primary"
            />
            {t("deletionShowArchived")}
          </label>
          <input
            value={search}
            onChange={(e) => handleSearchChange(e.target.value)}
            placeholder={t("searchCourses")}
            aria-label={t("searchCourses")}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm focus:border-accent-primary focus:outline-none sm:max-w-xs"
          />
        </div>
      </div>

      {/* 로딩 */}
      {loading && (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-16 animate-pulse rounded-lg border border-border bg-bg-surface" />
          ))}
        </div>
      )}

      {/* 에러 */}
      {!loading && error && (
        <div className="py-8 text-center text-text-muted">
          <p>{error}</p>
          <button onClick={fetchCourses} className="mt-2 text-accent-primary hover:underline">{tc("retry")}</button>
        </div>
      )}

      {/* 목록 */}
      {!loading && !error && courses.length === 0 && (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border p-12 text-center">
          <h3 className="mb-1 text-lg font-semibold">{t("noCourses")}</h3>
          <p className="mb-4 text-sm text-text-secondary">{t("noCoursesDesc")}</p>
          <Link href="/admin/courses/new">
            <Button>{t("newCourse")}</Button>
          </Link>
        </div>
      )}

      {!loading && !error && courses.length > 0 && (
        <div className="overflow-hidden rounded-xl border border-border">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-bg-surface">
              <tr>
                <th className="px-4 py-3 text-left font-medium">{t("colTitle")}</th>
                <th className="px-4 py-3 text-left font-medium">{t("colCategory")}</th>
                <th className="px-4 py-3 text-left font-medium">{t("colDifficulty")}</th>
                <th className="px-4 py-3 text-center font-medium">{t("colStatus")}</th>
                <th className="px-4 py-3 text-right font-medium">{t("colActions")}</th>
              </tr>
            </thead>
            <tbody>
              {courses.map((course) => (
                <tr key={course.id} className="border-b border-border last:border-0 hover:bg-bg-surface/50">
                  <td className="px-4 py-3">
                    <Link href={`/admin/courses/${course.id}`} className="font-medium hover:text-accent-primary">
                      {course.title}
                    </Link>
                    <span className="ml-2 text-xs text-text-muted">{t("lessonCount", { count: course.lesson_count })}</span>
                  </td>
                  <td className="px-4 py-3 text-text-secondary">{course.category.toUpperCase()}</td>
                  <td className="px-4 py-3 text-text-secondary">{difficultyLabel(course.difficulty)}</td>
                  <td className="px-4 py-3 text-center">
                    {course.is_deleted ? (
                      <span className="inline-block rounded-full bg-accent-error/10 px-2.5 py-0.5 text-xs font-medium text-accent-error">
                        {t("deletionArchived")}
                      </span>
                    ) : (
                      <span className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_BADGE[course.status] ?? ""}`}>
                        {ts(course.status)}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex items-center justify-end gap-2">
                      {!course.is_deleted && course.status === "pending" && (
                        <>
                          <button
                            onClick={() => handleApprove(course.id)}
                            disabled={actionLoading === course.id}
                            className="rounded-lg bg-accent-success/10 px-3 py-1 text-xs font-medium text-accent-success hover:bg-accent-success/20 disabled:opacity-50"
                          >
                            {ts("approved")}
                          </button>
                          <button
                            onClick={() => setRejectId(course.id)}
                            disabled={actionLoading === course.id}
                            className="rounded-lg bg-accent-error/10 px-3 py-1 text-xs font-medium text-accent-error hover:bg-accent-error/20 disabled:opacity-50"
                          >
                            {ts("rejected")}
                          </button>
                        </>
                      )}
                      {course.is_deleted ? isSuperAdmin && (
                        <button
                          onClick={() => { void handleRestoreCourse(course.id); }}
                          disabled={actionLoading === course.id}
                          className="rounded-lg bg-accent-success/10 px-3 py-1 text-xs font-medium text-accent-success hover:bg-accent-success/20 disabled:opacity-50"
                        >
                          {actionLoading === course.id ? tc("loading") : t("deletionRestore")}
                        </button>
                      ) : (
                        <>
                          <Link href={`/admin/courses/${course.id}`}>
                            <button className="rounded-lg bg-bg-surface px-3 py-1 text-xs font-medium text-text-secondary hover:text-text-primary">
                              {t("chalEdit")}
                            </button>
                          </Link>
                          {isSuperAdmin && <button
                            onClick={() => { void openDeletionDialog(course); }}
                            className="rounded-lg bg-accent-error/10 px-3 py-1 text-xs font-medium text-accent-error hover:bg-accent-error/20"
                          >
                            {t("deleteCourse")}
                          </button>}
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!loading && !error && (
        <ListPagination
          page={page}
          pageSize={PAGE_SIZE}
          total={total}
          onPageChange={changePage}
        />
      )}

      <RetentionDeletionDialog
        open={isSuperAdmin && deleteTarget !== null}
        target={deleteTarget}
        impact={deleteImpact}
        loadingImpact={deleteImpactLoading}
        submitting={deleteSubmitting}
        labels={{
          title: t("deleteCourse"),
          resourceLabel: t("deletionResourceCourse"),
          retainedDescription: t("deletionRetainedDescription"),
          impactTitle: t("deletionImpactTitle"),
          impactLoading: t("deletionImpactLoading"),
          impactEmpty: t("deletionImpactEmpty"),
          reasonLabel: t("deletionReasonLabel"),
          reasonPlaceholder: t("deletionReasonPlaceholder"),
          targetNameLabel: t("deletionTargetNameLabel"),
          targetNameHelp: t("deletionTargetNameHelp"),
          next: t("deletionNext"),
          cancel: tc("cancel"),
          execute: t("deletionExecute"),
          executing: t("deletionExecuting"),
          stageLabel: t("deletionStage", { stage: "{stage}" }),
        }}
        impactLabels={{
          lessons: t("deletionImpactLessons"),
          lesson_progress: t("deletionImpactLessonProgress"),
          course_prerequisites: t("deletionImpactCoursePrerequisites"),
          challenge_learning_prerequisites: t("deletionImpactChallengeLearningPrerequisites"),
          knowledge_tag_mappings: t("deletionImpactKnowledgeTagMappings"),
          lesson_attachments: t("deletionImpactLessonAttachments"),
        }}
        onClose={closeDeletionDialog}
        onSubmit={handleDeleteCourse}
      />

      {/* 거부 사유 모달 */}
      {rejectId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="reject-reason-title"
            className="w-full max-w-md rounded-xl border border-border bg-bg-primary p-6 shadow-lg"
          >
            <h3 id="reject-reason-title" className="mb-4 text-lg font-semibold">{t("rejectReasonTitle")}</h3>
            <textarea
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              placeholder={t("rejectReasonPlaceholder")}
              rows={4}
              className="w-full rounded-lg border border-border bg-bg-surface px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
            />
            <div className="mt-4 flex justify-end gap-3">
              <Button variant="ghost" onClick={() => { setRejectId(null); setRejectReason(""); }}>
                {tc("cancel")}
              </Button>
              <Button
                onClick={handleReject}
                disabled={!rejectReason.trim() || actionLoading === rejectId}
                className="bg-accent-error text-white hover:bg-accent-error/90"
              >
                {ts("rejected")}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
