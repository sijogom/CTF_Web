"use client";

import { useState, useEffect } from "react";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api-client";

interface LearningPrereq {
  type: string;
  target_id: string;
}

interface CourseItem {
  id: string;
  title: string;
}

interface LessonItem {
  id: string;
  title: string;
  order_index: number;
}

interface Props {
  selected: LearningPrereq[];
  onChange: (items: LearningPrereq[]) => void;
}

export function LearningPrereqSelector({ selected, onChange }: Props) {
  const t = useTranslations("admin");
  const [courses, setCourses] = useState<CourseItem[]>([]);
  const [lessons, setLessons] = useState<LessonItem[]>([]);
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState<"course" | "lesson">("course");
  const [selectedCourseId, setSelectedCourseId] = useState<string>("");

  // 코스 목록
  useEffect(() => {
    apiFetch<CourseItem[] | { items: CourseItem[] }>("/api/learn/courses?size=100")
      .then((res) => setCourses(Array.isArray(res) ? res : res.items ?? []))
      .catch(() => {});
  }, []);

  // 레슨 탭에서 코스 선택 시 해당 코스의 레슨 로딩
  useEffect(() => {
    if (typeFilter !== "lesson" || !selectedCourseId) {
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
  }, [typeFilter, selectedCourseId]);

  const activeLessons =
    typeFilter === "lesson" && selectedCourseId ? lessons : [];

  const filteredCourses = courses.filter(
    (c) =>
      !selected.some((s) => s.type === "course" && s.target_id === c.id) &&
      c.title.toLowerCase().includes(search.toLowerCase())
  );

  const filteredLessons = activeLessons.filter(
    (l) =>
      !selected.some((s) => s.type === "lesson" && s.target_id === l.id) &&
      l.title.toLowerCase().includes(search.toLowerCase())
  );

  const addItem = (type: string, id: string) => {
    onChange([...selected, { type, target_id: id }]);
    setSearch("");
  };

  const removeItem = (type: string, id: string) => {
    onChange(selected.filter((s) => !(s.type === type && s.target_id === id)));
  };

  // 선택된 항목의 이름 찾기
  const getName = (item: LearningPrereq): string => {
    if (item.type === "course") {
      return courses.find((c) => c.id === item.target_id)?.title ?? item.target_id;
    }
    return activeLessons.find((l) => l.id === item.target_id)?.title ?? item.target_id;
  };

  return (
    <div>
      <label className="mb-1.5 block text-sm font-medium">
        {t("learningPrerequisites")}
      </label>
      <p className="mb-2 text-xs text-text-muted">{t("learningPrerequisitesDesc")}</p>

      {/* 선택된 항목 */}
      {selected.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2">
          {selected.map((item) => (
            <span
              key={`${item.type}-${item.target_id}`}
              className="inline-flex items-center gap-1.5 rounded-full bg-accent-primary/10 px-3 py-1 text-xs font-medium text-accent-primary"
            >
              <span className="rounded bg-accent-primary/20 px-1 text-[10px] uppercase">
                {item.type === "course" ? "코스" : "레슨"}
              </span>
              {getName(item)}
              <button
                type="button"
                onClick={() => removeItem(item.type, item.target_id)}
                className="text-accent-primary/60 hover:text-accent-error"
              >
                &times;
              </button>
            </span>
          ))}
        </div>
      )}

      {/* 타입 토글 */}
      <div className="mb-2 flex gap-2">
        <button
          type="button"
          onClick={() => { setTypeFilter("course"); setSelectedCourseId(""); }}
          className={`rounded-lg px-3 py-1 text-xs font-medium transition ${
            typeFilter === "course"
              ? "bg-accent-primary text-white"
              : "bg-bg-elevated text-text-muted hover:text-text-primary"
          }`}
        >
          코스
        </button>
        <button
          type="button"
          onClick={() => setTypeFilter("lesson")}
          className={`rounded-lg px-3 py-1 text-xs font-medium transition ${
            typeFilter === "lesson"
              ? "bg-accent-primary text-white"
              : "bg-bg-elevated text-text-muted hover:text-text-primary"
          }`}
        >
          레슨
        </button>
      </div>

      {/* 레슨 모드: 코스 선택 드롭다운 */}
      {typeFilter === "lesson" && (
        <select
          value={selectedCourseId}
          onChange={(e) => setSelectedCourseId(e.target.value)}
          className="mb-2 w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
        >
          <option value="">코스를 먼저 선택하세요...</option>
          {courses.map((c) => (
            <option key={c.id} value={c.id}>{c.title}</option>
          ))}
        </select>
      )}

      {/* 검색 + 드롭다운 */}
      <div className="relative">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onBlur={() => setTimeout(() => setSearch(""), 200)}
          placeholder={typeFilter === "course" ? "코스 검색..." : "레슨 검색..."}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm focus:border-accent-primary focus:outline-none"
        />
        {typeFilter === "course" && search && filteredCourses.length > 0 && (
          <div className="absolute z-20 mt-1 max-h-40 w-full overflow-y-auto rounded-lg border border-border bg-bg-surface shadow-lg">
            {filteredCourses.slice(0, 10).map((c) => (
              <button
                key={c.id}
                type="button"
                onMouseDown={() => addItem("course", c.id)}
                className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-text-secondary hover:bg-bg-elevated hover:text-text-primary"
              >
                <span className="rounded bg-bg-elevated px-1 text-[10px] text-text-muted uppercase">코스</span>
                <span>{c.title}</span>
              </button>
            ))}
          </div>
        )}
        {typeFilter === "lesson" && selectedCourseId && filteredLessons.length > 0 && (
          <div className="absolute z-20 mt-1 max-h-40 w-full overflow-y-auto rounded-lg border border-border bg-bg-surface shadow-lg">
            {filteredLessons.map((l) => (
              <button
                key={l.id}
                type="button"
                onMouseDown={() => addItem("lesson", l.id)}
                className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-text-secondary hover:bg-bg-elevated hover:text-text-primary"
              >
                <span className="rounded bg-bg-elevated px-1 text-[10px] text-text-muted uppercase">레슨</span>
                <span>{l.title}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
