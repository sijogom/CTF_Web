"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useTranslations, useLocale } from "next-intl";
import { useSearchParams } from "next/navigation";
import { CategoryFilter } from "@/components/wargame/CategoryFilter";
import { DifficultyFilter } from "@/components/wargame/DifficultyFilter";
import { ChallengeCard } from "@/components/wargame/ChallengeCard";
import { apiFetch, ApiError } from "@/lib/api-client";
import type { Challenge } from "@/types/challenge";
import { ListPagination } from "@/components/ui/ListPagination";
import { readEnum, readPositiveInt, writeUrlQuery } from "@/lib/url-query";
import { parseListPage } from "@/lib/list-response.mjs";

type SortKey = "solves" | "points" | "newest";

const SORT_KEYS = ["solves", "points", "newest"] as const;
const CATEGORY_KEYS = ["", "web", "pwn", "rev", "crypto", "forensic", "misc"] as const;
const DIFFICULTY_KEYS = ["", "beginner", "easy", "medium_low", "medium", "medium_high", "hard", "expert", "impossible"] as const;
const PAGE_SIZE = 20;

export default function WargamePage() {
  const t = useTranslations("wargame");
  const tc = useTranslations("common");
  const locale = useLocale();
  const searchParams = useSearchParams();
  const [challenges, setChallenges] = useState<Challenge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [category, setCategory] = useState(() => readEnum(searchParams.get("category"), CATEGORY_KEYS, ""));
  const [difficulty, setDifficulty] = useState(() => readEnum(searchParams.get("difficulty"), DIFFICULTY_KEYS, ""));
  const [sortBy, setSortBy] = useState<SortKey>(() => readEnum(searchParams.get("sort"), SORT_KEYS, "solves"));
  const [search, setSearch] = useState(() => searchParams.get("q") ?? "");
  const [debouncedSearch, setDebouncedSearch] = useState(() => searchParams.get("q") ?? "");
  const [page, setPage] = useState(() => readPositiveInt(searchParams.get("page")));
  const [total, setTotal] = useState(0);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const controllerRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);

  const syncFromUrl = useCallback(() => {
    const params = new URLSearchParams(window.location.search);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const nextSearch = params.get("q") ?? "";
    setCategory(readEnum(params.get("category"), CATEGORY_KEYS, ""));
    setDifficulty(readEnum(params.get("difficulty"), DIFFICULTY_KEYS, ""));
    setSortBy(readEnum(params.get("sort"), SORT_KEYS, "solves"));
    setSearch(nextSearch);
    setDebouncedSearch(nextSearch);
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

  const changeCategory = (value: string) => {
    setCategory(value as (typeof CATEGORY_KEYS)[number]);
    setPage(1);
    writeUrlQuery({ category: value || null, page: null }, "push");
  };

  const changeDifficulty = (value: string) => {
    setDifficulty(value as (typeof DIFFICULTY_KEYS)[number]);
    setPage(1);
    writeUrlQuery({ difficulty: value || null, page: null }, "push");
  };

  const changeSort = (value: SortKey) => {
    setSortBy(value);
    setPage(1);
    writeUrlQuery({ sort: value === "solves" ? null : value, page: null }, "push");
  };

  const changePage = (value: number) => {
    setPage(value);
    writeUrlQuery({ page: value === 1 ? null : value }, "push");
  };

  const fetchChallenges = useCallback(async () => {
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
      if (sortBy) params.set("sort", sortBy);
      if (debouncedSearch) params.set("search", debouncedSearch);
      params.set("lang", locale);
      params.set("page", String(page));
      params.set("size", String(PAGE_SIZE));

      const queryString = params.toString();
      const url = `/api/learn/challenges${queryString ? `?${queryString}` : ""}`;
      const res = await apiFetch<{ items: Challenge[]; total: number; page: number; size: number }>(
        url,
        { signal: controller.signal },
      );
      if (controller.signal.aborted || requestId !== requestIdRef.current) return;
      const response = parseListPage<Challenge>(res, page, PAGE_SIZE);
      const totalPages = Math.max(1, Math.ceil(response.total / PAGE_SIZE));
      if (page > totalPages) {
        setPage(totalPages);
        writeUrlQuery({ page: totalPages === 1 ? null : totalPages });
        return;
      }
      setChallenges(response.items);
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
  }, [category, difficulty, sortBy, debouncedSearch, locale, page, t]);

  useEffect(() => {
    fetchChallenges();
  }, [fetchChallenges]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">{t("title")}</h1>
        <p className="mt-2 text-text-secondary">{t("description")}</p>
      </div>

      {/* 검색 + 정렬 */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="search-input-wrapper w-full sm:max-w-xs">
          <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <circle cx="11" cy="11" r="8" />
            <path d="m21 21-4.35-4.35" />
          </svg>
          <input
            value={search}
            onChange={(e) => handleSearchChange(e.target.value)}
            placeholder={`${tc("search")}...`}
            aria-label={tc("search")}
            className="w-full rounded-lg border border-border bg-bg-primary py-2.5 pr-4 text-sm focus:border-accent-primary focus:outline-none"
          />
        </div>
        <div className="flex gap-1.5">
          {(["solves", "points", "newest"] as SortKey[]).map((key) => (
            <button
              key={key}
              onClick={() => changeSort(key)}
              className={`rounded-full px-3.5 py-1.5 text-sm font-medium transition-colors ${
                sortBy === key
                  ? "bg-accent-primary text-white"
                  : "text-text-secondary hover:bg-bg-surface"
              }`}
            >
              {key === "solves" ? t("sortBySolves") : key === "points" ? t("sortByPoints") : t("sortByNewest")}
            </button>
          ))}
        </div>
      </div>

      {/* 카테고리 필터 */}
      <CategoryFilter selected={category} onSelect={changeCategory} />

      {/* 난이도 필터 */}
      <DifficultyFilter selected={difficulty} onSelect={changeDifficulty} />

      {/* 로딩 상태 */}
      {loading && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div
              key={i}
              className="h-40 animate-pulse rounded-xl border border-border bg-bg-surface"
            />
          ))}
        </div>
      )}

      {/* 에러 상태 */}
      {!loading && error && (
        <div className="flex flex-col items-center gap-4 py-16">
          <p className="text-text-muted">{error}</p>
          <button
            onClick={fetchChallenges}
            className="rounded-lg bg-accent-primary px-4 py-2 text-sm font-medium text-white hover:bg-accent-primary/90"
          >
            {tc("retry")}
          </button>
        </div>
      )}

      {/* 데이터 렌더링 */}
      {!loading && !error && (
        <>
          {challenges.length === 0 ? (
            <div className="py-16 text-center text-text-muted">{t("noResults")}</div>
          ) : (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {challenges.map((c) => (
                <ChallengeCard key={c.id} challenge={c} solved={c.solved} />
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
    </div>
  );
}
