"use client";

import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";

interface Lesson {
  id: string;
  title: string;
  order_index: number;
  estimated_minutes: number;
  completed: boolean;
}

interface LessonListProps {
  courseId: string;
  lessons: Lesson[];
}

export function LessonList({ courseId, lessons }: LessonListProps) {
  const t = useTranslations("learn");

  return (
    <div className="space-y-2">
      {lessons.map((lesson, i) => (
        <Link
          key={lesson.id}
          href={`/learn/${courseId}/${lesson.id}`}
          className="group flex items-center gap-4 rounded-lg border border-border bg-bg-primary p-4 transition-colors hover:border-accent-primary"
        >
          {/* 완료 상태 아이콘 */}
          <div
            className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-bold ${
              lesson.completed
                ? "bg-accent-success/10 text-accent-success"
                : "bg-bg-surface text-text-muted"
            }`}
          >
            {lesson.completed ? (
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={3}>
                <polyline points="20 6 9 17 4 12" />
              </svg>
            ) : (
              <span>{i + 1}</span>
            )}
          </div>

          {/* 레슨 정보 */}
          <div className="flex-1">
            <h4 className="text-sm font-medium group-hover:text-accent-primary">
              {lesson.title}
            </h4>
          </div>

          {/* 소요 시간 */}
          <span className="shrink-0 text-xs text-text-muted">
            {t("minutes", { n: lesson.estimated_minutes })}
          </span>
        </Link>
      ))}
    </div>
  );
}
