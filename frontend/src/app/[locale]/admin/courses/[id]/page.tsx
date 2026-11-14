"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { apiFetch, ApiError } from "@/lib/api-client";
import { CourseLessonManager } from "@/components/admin/courses/CourseLessonManager";
import { CoursePrerequisiteManager } from "@/components/admin/courses/CoursePrerequisiteManager";
import { CourseMetadataEditor, type CourseMetadata } from "@/components/admin/courses/CourseMetadataEditor";
import { KnowledgeTagMappingEditor } from "@/components/admin/knowledge/KnowledgeTagMappingEditor";
import { useKnowledgeTagMappingDraft } from "@/lib/use-knowledge-tag-mapping-draft";
import { useToast } from "@/providers/ToastProvider";
import type { Lesson } from "@/types/course";

interface CourseDetail {
  id: string;
  title: string;
  description: string | null;
  metadata_revision: string;
  category: string;
  difficulty: string;
  estimated_hours: number;
  is_published: boolean;
  is_deleted: boolean;
  status: string;
  rejection_reason: string | null;
  lessons: Lesson[];
  prerequisites: string[];
  created_at: string;
}

const STATUS_BADGE: Record<string, string> = {
  draft: "bg-bg-surface text-text-muted",
  pending: "bg-accent-warning/10 text-accent-warning",
  approved: "bg-accent-success/10 text-accent-success",
  rejected: "bg-accent-error/10 text-accent-error",
};

export default function AdminCourseDetailPage() {
  const t = useTranslations("admin");
  const ts = useTranslations("status");
  const tc = useTranslations("common");
  const td = useTranslations("difficulty");
  const params = useParams();
  const courseId = params.id as string;
  const { toast } = useToast();

  const [course, setCourse] = useState<CourseDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionLoading, setActionLoading] = useState(false);
  const [lessonAuthoringActive, setLessonAuthoringActive] = useState(false);
  const [metadataEditing, setMetadataEditing] = useState(false);
  const courseRequestGenerationRef = useRef(0);
  const mappingController = useKnowledgeTagMappingDraft("course", courseId);

  const fetchCourse = useCallback(async () => {
    const generation = ++courseRequestGenerationRef.current;
    setLoading(true);
    setError("");
    try {
      const data = await apiFetch<CourseDetail>(`/api/learn/courses/${courseId}`);
      if (generation !== courseRequestGenerationRef.current) return;
      setCourse(data);
    } catch (err) {
      if (generation !== courseRequestGenerationRef.current) return;
      setError(err instanceof ApiError ? err.message : tc("error"));
    } finally {
      if (generation === courseRequestGenerationRef.current) setLoading(false);
    }
  }, [courseId, tc]);

  useEffect(() => {
    setMetadataEditing(false);
    setLessonAuthoringActive(false);
    void fetchCourse();
    return () => {
      courseRequestGenerationRef.current += 1;
    };
  }, [fetchCourse]);

  const mergeCourseMetadata = useCallback((metadata: CourseMetadata) => {
    setCourse((current) => (
      current && current.id === metadata.id
        ? {
            ...current,
            title: metadata.title,
            description: metadata.description,
            metadata_revision: metadata.metadata_revision,
          }
        : current
    ));
  }, []);

  const handleApprove = async () => {
    setActionLoading(true);
    try {
      await apiFetch(`/api/learn/courses/${courseId}/approve`, { method: "PATCH" });
      await fetchCourse();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setActionLoading(false);
    }
  };

  const handleSubmitForReview = async () => {
    setActionLoading(true);
    try {
      await apiFetch(`/api/learn/courses/${courseId}/submit`, { method: "PATCH" });
      await fetchCourse();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("error"));
    } finally {
      setActionLoading(false);
    }
  };

  if (loading && (!course || course.id !== courseId)) {
    return (
      <div className="space-y-4">
        <div className="h-8 w-64 animate-pulse rounded bg-bg-surface" />
        <div className="h-40 animate-pulse rounded-xl bg-bg-surface" />
      </div>
    );
  }

  if (!course || course.id !== courseId) {
    return (
      <div className="py-12 text-center">
        <p className="text-text-muted">{error || tc("error")}</p>
        <button onClick={fetchCourse} className="mt-2 text-accent-primary hover:underline">{tc("retry")}</button>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      {error && <div role="alert" className="flex flex-wrap items-center gap-2 text-sm text-accent-error">
        <span>{error}</span><button type="button" className="underline" onClick={fetchCourse}>{tc("retry")}</button>
      </div>}
      {/* 뒤로 */}
      <Link href="/admin/courses" className="text-sm text-text-muted hover:text-text-primary">
        {`\u2190 ${t("courseManagement")}`}
      </Link>

      {/* 코스 정보 헤더 */}
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="min-w-0 break-words text-2xl font-bold">{course.title}</h1>
              <span className={`shrink-0 rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_BADGE[course.status] ?? ""}`}>
                {ts(course.status)}
              </span>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-text-muted">
              <span>{course.category.toUpperCase()}</span>
              <span>{td(course.difficulty)}</span>
              <span>{course.estimated_hours}h</span>
              <span>{t("lessonCount", { count: course.lessons.length })}</span>
            </div>
            {course.description && (
              <div className="mt-3 text-sm text-text-secondary"><MarkdownRenderer content={course.description} /></div>
            )}
            {course.rejection_reason && (
              <div className="mt-3 rounded-lg bg-accent-error/5 p-3 text-sm text-accent-error">
                {t("rejectReasonTitle")}: {course.rejection_reason}
              </div>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            {course.status === "draft" && (
              <Button onClick={handleSubmitForReview} disabled={actionLoading || lessonAuthoringActive || metadataEditing || mappingController.saving || mappingController.dirty} variant="secondary">
                {tc("submit")}
              </Button>
            )}
            {course.status === "pending" && (
              <Button onClick={handleApprove} disabled={actionLoading || lessonAuthoringActive || metadataEditing || mappingController.saving || mappingController.dirty}>
                {ts("approved")}
              </Button>
            )}
          </div>
        </div>
      </div>

      <CourseMetadataEditor
        key={course.id}
        course={course}
        archived={course.is_deleted}
        lessonAuthoringActive={lessonAuthoringActive}
        courseActionActive={actionLoading}
        mappingController={mappingController}
        onEditingChange={setMetadataEditing}
        onSaved={mergeCourseMetadata}
      />

      <div inert={metadataEditing ? true : undefined} className={`space-y-6 ${metadataEditing ? "opacity-60" : ""}`}>
        {!metadataEditing && <section className="rounded-xl border border-border bg-bg-surface p-6">
          <KnowledgeTagMappingEditor entityType="course" entityId={courseId} controller={mappingController} independentSave
            disabled={metadataEditing || actionLoading || course.is_deleted} />
        </section>}

        <CoursePrerequisiteManager
          key={course.id}
          courseId={courseId}
          initialPrerequisiteIds={course.prerequisites}
          disabled={course.is_deleted}
          onSaved={(prerequisiteIds) => {
            setCourse((current) => (
              current ? { ...current, prerequisites: prerequisiteIds } : current
            ));
          }}
        />

        {/* 레슨 관리 */}
        <CourseLessonManager
          courseId={courseId}
          lessons={course.lessons}
          onRefresh={fetchCourse}
          onAuthoringStateChange={setLessonAuthoringActive}
        />
      </div>
    </div>
  );
}
