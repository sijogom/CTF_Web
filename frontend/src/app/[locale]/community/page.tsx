"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useTranslations, useLocale } from "next-intl";
import { useSearchParams } from "next/navigation";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { apiFetch, ApiError } from "@/lib/api-client";
import { readEnum, readPositiveInt, writeUrlQuery } from "@/lib/url-query";
import { parseListPage } from "@/lib/list-response.mjs";
import { ListPagination } from "@/components/ui/ListPagination";
import { Users } from "lucide-react";

interface Post {
  id: string;
  title: string;
  board_type: string;
  author_name: string;
  language: string;
  like_count: number;
  dislike_count: number;
  comment_count: number;
  view_count: number;
  is_pinned: boolean;
  is_anonymous: boolean;
  is_best: boolean;
  created_at: string;
}

const LANG_LABELS: Record<string, string> = {
  ko: "한국어",
  en: "EN",
  ja: "日本語",
  zh: "中文",
};

interface PostsResponse {
  items: Post[];
  total: number;
  page: number;
  size: number;
}

const BOARD_TYPE_KEYS = [
  { key: "", i18nKey: "boardAll" },
  { key: "free", i18nKey: "boardFree" },
  { key: "qna", i18nKey: "boardQna" },
  { key: "writeup", i18nKey: "boardWriteup" },
  { key: "notice", i18nKey: "boardNotice" },
] as const;

const BOARD_BADGE_CONFIG: Record<string, { i18nKey: string; color: string }> = {
  free: { i18nKey: "boardFree", color: "bg-bg-primary text-text-muted" },
  qna: { i18nKey: "boardQna", color: "bg-accent-primary/10 text-accent-primary" },
  writeup: { i18nKey: "boardWriteup", color: "bg-accent-success/10 text-accent-success" },
  notice: { i18nKey: "boardNotice", color: "bg-accent-warning/10 text-accent-warning" },
};

const PAGE_SIZE = 20;
const BOARD_TYPES = ["", "free", "qna", "writeup", "notice"] as const;

export default function CommunityPage() {
  const t = useTranslations("community");
  const tc = useTranslations("common");
  const tt = useTranslations("teams");
  const locale = useLocale();
  const searchParams = useSearchParams();
  const localeMap: Record<string, string> = { ko: "ko-KR", en: "en-US", ja: "ja-JP", zh: "zh-CN" };
  const dateLocale = localeMap[locale] || "ko-KR";
  const [posts, setPosts] = useState<Post[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [page, setPage] = useState(() => readPositiveInt(searchParams.get("page")));
  const [total, setTotal] = useState(0);

  const [boardType, setBoardType] = useState(() => readEnum(searchParams.get("board"), BOARD_TYPES, ""));
  const [search, setSearch] = useState(() => searchParams.get("q") ?? "");
  const [debouncedSearch, setDebouncedSearch] = useState(() => searchParams.get("q") ?? "");
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);

  const syncFromUrl = useCallback(() => {
    const params = new URLSearchParams(window.location.search);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const nextSearch = params.get("q") ?? "";
    setBoardType(readEnum(params.get("board"), BOARD_TYPES, ""));
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

  const fetchPosts = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams();
      if (boardType) params.set("board_type", boardType);
      if (debouncedSearch) params.set("search", debouncedSearch);
      params.set("lang", locale);
      params.set("page", String(page));
      params.set("size", String(PAGE_SIZE));

      const queryString = params.toString();
      const url = `/api/community/posts?${queryString}`;
      const res = await apiFetch<PostsResponse>(url, { signal: controller.signal });
      if (controller.signal.aborted || requestId !== requestIdRef.current) return;
      const response = parseListPage<Post>(res, page, PAGE_SIZE);
      const responseTotalPages = Math.max(1, Math.ceil(response.total / PAGE_SIZE));
      if (page > responseTotalPages) {
        setPage(responseTotalPages);
        writeUrlQuery({ page: responseTotalPages === 1 ? null : responseTotalPages });
        return;
      }
      setPosts(response.items);
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
  }, [boardType, debouncedSearch, page, locale, t]);

  useEffect(() => {
    fetchPosts();
  }, [fetchPosts]);

  /* 게시판 탭 변경 시 페이지를 1로 리셋 */
  const handleBoardTypeChange = (key: (typeof BOARD_TYPES)[number]) => {
    setBoardType(key);
    setPage(1);
    writeUrlQuery({ board: key || null, page: null }, "push");
  };

  const handleSearchChange = (value: string) => {
    setSearch(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setDebouncedSearch(value.trim());
      setPage(1);
      writeUrlQuery({ q: value.trim() || null, page: null });
    }, 300);
  };

  const changePage = (nextPage: number) => {
    setPage(nextPage);
    writeUrlQuery({ page: nextPage === 1 ? null : nextPage }, "push");
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold">{t("title")}</h1>
          <p className="mt-2 text-text-secondary">{t("description")}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Link href="/teams" className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm font-medium text-text-secondary hover:bg-bg-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary">
            <Users className="h-4 w-4 shrink-0" aria-hidden="true" />{tt("title")}
          </Link>
          <Link href="/community/write">
          <Button>{t("writePost")}</Button>
          </Link>
        </div>
      </div>

      {/* 게시판 탭 + 검색 */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex gap-2">
          {BOARD_TYPE_KEYS.map((bt) => (
            <button
              key={bt.key}
              onClick={() => handleBoardTypeChange(bt.key)}
              className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
                boardType === bt.key
                  ? "bg-accent-primary text-white"
                  : "bg-bg-surface text-text-secondary hover:text-text-primary"
              }`}
            >
              {t(bt.i18nKey)}
            </button>
          ))}
        </div>
        <input
          value={search}
          onChange={(e) => handleSearchChange(e.target.value)}
          placeholder={t("searchPosts")}
          aria-label={t("searchPosts")}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm focus:border-accent-primary focus:outline-none sm:max-w-xs"
        />
      </div>

      {/* 로딩 상태 */}
      {loading && (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <div
              key={i}
              className="h-16 animate-pulse rounded-lg border border-border bg-bg-surface"
            />
          ))}
        </div>
      )}

      {/* 에러 상태 */}
      {!loading && error && (
        <div className="flex flex-col items-center gap-4 py-16">
          <p className="text-text-muted">{error}</p>
          <button
            onClick={fetchPosts}
            className="rounded-lg bg-accent-primary px-4 py-2 text-sm font-medium text-white hover:bg-accent-primary/90"
          >
            {tc("retry")}
          </button>
        </div>
      )}

      {/* 게시글 목록 — 디시인사이드 스타일 테이블 */}
      {!loading && !error && (
        <>
          {posts.length === 0 ? (
            <div className="py-16 text-center text-text-muted">{t("noPosts")}</div>
          ) : (
            <div className="overflow-x-auto rounded-xl border border-border bg-bg-surface">
              {/* 테이블 헤더 */}
              <div className="hidden items-center border-b border-border bg-bg-primary/50 px-4 py-2.5 text-xs font-medium text-text-muted sm:flex">
                <span className="w-16 text-center">{t("boardLabel")}</span>
                <span className="flex-1 pl-2">{t("titleLabel")}</span>
                <span className="w-20 text-center">{t("like")}</span>
                <span className="w-16 text-center">{t("viewCount")}</span>
                <span className="w-28 text-center">{t("createdAt")}</span>
              </div>

              {posts.map((post, i) => {
                const badge = BOARD_BADGE_CONFIG[post.board_type];
                const date = new Date(post.created_at);
                const isToday = new Date().toDateString() === date.toDateString();
                const dateStr = isToday
                  ? date.toLocaleTimeString(dateLocale, { hour: "2-digit", minute: "2-digit" })
                  : date.toLocaleDateString(dateLocale, { month: "2-digit", day: "2-digit" });

                return (
                  <Link
                    key={post.id}
                    href={`/community/${post.id}`}
                    className={`flex items-center px-4 py-3 transition-colors hover:bg-bg-primary/50 ${
                      i < posts.length - 1 ? "border-b border-border" : ""
                    } ${post.is_pinned ? "bg-accent-warning/5" : ""}`}
                  >
                    {/* 게시판 배지 */}
                    <span className="w-16 shrink-0 text-center">
                      {badge && (
                        <span className={`inline-block rounded-full px-2 py-0.5 text-[10px] font-medium ${badge.color}`}>
                          {t(badge.i18nKey)}
                        </span>
                      )}
                    </span>

                    {/* 언어 뱃지 */}
                    <span className="w-14 shrink-0 text-center">
                      <span className="inline-block rounded bg-bg-primary px-1.5 py-0.5 text-[10px] font-medium text-text-muted">
                        {LANG_LABELS[post.language] ?? post.language}
                      </span>
                    </span>

                    {/* 제목 영역 */}
                    <div className="flex flex-1 items-center gap-1.5 overflow-hidden pl-1">
                      {post.is_pinned && <span className="shrink-0 text-xs text-accent-warning">📌</span>}
                      {post.is_best && (
                        <span className="shrink-0 rounded bg-accent-primary/20 px-1 py-0.5 text-[10px] font-bold text-accent-primary">
                          {t("bestPost")}
                        </span>
                      )}
                      <span className={`truncate text-sm font-medium ${post.is_pinned ? "text-accent-warning" : "text-text-primary"}`}>
                        {post.title}
                      </span>
                      {post.comment_count > 0 && (
                        <span className="shrink-0 text-xs font-medium text-accent-primary">
                          [{post.comment_count}]
                        </span>
                      )}
                      <span className="ml-auto shrink-0 text-[11px] text-text-muted sm:hidden">
                        {dateStr}
                      </span>
                    </div>

                    {/* 추천 (좋아요 - 싫어요 합산) */}
                    {(() => {
                      const net = post.like_count - post.dislike_count;
                      return (
                        <span className={`hidden w-20 shrink-0 text-center text-xs font-medium sm:block ${
                          net > 0 ? "text-accent-error" : net < 0 ? "text-blue-400" : "text-text-muted"
                        }`}>
                          {net > 0 ? `+${net}` : String(net)}
                        </span>
                      );
                    })()}

                    {/* 조회 */}
                    <span className="hidden w-16 shrink-0 text-center text-xs text-text-muted sm:block">
                      {post.view_count}
                    </span>

                    {/* 작성일 */}
                    <span className="hidden w-28 shrink-0 text-center text-xs text-text-muted sm:block">
                      {dateStr}
                    </span>
                  </Link>
                );
              })}
            </div>
          )}

          {/* 페이지네이션 */}
          <ListPagination
            page={page}
            pageSize={PAGE_SIZE}
            total={total}
            onPageChange={changePage}
          />
        </>
      )}
    </div>
  );
}
