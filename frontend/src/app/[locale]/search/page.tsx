"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { useSearchParams } from "next/navigation";
import { apiFetch } from "@/lib/api-client";
import { richContentToPlainText } from "@/lib/rich-content.mjs";
import { readEnum, writeUrlQuery } from "@/lib/url-query";
import {
  listItems,
  searchBatch,
  settleSearchSources,
  type SearchSource,
  type SearchSourceTask,
} from "@/lib/search-results";

type SearchCategory = "all" | "challenges" | "courses" | "posts";
type SearchResultType = SearchSource;

const SEARCH_CATEGORIES = ["all", "challenges", "courses", "posts"] as const;

interface SearchResult {
  id: string;
  type: SearchResultType;
  title: string;
  description?: string;
  category?: string;
  difficulty?: string;
  points?: number;
  author?: string;
}

interface CompletedSearch {
  query: string;
  category: SearchCategory;
  results: SearchResult[];
  failedTypes: SearchResultType[];
  totals: Partial<Record<SearchResultType, number>>;
  attempt: number;
}

const CATEGORY_ICON: Record<string, string> = {
  challenge: "M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z",
  course: "M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253",
  post: "M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z",
};

const TYPE_COLOR: Record<string, string> = {
  challenge: "text-category-web",
  course: "text-accent-success",
  post: "text-accent-primary",
};

const TYPE_HREF: Record<string, (id: string) => string> = {
  challenge: (id) => `/wargame/${id}`,
  course: (id) => `/learn/${id}`,
  post: (id) => `/community/${id}`,
};

const TYPE_LIST_HREF: Record<SearchResultType, (query: string) => string> = {
  challenge: (query) => `/wargame?q=${encodeURIComponent(query)}`,
  course: (query) => `/learn?view=list&q=${encodeURIComponent(query)}`,
  post: (query) => `/community?q=${encodeURIComponent(query)}`,
};

export default function SearchPage() {
  const t = useTranslations("common");
  const searchParams = useSearchParams();
  const initialQuery = searchParams.get("q") ?? "";
  const initialCategory = readEnum(searchParams.get("category"), SEARCH_CATEGORIES, "all");

  const [query, setQuery] = useState(initialQuery);
  const [debouncedQuery, setDebouncedQuery] = useState(initialQuery);
  const [category, setCategory] = useState<SearchCategory>(initialCategory);
  const [completedSearch, setCompletedSearch] = useState<CompletedSearch>({
    query: "",
    category: "all",
    results: [],
    failedTypes: [],
    totals: {},
    attempt: 0,
  });
  const [retryNonce, setRetryNonce] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(null);
  const requestIdRef = useRef(0);

  const categories: { value: SearchCategory; label: string }[] = [
    { value: "all", label: t("searchCategoryAll") },
    { value: "challenges", label: t("searchCategoryChallenges") },
    { value: "courses", label: t("searchCategoryCourses") },
    { value: "posts", label: t("searchCategoryPosts") },
  ];

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const syncFromUrl = useCallback(() => {
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
    requestIdRef.current += 1;
    const params = new URLSearchParams(window.location.search);
    const nextQuery = params.get("q") ?? "";
    const nextCategory = readEnum(params.get("category"), SEARCH_CATEGORIES, "all");
    setQuery(nextQuery);
    setDebouncedQuery(nextQuery);
    setCategory(nextCategory);
    if (!nextQuery.trim()) {
      setCompletedSearch({ query: "", category: nextCategory, results: [], failedTypes: [], totals: {}, attempt: retryNonce });
    }
  }, [retryNonce]);

  useEffect(() => {
    window.addEventListener("popstate", syncFromUrl);
    return () => {
      window.removeEventListener("popstate", syncFromUrl);
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [syncFromUrl]);

  const handleQueryChange = useCallback((value: string) => {
    setQuery(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (!value.trim()) {
      requestIdRef.current += 1;
      setDebouncedQuery("");
      setCompletedSearch({ query: "", category, results: [], failedTypes: [], totals: {}, attempt: retryNonce });
      return;
    }
    debounceRef.current = setTimeout(() => setDebouncedQuery(value), 300);
  }, [category, retryNonce]);

  const handleCategoryChange = useCallback((value: SearchCategory) => {
    setCategory(value);
    writeUrlQuery(
      { q: debouncedQuery.trim() || null, category: value === "all" ? null : value },
      "push",
    );
  }, [debouncedQuery]);

  useEffect(() => {
    writeUrlQuery({
      q: debouncedQuery.trim() || null,
      category: category === "all" ? null : category,
    });
  }, [category, debouncedQuery]);

  useEffect(() => {
    if (!debouncedQuery.trim()) {
      requestIdRef.current += 1;
      return;
    }

    const controller = new AbortController();
    const requestId = ++requestIdRef.current;
    const encodedQuery = encodeURIComponent(debouncedQuery.trim());
    const tasks: Array<SearchSourceTask<SearchResult>> = [];

    if (category === "all" || category === "challenges") {
      tasks.push({
        source: "challenge",
        request: apiFetch<{
          items: Array<{
            id: number;
            name: string;
            category?: string;
            difficulty?: string;
            value?: number;
          }>;
          total: number;
        }>(`/api/learn/challenges?search=${encodedQuery}&size=10`, { signal: controller.signal })
          .then((res) => {
            const batch = searchBatch(res);
            return {
              total: batch.total,
              items: batch.items.map((challenge) => ({
                id: String(challenge.id),
                type: "challenge" as const,
                title: challenge.name,
                category: challenge.category,
                difficulty: challenge.difficulty,
                points: challenge.value,
              })),
            };
          }),
      });
    }

    if (category === "all" || category === "courses") {
      tasks.push({
        source: "course",
        request: apiFetch<{
          items: Array<{
          id: string;
          title: string;
          difficulty?: string;
          }>;
          total: number;
        }>(
          `/api/learn/courses?search=${encodedQuery}&size=10&include_pagination=true`,
          { signal: controller.signal },
        ).then((res) => {
          const batch = searchBatch(res);
          return {
            total: batch.total,
            items: listItems(res).map((course) => ({
              id: String(course.id),
              type: "course" as const,
              title: course.title,
              difficulty: course.difficulty,
            })),
          };
        }),
      });
    }

    if (category === "all" || category === "posts") {
      tasks.push({
        source: "post",
        request: apiFetch<{ items: Array<{ id: string; title: string; author_name?: string }>; total: number }>(
          `/api/community/posts?search=${encodedQuery}&size=10`,
          { signal: controller.signal },
        ).then((res) => {
          const batch = searchBatch(res);
          return {
            total: batch.total,
            items: batch.items.map((post) => ({
              id: String(post.id),
              type: "post" as const,
              title: post.title,
              author: post.author_name,
            })),
          };
        }),
      });
    }

    void settleSearchSources(tasks).then(({ results, failedSources, totals }) => {
      if (controller.signal.aborted || requestId !== requestIdRef.current) return;
      setCompletedSearch({
        query: debouncedQuery,
        category,
        results,
        failedTypes: failedSources,
        totals,
        attempt: retryNonce,
      });
    });

    return () => {
      controller.abort();
    };
  }, [debouncedQuery, category, retryNonce]);

  const loading = Boolean(debouncedQuery.trim()) && (
    completedSearch.query !== debouncedQuery
    || completedSearch.category !== category
    || completedSearch.attempt !== retryNonce
  );
  const filteredResults = loading ? [] : completedSearch.results;
  const requestedTypeCount = category === "all" ? 3 : 1;
  const allFailed = !loading && completedSearch.failedTypes.length === requestedTypeCount;
  const partialFailure = !loading && completedSearch.failedTypes.length > 0 && !allFailed;
  const failedSourceLabels = completedSearch.failedTypes.map((type) => (
    type === "challenge"
      ? t("searchCategoryChallenges")
      : type === "course"
        ? t("searchCategoryCourses")
        : t("searchCategoryPosts")
  )).join(", ");
  const successfulTotal = Object.values(completedSearch.totals).reduce((sum, total) => sum + total, 0);
  const sourceLinks = (Object.entries(completedSearch.totals) as Array<[SearchResultType, number]>)
    .filter(([, total]) => total > 0);

  return (
    <div className="mx-auto max-w-3xl px-4 py-8 sm:px-6">
      {/* 검색 헤더 */}
      <h1 className="mb-6 text-2xl font-bold text-text-primary">{t("searchTitle")}</h1>

      {/* 검색 입력 */}
      <div className="relative mb-6">
        <svg
          className="absolute left-4 top-1/2 h-5 w-5 -translate-y-1/2 text-text-muted"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
        >
          <circle cx="11" cy="11" r="8" />
          <path d="M21 21l-4.35-4.35" />
        </svg>
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => handleQueryChange(e.target.value)}
          placeholder={t("searchPlaceholder")}
          className="w-full rounded-xl border border-border bg-bg-surface py-3.5 pl-12 pr-4 text-base text-text-primary placeholder:text-text-muted focus:border-accent-primary focus:outline-none focus:ring-1 focus:ring-accent-primary"
        />
        {query && (
          <button
            onClick={() => {
              if (debounceRef.current) clearTimeout(debounceRef.current);
              setQuery("");
              setDebouncedQuery("");
              requestIdRef.current += 1;
              setCompletedSearch({ query: "", category, results: [], failedTypes: [], totals: {}, attempt: retryNonce });
              writeUrlQuery({ q: null });
              inputRef.current?.focus();
            }}
            className="absolute right-4 top-1/2 -translate-y-1/2 rounded p-1 text-text-muted hover:text-text-primary"
            aria-label={t("clear")}
          >
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        )}
      </div>

      {/* 카테고리 필터 */}
      <div className="mb-6 flex max-w-full flex-wrap gap-2">
        {categories.map(({ value, label }) => (
          <button
            key={value}
            onClick={() => handleCategoryChange(value)}
            className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
              category === value
                ? "bg-accent-primary text-white"
                : "bg-bg-surface text-text-secondary hover:bg-bg-elevated hover:text-text-primary"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* 결과 영역 */}
      {loading ? (
        <div className="flex justify-center py-16">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
        </div>
      ) : !debouncedQuery.trim() ? (
        /* 빈 상태 — 검색어 입력 안내 */
        <div className="flex flex-col items-center justify-center py-20 text-center">
          <svg className="mb-4 h-16 w-16 text-text-muted/40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <circle cx="11" cy="11" r="8" />
            <path d="M21 21l-4.35-4.35" />
          </svg>
          <p className="text-lg font-medium text-text-secondary">{t("searchEmpty")}</p>
          <p className="mt-1 text-sm text-text-muted">Ctrl+K</p>
        </div>
      ) : allFailed ? (
        <div role="alert" className="flex flex-col items-center justify-center gap-4 py-20 text-center">
          <p className="text-lg font-medium text-text-secondary">{t("searchAllFailed")}</p>
          <button
            type="button"
            onClick={() => setRetryNonce((value) => value + 1)}
            className="rounded-lg bg-accent-primary px-4 py-2 text-sm font-medium text-white hover:bg-accent-primary/90"
          >
            {t("searchRetry")}
          </button>
        </div>
      ) : filteredResults.length === 0 ? (
        /* 결과 없음 */
        <div className="flex flex-col items-center justify-center py-20 text-center">
          <svg className="mb-4 h-16 w-16 text-text-muted/40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <circle cx="11" cy="11" r="8" />
            <path d="M21 21l-4.35-4.35" />
            <path d="M8 11h6" />
          </svg>
          <p className="text-lg font-medium text-text-secondary">
            {t("searchNoResults", { query: debouncedQuery })}
          </p>
        </div>
      ) : (
        /* 검색 결과 */
        <div>
          <p className="mb-4 text-sm text-text-muted">
            {t("searchDisplayedCount", { shown: filteredResults.length, total: successfulTotal })}
          </p>
          <div className="space-y-2">
            {filteredResults.map((result) => (
              <Link
                key={`${result.type}-${result.id}`}
                href={TYPE_HREF[result.type](result.id)}
                className="group flex items-start gap-3 rounded-xl border border-border bg-bg-surface p-4 transition-colors hover:border-accent-primary/40 hover:bg-bg-elevated"
              >
                {/* 타입 아이콘 */}
                <div className={`mt-0.5 flex-shrink-0 ${TYPE_COLOR[result.type]}`}>
                  <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d={CATEGORY_ICON[result.type]} />
                  </svg>
                </div>

                {/* 내용 */}
                <div className="min-w-0 flex-1">
                  <h3 className="text-sm font-semibold text-text-primary group-hover:text-accent-primary">
                    {result.title}
                  </h3>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-text-muted">
                    <span className="rounded bg-bg-elevated px-1.5 py-0.5 font-medium">
                      {result.type === "challenge" ? t("searchCategoryChallenges")
                        : result.type === "course" ? t("searchCategoryCourses")
                        : t("searchCategoryPosts")}
                    </span>
                    {result.category && (
                      <span>{result.category}</span>
                    )}
                    {result.difficulty && (
                      <span>{result.difficulty}</span>
                    )}
                    {result.points !== undefined && (
                      <span>{result.points}{t("pointsUnit")}</span>
                    )}
                    {result.author && (
                      <span>{result.author}</span>
                    )}
                  </div>
                  {result.description && (
                    <p className="mt-1.5 line-clamp-2 text-xs text-text-secondary">
                      {richContentToPlainText(result.description)}
                    </p>
                  )}
                </div>
              </Link>
            ))}
          </div>
          {sourceLinks.length > 0 && (
            <div className="mt-5 flex flex-wrap gap-2" aria-label={t("searchAllListsLabel")}>
              {sourceLinks.map(([type, total]) => {
                const label = type === "challenge"
                  ? t("searchCategoryChallenges")
                  : type === "course"
                    ? t("searchCategoryCourses")
                    : t("searchCategoryPosts");
                return (
                  <Link
                    key={type}
                    href={TYPE_LIST_HREF[type](debouncedQuery.trim())}
                    className="rounded-lg border border-border bg-bg-surface px-3 py-2 text-sm font-medium text-text-secondary hover:border-accent-primary/40 hover:text-accent-primary"
                  >
                    {t("searchViewAllCategory", { category: label, count: total })}
                  </Link>
                );
              })}
            </div>
          )}
        </div>
      )}

      {partialFailure && (
        <p role="status" className="mt-4 rounded-lg border border-accent-warning/30 bg-accent-warning/10 p-3 text-sm text-text-secondary">
          {t("searchPartialFailure", { sources: failedSourceLabels })}
        </p>
      )}
    </div>
  );
}
