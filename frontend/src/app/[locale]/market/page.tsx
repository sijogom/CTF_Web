"use client";

import { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { apiFetch } from "@/lib/api-client";
import { useAuth } from "@/providers/AuthProvider";
import { richContentToPlainText } from "@/lib/rich-content.mjs";

interface MarketItem {
  id: string;
  title: string;
  description: string;
  price: number;
  category: string;
  seller_name: string;
  download_count: number;
  created_at: string;
}

interface ItemListResponse {
  items: MarketItem[];
  total: number;
  page: number;
  size: number;
}

const CATEGORIES = ["all", "material", "etc"] as const;

export default function MarketPage() {
  const t = useTranslations("market");
  const tc = useTranslations("common");
  const { user } = useAuth();

  const [items, setItems] = useState<MarketItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [category, setCategory] = useState<string>("all");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [myPoints, setMyPoints] = useState<number | null>(null);

  const fetchItems = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({ page: String(page), size: "20" });
      if (category !== "all") params.set("category", category);
      if (search.trim()) params.set("q", search.trim());

      const data = await apiFetch<ItemListResponse>(
        `/api/community/marketplace/items?${params}`
      );
      setItems(data.items);
      setTotal(data.total);
    } catch {
      setError(t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [page, category, search, t]);

  useEffect(() => {
    fetchItems();
  }, [fetchItems]);

  useEffect(() => {
    if (!user) return;
    apiFetch<{ points: number }>("/api/auth/me/points")
      .then((d) => setMyPoints(d.points))
      .catch(() => {});
  }, [user]);

  const totalPages = Math.max(1, Math.ceil(total / 20));

  const categoryLabel = (cat: string) => {
    const map: Record<string, string> = {
      all: t("categoryAll"),
      exam: t("categoryExam"),
      material: t("categoryMaterial"),
      etc: t("categoryEtc"),
    };
    return map[cat] ?? cat;
  };

  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      {/* 헤더 */}
      <div className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-text-primary">{t("title")}</h1>
          <p className="mt-1 text-sm text-text-secondary">{t("description")}</p>
        </div>
        <div className="flex items-center gap-3">
          {myPoints !== null && (
            <span className="rounded-lg border border-accent-warning/30 bg-accent-warning/10 px-3 py-1.5 text-sm font-semibold text-accent-warning">
              {t("myPoints")}: {myPoints.toLocaleString()}P
            </span>
          )}
          {user && (
            <Link
              href="/market/sell"
              className="rounded-lg bg-accent-primary px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent-primary-hover"
            >
              {t("sell")}
            </Link>
          )}
        </div>
      </div>

      {/* 필터 + 검색 */}
      <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="flex gap-1.5">
          {CATEGORIES.map((cat) => (
            <button
              key={cat}
              onClick={() => { setCategory(cat); setPage(1); }}
              className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                category === cat
                  ? "bg-accent-primary text-white"
                  : "bg-bg-surface text-text-secondary hover:text-text-primary"
              }`}
            >
              {categoryLabel(cat)}
            </button>
          ))}
        </div>
        <div className="flex-1">
          <input
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(1); }}
            placeholder={t("searchItems")}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2 text-sm focus:border-accent-primary focus:outline-none sm:max-w-xs"
          />
        </div>
      </div>

      {/* 에러 */}
      {error && (
        <div className="mb-4 rounded-lg border border-accent-error/30 bg-accent-error/10 p-3 text-sm text-accent-error">
          {error}
        </div>
      )}

      {/* 로딩 */}
      {loading ? (
        <div className="flex justify-center py-20">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
        </div>
      ) : items.length === 0 ? (
        <div className="py-20 text-center text-text-muted">{t("noItems")}</div>
      ) : (
        <>
          {/* 아이템 그리드 */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {items.map((item) => (
              <Link
                key={item.id}
                href={`/market/${item.id}`}
                className="group rounded-xl border border-border bg-bg-surface p-5 transition-all hover:border-accent-primary/40 hover:shadow-lg"
              >
                <div className="mb-3 flex items-start justify-between">
                  <span className="rounded-md bg-bg-elevated px-2 py-0.5 text-xs font-medium text-text-muted">
                    {categoryLabel(item.category)}
                  </span>
                  <span className="text-lg font-bold text-accent-warning">
                    {item.price.toLocaleString()}P
                  </span>
                </div>
                <h3 className="mb-2 text-base font-semibold text-text-primary group-hover:text-accent-primary">
                  {item.title}
                </h3>
                <p className="mb-3 line-clamp-2 text-sm text-text-secondary">
                  {richContentToPlainText(item.description)}
                </p>
                <div className="flex items-center justify-between text-xs text-text-muted">
                  <span>{item.seller_name}</span>
                  <span>{t("downloads", { count: item.download_count })}</span>
                </div>
              </Link>
            ))}
          </div>

          {/* 페이지네이션 */}
          {totalPages > 1 && (
            <div className="mt-6 flex items-center justify-center gap-2">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="rounded-lg border border-border px-3 py-1.5 text-sm disabled:opacity-40"
              >
                {tc("back")}
              </button>
              <span className="text-sm text-text-secondary">
                {page} / {totalPages}
              </span>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page === totalPages}
                className="rounded-lg border border-border px-3 py-1.5 text-sm disabled:opacity-40"
              >
                {tc("next")}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
