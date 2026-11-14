"use client";

import { useState, useEffect } from "react";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api-client";

interface CourseItem {
  id: string;
  title: string;
}

interface LessonItem {
  id: string;
  title: string;
}

interface ChallengeItem {
  id: number;
  name: string;
  category: string;
}

interface Props {
  recommendedChallenges: number[];
  recommendedLearning: { type: string; target_id: string }[];
  onChangeChallenges: (ids: number[]) => void;
  onChangeLearning: (items: { type: string; target_id: string }[]) => void;
  allChallenges: ChallengeItem[];
  currentChallengeId?: number;
}

export function RecommendedPrereqSelector({
  recommendedChallenges,
  recommendedLearning,
  onChangeChallenges,
  onChangeLearning,
  allChallenges,
  currentChallengeId,
}: Props) {
  const t = useTranslations("admin");
  const [courses, setCourses] = useState<CourseItem[]>([]);
  const [lessons, setLessons] = useState<LessonItem[]>([]);
  const [challengeSearch, setChallengeSearch] = useState("");
  const [learningSearch, setLearningSearch] = useState("");
  const [learningType, setLearningType] = useState<"course" | "lesson">("course");
  const [selectedCourseId, setSelectedCourseId] = useState("");

  useEffect(() => {
    apiFetch<CourseItem[] | { items: CourseItem[] }>("/api/learn/courses?size=100")
      .then((res) => setCourses(Array.isArray(res) ? res : res.items ?? []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (learningType !== "lesson" || !selectedCourseId) {
      return;
    }

    let cancelled = false;
    apiFetch<{ lessons?: LessonItem[] }>(`/api/learn/courses/${selectedCourseId}`)
      .then((res) => {
        if (!cancelled) setLessons(res.lessons ?? []);
      })
      .catch(() => {
        if (!cancelled) setLessons([]);
      });

    return () => {
      cancelled = true;
    };
  }, [learningType, selectedCourseId]);

  const activeLessons =
    learningType === "lesson" && selectedCourseId ? lessons : [];

  // 추천 챌린지 필터
  const filteredChallenges = allChallenges.filter(
    (c) =>
      c.id !== currentChallengeId &&
      !recommendedChallenges.includes(c.id) &&
      c.name.toLowerCase().includes(challengeSearch.toLowerCase())
  );

  const filteredCourses = courses.filter(
    (c) =>
      !recommendedLearning.some((r) => r.type === "course" && r.target_id === c.id) &&
      c.title.toLowerCase().includes(learningSearch.toLowerCase())
  );

  const filteredLessons = activeLessons.filter(
    (l) =>
      !recommendedLearning.some((r) => r.type === "lesson" && r.target_id === l.id) &&
      l.title.toLowerCase().includes(learningSearch.toLowerCase())
  );

  return (
    <div className="space-y-4">
      {/* 추천 선행 문제 */}
      <div>
        <label className="mb-1.5 block text-sm font-medium">
          {t("recommendedChallenges")}
        </label>
        <p className="mb-2 text-xs text-text-muted">{t("recommendedChallengesDesc")}</p>

        {recommendedChallenges.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {recommendedChallenges.map((cid) => {
              const ch = allChallenges.find((c) => c.id === cid);
              return (
                <span key={cid} className="inline-flex items-center gap-1.5 rounded-full bg-accent-warning/10 px-3 py-1 text-xs font-medium text-accent-warning">
                  #{cid} {ch?.name ?? ""}
                  <button type="button" onClick={() => onChangeChallenges(recommendedChallenges.filter((id) => id !== cid))} className="text-accent-warning/60 hover:text-accent-error">&times;</button>
                </span>
              );
            })}
          </div>
        )}

        <div className="relative">
          <input
            value={challengeSearch}
            onChange={(e) => setChallengeSearch(e.target.value)}
            onBlur={() => setTimeout(() => setChallengeSearch(""), 200)}
            placeholder="추천 선행 문제 검색..."
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm focus:border-accent-primary focus:outline-none"
          />
          {challengeSearch && filteredChallenges.length > 0 && (
            <div className="absolute z-20 mt-1 max-h-40 w-full overflow-y-auto rounded-lg border border-border bg-bg-surface shadow-lg">
              {filteredChallenges.slice(0, 10).map((c) => (
                <button key={c.id} type="button" onMouseDown={() => { onChangeChallenges([...recommendedChallenges, c.id]); setChallengeSearch(""); }}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-text-secondary hover:bg-bg-elevated hover:text-text-primary">
                  <span className="text-text-muted">#{c.id}</span>
                  <span>{c.name}</span>
                  <span className="ml-auto text-xs text-text-muted">{c.category}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 추천 선행 학습 */}
      <div>
        <label className="mb-1.5 block text-sm font-medium">
          {t("recommendedLearningLabel")}
        </label>
        <p className="mb-2 text-xs text-text-muted">{t("recommendedLearningDesc")}</p>

        {recommendedLearning.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {recommendedLearning.map((item) => {
              const name = item.type === "course"
                ? courses.find((c) => c.id === item.target_id)?.title
                : activeLessons.find((l) => l.id === item.target_id)?.title;
              return (
                <span key={`${item.type}-${item.target_id}`} className="inline-flex items-center gap-1.5 rounded-full bg-accent-warning/10 px-3 py-1 text-xs font-medium text-accent-warning">
                  <span className="rounded bg-accent-warning/20 px-1 text-[10px] uppercase">{item.type === "course" ? "코스" : "레슨"}</span>
                  {name ?? item.target_id}
                  <button type="button" onClick={() => onChangeLearning(recommendedLearning.filter((r) => !(r.type === item.type && r.target_id === item.target_id)))} className="text-accent-warning/60 hover:text-accent-error">&times;</button>
                </span>
              );
            })}
          </div>
        )}

        <div className="mb-2 flex gap-2">
          <button type="button" onClick={() => { setLearningType("course"); setSelectedCourseId(""); }}
            className={`rounded-lg px-3 py-1 text-xs font-medium transition ${learningType === "course" ? "bg-accent-primary text-white" : "bg-bg-elevated text-text-muted hover:text-text-primary"}`}>
            코스
          </button>
          <button type="button" onClick={() => setLearningType("lesson")}
            className={`rounded-lg px-3 py-1 text-xs font-medium transition ${learningType === "lesson" ? "bg-accent-primary text-white" : "bg-bg-elevated text-text-muted hover:text-text-primary"}`}>
            레슨
          </button>
        </div>

        {learningType === "lesson" && (
          <select value={selectedCourseId} onChange={(e) => setSelectedCourseId(e.target.value)}
            className="mb-2 w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none">
            <option value="">코스를 먼저 선택하세요...</option>
            {courses.map((c) => (<option key={c.id} value={c.id}>{c.title}</option>))}
          </select>
        )}

        <div className="relative">
          <input value={learningSearch} onChange={(e) => setLearningSearch(e.target.value)}
            onBlur={() => setTimeout(() => setLearningSearch(""), 200)}
            placeholder={learningType === "course" ? "추천 코스 검색..." : "추천 레슨 검색..."}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm focus:border-accent-primary focus:outline-none"
          />
          {learningType === "course" && learningSearch && filteredCourses.length > 0 && (
            <div className="absolute z-20 mt-1 max-h-40 w-full overflow-y-auto rounded-lg border border-border bg-bg-surface shadow-lg">
              {filteredCourses.slice(0, 10).map((c) => (
                <button key={c.id} type="button" onMouseDown={() => { onChangeLearning([...recommendedLearning, { type: "course", target_id: c.id }]); setLearningSearch(""); }}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-text-secondary hover:bg-bg-elevated hover:text-text-primary">
                  <span className="rounded bg-bg-elevated px-1 text-[10px] text-text-muted uppercase">코스</span>
                  <span>{c.title}</span>
                </button>
              ))}
            </div>
          )}
          {learningType === "lesson" && selectedCourseId && filteredLessons.length > 0 && (
            <div className="absolute z-20 mt-1 max-h-40 w-full overflow-y-auto rounded-lg border border-border bg-bg-surface shadow-lg">
              {filteredLessons.map((l) => (
                <button key={l.id} type="button" onMouseDown={() => { onChangeLearning([...recommendedLearning, { type: "lesson", target_id: l.id }]); setLearningSearch(""); }}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-text-secondary hover:bg-bg-elevated hover:text-text-primary">
                  <span className="rounded bg-bg-elevated px-1 text-[10px] text-text-muted uppercase">레슨</span>
                  <span>{l.title}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
