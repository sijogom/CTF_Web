"use client";

import { useState, useEffect } from "react";
import { useParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { LessonList } from "@/components/learn/LessonList";
import { CategoryIcon } from "@/components/ui/CategoryIcon";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { apiFetch, ApiError } from "@/lib/api-client";

interface LessonSummary {
  id: string;
  title: string;
  order_index: number;
  estimated_minutes: number;
  completed: boolean;
}

interface Course {
  id: string;
  title: string;
  description: string;
  category: string;
  difficulty: string;
  estimated_hours: number;
  author_id: string;
  is_published: boolean;
  prerequisites: string[];
  prerequisite_details: Array<{
    id: string;
    title: string;
    completed: boolean;
    completed_lessons: number;
    total_lessons: number;
  }>;
  locked: boolean;
  progress_percent: number;
  lessons: LessonSummary[];
}

export default function CourseDetailPage() {
  const params = useParams();
  const t = useTranslations("learn");
  const td = useTranslations("difficulty");
  const courseId = params.courseId as string;

  const [course, setCourse] = useState<Course | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 데이터 페칭 초기화
    setLoading(true);
    setError(null);
    apiFetch<Course>(`/api/learn/courses/${courseId}`)
      .then(setCourse)
      .catch((err) => setError(err instanceof ApiError ? err.message : t("loadFailed")))
      .finally(() => setLoading(false));
  }, [courseId, t]);

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }

  if (error) {
    return <div className="py-20 text-center text-status-error">{error}</div>;
  }

  if (!course) return null;

  const completedCount = course.lessons.filter((l) => l.completed).length;
  const totalCount = course.lessons.length;

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      {/* 헤더 */}
      <div>
        <div className="mb-3 flex items-center gap-3">
          <CategoryIcon
            category={course.category.toLowerCase() as "web" | "pwn" | "rev" | "crypto" | "forensic" | "misc"}
            size="md"
          />
          <span className="text-sm font-medium uppercase text-text-muted">{course.category}</span>
          <span className="rounded-full bg-bg-surface px-2.5 py-0.5 text-xs font-medium text-text-secondary">
            {td(course.difficulty)}
          </span>
        </div>
        <h1 className="text-3xl font-bold">{course.title}</h1>
        <div className="mt-2 text-text-secondary"><MarkdownRenderer content={course.description} /></div>
        <Link
          href={`/learn?view=map&focus_type=course&focus_id=${course.id}`}
          className="mt-4 inline-flex items-center gap-2 rounded-lg border border-accent-primary/40 px-3 py-2 text-sm font-medium text-accent-primary transition-colors hover:border-accent-primary hover:bg-accent-primary/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
        >
          <span aria-hidden="true">◎</span>
          {t("viewMap")}
        </Link>
      </div>

      {course.locked ? (
        <section className="rounded-xl border border-accent-warning/40 bg-accent-warning/10 p-6" aria-labelledby="course-prerequisite-title">
          <h2 id="course-prerequisite-title" className="text-xl font-semibold text-text-primary">{t("courseLocked")}</h2>
          <p className="mt-2 text-sm leading-6 text-text-secondary">{t("courseLockedDescription")}</p>
          <ul className="mt-4 space-y-2">
            {course.prerequisite_details.map((prerequisite) => (
              <li key={prerequisite.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-bg-surface px-4 py-3 text-sm">
                <div>
                  <p className="font-medium text-text-primary">{prerequisite.title}</p>
                  <p className="mt-1 text-xs text-text-muted">
                    {t("prerequisiteProgress", { completed: prerequisite.completed_lessons, total: prerequisite.total_lessons })}
                  </p>
                </div>
                {prerequisite.completed ? (
                  <span className="inline-flex items-center gap-1 rounded-full bg-accent-success/15 px-2.5 py-1 text-xs font-medium text-accent-success">
                    ✓ {t("completed")}
                  </span>
                ) : (
                  <Link href={`/learn/${prerequisite.id}`} className="rounded-lg bg-accent-primary px-3 py-2 text-xs font-medium text-white hover:bg-accent-primary-hover">
                    {t("openPrerequisite")}
                  </Link>
                )}
              </li>
            ))}
          </ul>
        </section>
      ) : (
        <>
          {/* 진행 상황 */}
          <div className="rounded-xl border border-border bg-bg-surface p-6">
            <div className="mb-2 flex items-center justify-between text-sm">
              <span className="text-text-secondary">
                {t("lessonsCompleted", { completed: completedCount, total: totalCount })}
              </span>
              <span className="font-medium text-accent-primary">
                {Math.round(course.progress_percent)}%
              </span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-bg-primary">
              <div
                className="h-full rounded-full bg-accent-primary transition-all"
                style={{ width: `${course.progress_percent}%` }}
              />
            </div>
            <p className="mt-2 text-xs text-text-muted">
              {t("estimatedTimeHours", { n: course.estimated_hours })}
            </p>
          </div>

          {/* 레슨 목록 */}
          <div>
            <h2 className="mb-4 text-xl font-semibold">{t("lessonList")}</h2>
            <LessonList courseId={courseId} lessons={course.lessons} />
          </div>
        </>
      )}
    </div>
  );
}
