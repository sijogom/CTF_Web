"use client";

import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useTranslations, useLocale } from "next-intl";
import { useRouter } from "next/navigation";
import * as Dialog from "@radix-ui/react-dialog";
import { apiFetch } from "@/lib/api-client";
import { runGuardedNavigation } from "@/lib/use-unsaved-changes-guard";
import {
  listItems,
  searchBatch,
  settleSearchSources,
  type SearchSource,
  type SearchSourceTask,
} from "@/lib/search-results";

interface SearchResult {
  id: string;
  type: "challenge" | "course" | "post";
  title: string;
  href: string;
}

const TYPE_ICONS: Record<string, string> = {
  challenge: "🏴",
  course: "📚",
  post: "💬",
};

export function CommandPalette() {
  const t = useTranslations("common");
  const locale = useLocale();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [failedSources, setFailedSources] = useState<SearchSource[]>([]);
  const [retryNonce, setRetryNonce] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const router = useRouter();

  // Ctrl+K 단축키
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "k") {
        e.preventDefault();
        setOpen((prev) => !prev);
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, []);

  // 빈 쿼리면 결과 표시 안 함 (effect 없이 파생 상태)
  const displayedResults = useMemo(
    () => (query.trim() ? results : []),
    [query, results],
  );

  const handleQueryChange = useCallback((nextQuery: string) => {
    setQuery(nextQuery);
    setSelectedIndex(0);
    setResults([]);
    setFailedSources([]);

    if (!nextQuery.trim()) {
      setResults([]);
      setLoading(false);
    }
  }, []);

  // 검색 debounce — 3개 API 병렬 호출
  useEffect(() => {
    if (!query.trim()) return;

    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setLoading(true);
      const q = encodeURIComponent(query.trim());
      const tasks: Array<SearchSourceTask<SearchResult>> = [
        {
          source: "challenge",
          request: apiFetch<{ items: Array<{ id: string; name: string }>; total: number }>(
            `/api/learn/challenges?search=${q}&size=5`,
            { signal: controller.signal },
          ).then((data) => {
            const batch = searchBatch(data);
            return {
              total: batch.total,
              items: batch.items.map((challenge) => ({
                id: String(challenge.id),
                type: "challenge" as const,
                title: challenge.name,
                href: `/${locale}/wargame/${challenge.id}`,
              })),
            };
          }),
        },
        {
          source: "course",
          request: apiFetch<{ items: Array<{ id: string; title: string }>; total: number }>(
            `/api/learn/courses?search=${q}&size=5&include_pagination=true`,
            { signal: controller.signal },
          ).then((data) => {
            const batch = searchBatch(data);
            return {
              total: batch.total,
              items: listItems(data).map((course) => ({
                id: String(course.id),
                type: "course" as const,
                title: course.title,
                href: `/${locale}/learn/${course.id}`,
              })),
            };
          }),
        },
        {
          source: "post",
          request: apiFetch<{ items: Array<{ id: string; title: string }>; total: number }>(
            `/api/community/posts?search=${q}&size=5`,
            { signal: controller.signal },
          ).then((data) => {
            const batch = searchBatch(data);
            return {
              total: batch.total,
              items: batch.items.map((post) => ({
                id: String(post.id),
                type: "post" as const,
                title: post.title,
                href: `/${locale}/community/${post.id}`,
              })),
            };
          }),
        },
      ];

      const settled = await settleSearchSources(tasks);
      if (!controller.signal.aborted) {
        setResults(settled.results);
        setFailedSources(settled.failedSources);
        setLoading(false);
      }
    }, 300);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, locale, retryNonce]);

  const navigate = useCallback(
    (href: string) => {
      const result = runGuardedNavigation(() => {
        setOpen(false);
        setQuery("");
        setResults([]);
        setFailedSources([]);
        setLoading(false);
        router.push(href);
      }, { onCancel: () => setOpen(true) });
      if (result === "pending") setOpen(false);
    },
    [router]
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.nativeEvent.isComposing) return;
    if (e.key === "ArrowDown") {
      if (displayedResults.length === 0) return;
      e.preventDefault();
      setSelectedIndex((prev) => Math.min(prev + 1, displayedResults.length - 1));
    } else if (e.key === "ArrowUp") {
      if (displayedResults.length === 0) return;
      e.preventDefault();
      setSelectedIndex((prev) => Math.max(prev - 1, 0));
    } else if (e.key === "Enter" && displayedResults[selectedIndex]) {
      e.preventDefault();
      navigate(displayedResults[selectedIndex].href);
    }
  };

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/50 backdrop-blur-sm" />
        <Dialog.Content
          className="fixed left-1/2 top-1/4 z-50 w-full max-w-lg -translate-x-1/2 rounded-xl border border-border bg-bg-surface shadow-2xl"
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            inputRef.current?.focus();
          }}
        >
          <Dialog.Title className="sr-only">{t("searchTitle")}</Dialog.Title>
          <Dialog.Description className="sr-only">
            {t("commandPaletteHint")}
          </Dialog.Description>
          <div className="flex items-center gap-3 border-b border-border px-4 py-3">
            <span className="text-text-muted">🔍</span>
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => handleQueryChange(e.target.value)}
              onKeyDownCapture={handleKeyDown}
              placeholder={t("commandPaletteSearch")}
              role="combobox"
              aria-controls="command-palette-results"
              aria-expanded={displayedResults.length > 0}
              aria-activedescendant={displayedResults[selectedIndex] ? `command-result-${selectedIndex}` : undefined}
              className="flex-1 bg-transparent text-sm text-text-primary placeholder:text-text-muted focus:outline-none"
            />
            <kbd className="rounded border border-border px-1.5 py-0.5 text-xs text-text-muted">
              ESC
            </kbd>
          </div>

          {/* 검색 결과 */}
          {failedSources.length > 0 && failedSources.length < 3 && !loading && (
            <p role="status" className="border-b border-accent-warning/30 bg-accent-warning/10 px-4 py-2 text-xs text-text-secondary">
              {t("searchPartialFailure", {
                sources: failedSources.map((source) => (
                  source === "challenge"
                    ? t("searchCategoryChallenges")
                    : source === "course"
                      ? t("searchCategoryCourses")
                      : t("searchCategoryPosts")
                )).join(", "),
              })}
            </p>
          )}

          {loading ? (
            <div className="flex justify-center p-8" role="status" aria-label={t("loading")}>
              <div className="h-5 w-5 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
            </div>
          ) : displayedResults.length > 0 ? (
            <div id="command-palette-results" role="listbox" className="max-h-80 overflow-y-auto p-2">
              {displayedResults.map((result, i) => (
                <button
                  key={`${result.type}-${result.id}`}
                  id={`command-result-${i}`}
                  role="option"
                  aria-selected={i === selectedIndex}
                  onClick={() => navigate(result.href)}
                  className={`flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left text-sm transition-colors ${
                    i === selectedIndex
                      ? "bg-accent-primary/10 text-accent-primary"
                      : "text-text-secondary hover:bg-bg-elevated"
                  }`}
                >
                  <span>{TYPE_ICONS[result.type]}</span>
                  <span>{result.title}</span>
                </button>
              ))}
            </div>
          ) : failedSources.length === 3 ? (
            <div role="alert" className="flex flex-col items-center gap-3 p-8 text-center text-sm text-text-muted">
              <p>{t("searchAllFailed")}</p>
              <button
                type="button"
                onClick={() => setRetryNonce((value) => value + 1)}
                className="rounded-lg bg-accent-primary px-3 py-2 font-medium text-white"
              >
                {t("searchRetry")}
              </button>
            </div>
          ) : query.trim() ? (
            <div className="p-8 text-center text-sm text-text-muted">
              {t("noSearchResults")}
            </div>
          ) : (
            <div className="p-8 text-center text-sm text-text-muted">
              {t("commandPaletteHint")}
            </div>
          )}
          {query.trim() && !loading && (
            <div className="border-t border-border p-2">
              <button
                type="button"
                onClick={() => navigate(`/${locale}/search?q=${encodeURIComponent(query.trim())}`)}
                className="w-full rounded-lg px-3 py-2 text-center text-sm font-medium text-accent-primary hover:bg-bg-elevated"
              >
                {t("searchViewAll")}
              </button>
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
