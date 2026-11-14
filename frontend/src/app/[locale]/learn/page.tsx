"use client";

import { useState, useEffect, useCallback, useMemo, useRef, lazy, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { CourseCard } from "@/components/learn/CourseCard";
import { LearningRecommendations } from "@/components/learn/LearningRecommendations";
import { knowledgeExtensionsEnabled } from "@/lib/knowledge-release-stage.mjs";
import { LearningEntryNavigation } from "@/components/learn/LearningEntryNavigation";
import { CategoryFilter } from "@/components/wargame/CategoryFilter";
import { apiFetch, ApiError } from "@/lib/api-client";
import type { KnowledgeGraphInitialFocus } from "@/components/learn/KnowledgeGraphMap";
import { ListPagination } from "@/components/ui/ListPagination";
import { readEnum, readPositiveInt, writeUrlQuery } from "@/lib/url-query";
import { parseListPage } from "@/lib/list-response.mjs";
import { getLearnHydrationSearch, parseMapFocus } from "@/lib/learn-route-state.mjs";

const KnowledgeGraphMap = lazy(() =>
  import("@/components/learn/KnowledgeGraphMap").then((m) => ({ default: m.KnowledgeGraphMap }))
);

interface Course {
  id: string;
  title: string;
  category: string;
  difficulty: string;
  estimated_hours: number;
  lesson_count: number;
  progress_percent: number;
  thumbnail_url?: string;
}

const LEARN_DIFFICULTIES = [
  { key: "", label: "all" },
  { key: "introductory", label: "introductory" },
  { key: "basic", label: "basic" },
  { key: "intermediate", label: "intermediate" },
  { key: "advanced", label: "advanced" },
] as const;

const LEARN_CATEGORIES = ["", "web", "pwn", "rev", "crypto", "forensic", "misc"] as const;
const LEARN_DIFFICULTY_KEYS = LEARN_DIFFICULTIES.map((item) => item.key);
const PAGE_SIZE = 20;

interface CourseListResponse {
  items: Course[];
  total: number;
  page: number;
  size: number;
}

const LEARN_DIFFICULTY_STYLES: Record<string, string> = {
  introductory: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  basic: "bg-sky-500/20 text-sky-400 border-sky-500/30",
  intermediate: "bg-amber-500/20 text-amber-400 border-amber-500/30",
  advanced: "bg-red-500/20 text-red-400 border-red-500/30",
};

export default function LearnPage() {
  const t = useTranslations("learn");
  const td = useTranslations("difficulty");
  const tc = useTranslations("common");
  const searchParams = useSearchParams();
  const routeSearch = searchParams.toString();
  // Client navigation의 첫 Learn 렌더에서는 브라우저 주소가 아직 이전
  // 페이지일 수 있으므로, 화면의 기준은 라우터가 전달한 query로 둔다.
  const initialMapFocus = useMemo(() => parseMapFocus(routeSearch), [routeSearch]);
  const routeRequestsMap = useMemo(
    () => new URLSearchParams(routeSearch).get("view") === "map",
    [routeSearch],
  );
  const initialParams = useMemo(() => new URLSearchParams(routeSearch), [routeSearch]);
  const [courses, setCourses] = useState<Course[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [category, setCategory] = useState(() => readEnum(initialParams.get("category"), LEARN_CATEGORIES, ""));
  const [difficulty, setDifficulty] = useState(() => readEnum(initialParams.get("difficulty"), LEARN_DIFFICULTY_KEYS, ""));
  const [search, setSearch] = useState(() => initialParams.get("q") ?? "");
  const [debouncedSearch, setDebouncedSearch] = useState(() => initialParams.get("q") ?? "");
  const [page, setPage] = useState(() => readPositiveInt(initialParams.get("page")));
  const [total, setTotal] = useState(0);
  const [mapFocus, setMapFocus] = useState<KnowledgeGraphInitialFocus | null>(initialMapFocus);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);
  // Next의 client navigation에서 searchParams 반영이 한 프레임 늦어져도
  // 버튼을 누른 즉시 화면을 전환한다. URL이 따라오면 override를 해제한다.
  const [viewOverride, setViewOverride] = useState<"list" | "map" | null>(null);

  const visibleViewMode = viewOverride ?? (routeRequestsMap ? "map" : "list");

  // 일부 hydration 경로에서만 라우터 query가 첫 프레임에 비어 있을 수
  // 있다. mount 뒤 주소가 실제 Learn 경로임을 확인한 경우에만 현재 URL로
  // 초기 상태를 보완해, client navigation 전 페이지의 query는 읽지 않는다.
  useEffect(() => {
    const hydrationSearch = getLearnHydrationSearch(
      routeSearch,
      window.location.pathname,
      window.location.search,
    );
    if (hydrationSearch === null) return;

    const params = new URLSearchParams(hydrationSearch);
    const nextSearch = params.get("q") ?? "";
    if (debounceRef.current) clearTimeout(debounceRef.current);
    setCategory(readEnum(params.get("category"), LEARN_CATEGORIES, ""));
    setDifficulty(readEnum(params.get("difficulty"), LEARN_DIFFICULTY_KEYS, ""));
    setSearch(nextSearch);
    setDebouncedSearch(nextSearch);
    setPage(readPositiveInt(params.get("page")));
    setMapFocus(parseMapFocus(hydrationSearch));
    setViewOverride(params.get("view") === "map" ? "map" : "list");
  }, [routeSearch]);

  useEffect(() => {
    if (viewOverride === null || routeSearch === "") return;
    const urlView = routeRequestsMap ? "map" : "list";
    if (urlView === viewOverride) setViewOverride(null);
  }, [routeRequestsMap, routeSearch, viewOverride]);

  // 탭은 페이지 내부의 전환 상태가 기준이다. Next 라우터 이동으로
  // 페이지를 다시 만들면 검색·필터·맵 선택 상태가 함께 끊기므로,
  // 주소만 history에 반영하고 실제 화면은 즉시 로컬 상태로 바꾼다.
  const switchView = useCallback(
    (nextView: "list" | "map") => {
      setViewOverride(nextView);
      writeUrlQuery({ view: nextView === "map" ? "map" : null }, "push");
    },
    [],
  );

  // 브라우저 뒤로/앞으로 가기에서도 같은 내부 전환 상태를 유지한다.
  useEffect(() => {
    const handlePopState = () => {
      const params = new URLSearchParams(window.location.search);
      const nextView = params.get("view") === "map"
        ? "map"
        : "list";
      setViewOverride(nextView);
      if (debounceRef.current) clearTimeout(debounceRef.current);
      const nextSearch = params.get("q") ?? "";
      setCategory(readEnum(params.get("category"), LEARN_CATEGORIES, ""));
      setDifficulty(readEnum(params.get("difficulty"), LEARN_DIFFICULTY_KEYS, ""));
      setSearch(nextSearch);
      setDebouncedSearch(nextSearch);
      setPage(readPositiveInt(params.get("page")));
      setMapFocus(parseMapFocus(window.location.search));
    };
    window.addEventListener("popstate", handlePopState);
    return () => {
      window.removeEventListener("popstate", handlePopState);
      if (debounceRef.current) clearTimeout(debounceRef.current);
      controllerRef.current?.abort();
    };
  }, []);

  const handleSearchChange = (value: string) => {
    setSearch(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setDebouncedSearch(value.trim());
      setPage(1);
      writeUrlQuery({ q: value.trim() || null, page: null });
    }, 300);
  };

  const changeCategory = (value: string) => {
    setCategory(value as (typeof LEARN_CATEGORIES)[number]);
    setPage(1);
    writeUrlQuery({ category: value || null, page: null }, "push");
  };

  const changeDifficulty = (value: string) => {
    setDifficulty(value as (typeof LEARN_DIFFICULTY_KEYS)[number]);
    setPage(1);
    writeUrlQuery({ difficulty: value || null, page: null }, "push");
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
      if (category) params.set("category", category);
      if (difficulty) params.set("difficulty", difficulty);
      if (debouncedSearch) params.set("search", debouncedSearch);
      params.set("page", String(page));
      params.set("size", String(PAGE_SIZE));
      params.set("include_pagination", "true");

      const queryString = params.toString();
      const url = `/api/learn/courses${queryString ? `?${queryString}` : ""}`;
      const res = await apiFetch<CourseListResponse>(url, { signal: controller.signal });
      if (controller.signal.aborted || requestId !== requestIdRef.current) return;
      const response = parseListPage<Course>(res, page, PAGE_SIZE);
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
      setError(
        err instanceof ApiError
          ? err.message
          : t("loadFailed"),
      );
    } finally {
      if (!controller.signal.aborted && requestId === requestIdRef.current) setLoading(false);
    }
  }, [category, difficulty, debouncedSearch, page, t]);

  useEffect(() => {
    // URL이 `?view=map`이어도 내부 탭에서 목록으로 전환할 수 있다.
    // pushState는 Next의 searchParams를 즉시 갱신하지 않으므로
    // routeRequestsMap만 보면 목록 전환 직후 코스 조회를 영원히 건너뛴다.
    if (visibleViewMode !== "list") return;
    void fetchCourses();
  }, [fetchCourses, visibleViewMode]);

  return (
    <div className="relative z-0 min-w-0 space-y-6">
      <div className="flex min-w-0 flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-3xl font-bold">{t("title")}</h1>
          <p className="mt-2 text-text-secondary">{t("description")}</p>
        </div>
        <nav
          aria-label={t("title")}
          role="tablist"
          className="relative z-10 flex w-fit shrink-0 rounded-lg border border-border bg-bg-surface p-1 shadow-sm"
        >
          <button
            type="button"
            onClick={() => switchView("list")}
            id="learn-list-tab"
            role="tab"
            aria-selected={visibleViewMode === "list"}
            aria-controls="learn-list-panel"
            aria-current={visibleViewMode === "list" ? "page" : undefined}
            className={`rounded-md px-4 py-2 text-sm font-semibold transition-colors ${
              visibleViewMode === "list"
                ? "bg-accent-primary text-white"
                : "text-text-secondary hover:bg-bg-primary hover:text-text-primary"
            }`}
          >
            {t("viewList")}
          </button>
          <button
            type="button"
            onClick={() => switchView("map")}
            id="learn-map-tab"
            role="tab"
            aria-selected={visibleViewMode === "map"}
            aria-controls="learn-map-panel"
            aria-current={visibleViewMode === "map" ? "page" : undefined}
            className={`rounded-md px-4 py-2 text-sm font-semibold transition-colors ${
              visibleViewMode === "map"
                ? "bg-accent-primary text-white"
                : "text-text-secondary hover:bg-bg-primary hover:text-text-primary"
            }`}
          >
            {t("viewMap")}
          </button>
        </nav>
      </div>

      {visibleViewMode === "list" && <LearningEntryNavigation />}

      {visibleViewMode === "list" && (
        <div
          id="learn-list-panel"
          role="tabpanel"
          aria-labelledby="learn-list-tab"
          tabIndex={-1}
          className="relative z-0 min-w-0 space-y-4"
        >
          {/* 목록 화면은 워게임과 같은 검색·필터 레이아웃을 사용한다. */}
          <div className="relative z-0 search-input-wrapper w-full sm:max-w-xs">
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <circle cx="11" cy="11" r="8" />
              <path d="m21 21-4.35-4.35" />
            </svg>
            <input
              value={search}
              onChange={(event) => handleSearchChange(event.target.value)}
              placeholder={`${tc("search")}...`}
              aria-label={tc("search")}
              className="w-full rounded-lg border border-border bg-bg-primary py-2.5 pr-4 text-sm focus:border-accent-primary focus:outline-none"
            />
          </div>

          <CategoryFilter selected={category} onSelect={changeCategory} />

          <div className="flex flex-wrap gap-1.5">
            {LEARN_DIFFICULTIES.map(({ key, label }) => (
              <button
                key={key || "all"}
                type="button"
                onClick={() => changeDifficulty(key)}
                className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
                  key === difficulty
                    ? key
                      ? LEARN_DIFFICULTY_STYLES[key]
                      : "border-accent-primary bg-accent-primary text-white"
                    : "border-border bg-bg-surface text-text-secondary hover:text-text-primary"
                }`}
              >
                {key === "" ? tc("all") : td(label)}
              </button>
            ))}
          </div>

        {loading && (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <div
                key={i}
                className="h-44 animate-pulse rounded-xl border border-border bg-bg-surface"
              />
            ))}
          </div>
        )}

        {/* 에러 상태 */}
        {!loading && error && (
          <div className="flex flex-col items-center gap-4 py-16">
            <p className="text-text-muted">{error}</p>
            <button
              onClick={fetchCourses}
              className="rounded-lg bg-accent-primary px-4 py-2 text-sm font-medium text-white hover:bg-accent-primary/90"
            >
              {tc("retry")}
            </button>
          </div>
        )}

        {/* 코스 그리드 */}
        {!loading && !error && (
          <>
            {courses.length === 0 ? (
              <div className="py-16 text-center text-text-muted">
                {tc("noData")}
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {courses.map((c) => (
                  <CourseCard key={c.id} course={c} />
                ))}
              </div>
            )}
          </>
        )}
        {!loading && !error && (
          <ListPagination
            page={page}
            pageSize={PAGE_SIZE}
            total={total}
            onPageChange={changePage}
          />
        )}
        {knowledgeExtensionsEnabled() && <LearningRecommendations />}
        </div>
      )}

      {visibleViewMode === "map" && (
        <div
          id="learn-map-panel"
          role="tabpanel"
          aria-labelledby="learn-map-tab"
          tabIndex={-1}
          className="relative z-0 isolate min-w-0 space-y-3"
        >
            <Suspense
              fallback={
                <div className="h-[min(68svh,680px)] min-h-[460px] rounded-xl border border-border bg-bg-surface sm:h-[min(78vh,820px)] sm:min-h-[620px]" aria-busy="true" />
              }
            >
              <KnowledgeGraphMap
                initialFocus={mapFocus}
                onFocusChange={(nextFocus) => {
                  setMapFocus(nextFocus);
                  writeUrlQuery(
                    { focus_type: nextFocus.type, focus_id: nextFocus.id },
                    "push",
                  );
                }}
              />
            </Suspense>
        </div>
      )}
    </div>
  );
}
