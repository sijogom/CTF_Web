"use client";

import { Link } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { CategoryIcon } from "@/components/ui/CategoryIcon";

interface CourseCardProps {
  course: {
    id: string;
    title: string;
    category: string;
    difficulty: string;
    estimated_hours: number;
    lesson_count: number;
    progress_percent: number;
    thumbnail_url?: string;
  };
}

const DIFFICULTY_STYLES: Record<string, { key: string; color: string }> = {
  introductory: { key: "introductory", color: "bg-emerald-500/10 text-emerald-400" },
  basic:        { key: "basic",        color: "bg-sky-500/10 text-sky-400" },
  intermediate: { key: "intermediate", color: "bg-amber-500/10 text-amber-400" },
  advanced:     { key: "advanced",     color: "bg-red-500/10 text-red-400" },
};

export function CourseCard({ course }: CourseCardProps) {
  const td = useTranslations("difficulty");
  const t = useTranslations("learn");
  const diff = DIFFICULTY_STYLES[course.difficulty] ?? DIFFICULTY_STYLES.introductory;

  return (
    <Link
      href={`/learn/${course.id}`}
      className="group block min-w-0 rounded-xl border border-border bg-bg-surface p-5 transition-all hover:border-accent-primary hover:shadow-lg"
    >
      {/* 상단: 카테고리 + 난이도 */}
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <CategoryIcon
            category={course.category.toLowerCase() as "web" | "pwn" | "rev" | "crypto" | "forensic" | "misc"}
            size="sm"
          />
          <span className="text-xs font-medium uppercase text-text-muted">
            {course.category}
          </span>
        </div>
        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${diff.color}`}>
          {td(diff.key)}
        </span>
      </div>

      {/* 제목 */}
      <h3 className="mb-2 min-w-0 text-lg font-semibold [overflow-wrap:anywhere] group-hover:text-accent-primary">
        {course.title}
      </h3>

      {/* 메타 정보 */}
      <div className="mb-3 flex items-center gap-3 text-sm text-text-secondary">
        <span>{t("lessonCount", { count: course.lesson_count })}</span>
        <span>{t("estimatedHours", { n: course.estimated_hours })}</span>
      </div>

      {/* 진행도 바 */}
      {course.progress_percent > 0 && (
        <div className="mt-auto">
          <div className="mb-1 flex items-center justify-between text-xs text-text-muted">
            <span>{t("progress")}</span>
            <span>{Math.round(course.progress_percent)}%</span>
          </div>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-bg-primary">
            <div
              className="h-full rounded-full bg-accent-primary transition-all"
              style={{ width: `${Math.min(100, Math.max(0, course.progress_percent))}%` }}
            />
          </div>
        </div>
      )}
    </Link>
  );
}
