"use client";

import { useState, useEffect, useLayoutEffect, useRef } from "react";
import { useParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { Button } from "@/components/ui/Button";
import { Link } from "@/i18n/navigation";
import { apiFetch, ApiError } from "@/lib/api-client";
import { formatFileSize } from "@/lib/utils";
import { useAuth } from "@/providers/AuthProvider";

interface AttachmentInfo {
  filename: string;
  original_name: string;
  size: number;
  content_type: string;
}

interface Lesson {
  id: string;
  title: string;
  content: string;
  order_index: number;
  estimated_minutes: number;
  challenge_id: string | null;
  attachments: AttachmentInfo[];
  completed: boolean;
}

interface LessonSiblings {
  prev: { id: string; title: string } | null;
  next: { id: string; title: string } | null;
}

interface LessonResponse {
  lesson: Lesson;
  siblings: LessonSiblings;
}

function getFileIcon(contentType: string): string {
  if (contentType.startsWith("image/")) return "image";
  if (contentType === "application/pdf") return "pdf";
  if (contentType.includes("zip") || contentType.includes("tar") || contentType.includes("compressed")) return "archive";
  return "file";
}

export default function LessonPage() {
  const params = useParams();
  const t = useTranslations("learn");
  const tc = useTranslations("common");
  const courseId = params.courseId as string;
  const lessonId = params.lessonId as string;
  const { user } = useAuth();
  const identity = `${courseId}:${lessonId}:${user?.id ?? "anonymous"}`;
  const identityRef = useRef(identity);
  useLayoutEffect(() => { identityRef.current = identity; }, [identity]);
  const generationRef = useRef(0);
  const completionController = useRef<AbortController | null>(null);

  const [lesson, setLesson] = useState<Lesson | null>(null);
  const [siblings, setSiblings] = useState<LessonSiblings>({ prev: null, next: null });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [completed, setCompleted] = useState(false);
  const [completing, setCompleting] = useState(false);
  const [loadedIdentity, setLoadedIdentity] = useState("");

  useEffect(() => {
    const generation = ++generationRef.current;
    const controller = new AbortController();
    const isCurrent = () => !controller.signal.aborted
      && generation === generationRef.current && identityRef.current === identity;
    setLoadedIdentity(identity);
    setLoading(true);
    setError(null);
    setLesson(null);
    setCompleted(false);
    setCompleting(false);
    apiFetch<LessonResponse>(`/api/learn/courses/${courseId}/lessons/${lessonId}`, { signal: controller.signal })
      .then((data) => {
        if (!isCurrent()) return;
        setLesson(data.lesson);
        setSiblings(data.siblings);
        setCompleted(data.lesson.completed);
      })
      .catch((err) => { if (isCurrent()) setError(err instanceof ApiError ? err.message : tc("errorOccurred")); })
      .finally(() => { if (isCurrent()) setLoading(false); });
    return () => {
      controller.abort();
      completionController.current?.abort();
      completionController.current = null;
      generationRef.current += 1;
    };
  }, [courseId, lessonId, tc, identity]);

  const handleComplete = async () => {
    if (completionController.current || loadedIdentity !== identity || !lesson || completed) return;
    const controller = new AbortController();
    completionController.current = controller;
    const generation = generationRef.current;
    const isCurrent = () => !controller.signal.aborted
      && generation === generationRef.current && identityRef.current === identity;
    setCompleting(true);
    try {
      await apiFetch(`/api/learn/progress/${lessonId}/complete`, { method: "POST", signal: controller.signal });
      if (isCurrent()) setCompleted(true);
    } catch (err) {
      if (!isCurrent()) return;
      const message = err instanceof ApiError ? err.message : "Failed to complete lesson";
      setError(message);
    } finally {
      if (completionController.current === controller) completionController.current = null;
      if (isCurrent()) setCompleting(false);
    }
  };

  if (loading || loadedIdentity !== identity) {
    return (
      <div className="flex justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }

  if (error) {
    return <div className="py-20 text-center text-status-error">{error}</div>;
  }

  if (!lesson) return null;

  const attachments = lesson.attachments || [];

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      {/* 네비게이션 */}
      <div className="flex items-center gap-2 text-sm text-text-muted">
        <Link href="/learn" className="hover:text-text-primary">
          {t("breadcrumbLearn")}
        </Link>
        <span>/</span>
        <Link href={`/learn/${courseId}`} className="hover:text-text-primary">
          {t("breadcrumbCourse")}
        </Link>
        <span>/</span>
        <span className="text-text-primary">{lesson.title}</span>
      </div>

      {/* 레슨 헤더 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{lesson.title}</h1>
          <Link
            href={`/learn?view=map&focus_type=lesson&focus_id=${lesson.id}`}
            className="mt-2 inline-flex items-center gap-2 text-sm font-medium text-accent-primary hover:text-accent-primary-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
          >
            <span aria-hidden="true">◎</span>
            {t("viewMap")}
          </Link>
        </div>
        <span className="text-sm text-text-muted">
          {t("estimatedMinutes", { n: lesson.estimated_minutes })}
        </span>
      </div>

      {/* 마크다운 본문 */}
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <MarkdownRenderer content={lesson.content} />
      </div>

      {/* 첨부파일 */}
      {attachments.length > 0 && (
        <div className="rounded-xl border border-border bg-bg-surface p-6">
          <h2 className="mb-4 text-lg font-semibold">{t("attachments")}</h2>
          <div className="space-y-2">
            {attachments.map((att) => (
              <a
                key={att.filename}
                href={`/ctf/api/learn/courses/${courseId}/lessons/${lessonId}/attachments/${att.filename}/download`}
                className="flex items-center gap-3 rounded-lg border border-border bg-bg-primary px-4 py-3 transition-colors hover:border-accent-primary hover:bg-bg-primary/80"
              >
                {/* 파일 아이콘 */}
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-accent-primary/10 text-accent-primary">
                  {getFileIcon(att.content_type) === "image" ? (
                    <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                      <rect x="3" y="3" width="18" height="18" rx="2" />
                      <circle cx="8.5" cy="8.5" r="1.5" />
                      <path d="m21 15-5-5L5 21" />
                    </svg>
                  ) : getFileIcon(att.content_type) === "archive" ? (
                    <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                      <path d="M21 8v13H3V8" />
                      <path d="M1 3h22v5H1z" />
                      <path d="M10 12h4" />
                    </svg>
                  ) : (
                    <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                      <polyline points="14 2 14 8 20 8" />
                    </svg>
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium truncate">{att.original_name}</p>
                  <p className="text-xs text-text-muted">{formatFileSize(att.size)}</p>
                </div>
                <svg className="h-4 w-4 shrink-0 text-text-muted" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <polyline points="7 10 12 15 17 10" />
                  <line x1="12" y1="15" x2="12" y2="3" />
                </svg>
              </a>
            ))}
          </div>
        </div>
      )}

      {/* 실습 연동 */}
      {lesson.challenge_id && (
        <div className="rounded-xl border border-border bg-bg-surface p-6">
          <h2 className="mb-2 text-lg font-semibold">{t("relatedChallenge")}</h2>
          <p className="mb-4 text-sm text-text-secondary">
            {t("relatedChallengeDesc")}
          </p>
          <Link href={`/wargame/${lesson.challenge_id}`}>
            <Button variant="secondary">{t("goToChallenge")}</Button>
          </Link>
        </div>
      )}

      {/* 완료 + 네비게이션 */}
      <div className="flex items-center justify-between rounded-xl border border-border bg-bg-surface p-6">
        {completed ? (
          <div className="flex items-center gap-2 text-accent-success">
            <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5}>
              <polyline points="20 6 9 17 4 12" />
            </svg>
            <span className="font-medium">{t("lessonCompleted")}</span>
          </div>
        ) : (
          <Button onClick={handleComplete} disabled={completing}>
            {completing ? tc("processing") : t("markComplete")}
          </Button>
        )}

        <div className="flex gap-2">
          {siblings.prev && (
            <Link href={`/learn/${courseId}/${siblings.prev.id}`}>
              <Button variant="ghost" size="sm">
                {t("prevLesson")}
              </Button>
            </Link>
          )}
          <Link href={`/learn/${courseId}`}>
            <Button variant="ghost" size="sm">
              {t("backToList")}
            </Button>
          </Link>
          {siblings.next && (
            <Link href={`/learn/${courseId}/${siblings.next.id}`}>
              <Button variant="ghost" size="sm">
                {t("nextLesson")}
              </Button>
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}
