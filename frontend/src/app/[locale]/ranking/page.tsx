"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { apiFetch, ApiError } from "@/lib/api-client";

interface RankingEntry {
  rank: number;
  user_id?: string;
  username: string;
  avatar_url: string | null;
  level: number;
  xp: number;
  solves: number;
  streak: number;
  discord_username?: string | null;
  baekjoon_handle?: string | null;
  baekjoon_tier?: number | null;
  tier_name?: string | null;
  tier_image?: string | null;
}

type TabKey = "overall" | "web" | "pwn" | "rev" | "crypto" | "forensic" | "misc" | "weekly" | "monthly";

/** labelKey: ranking namespace key if present, otherwise null (use raw key as label) */
const TABS: { key: TabKey; labelKey: string | null }[] = [
  { key: "overall", labelKey: "overall" },
  { key: "web", labelKey: null },
  { key: "pwn", labelKey: null },
  { key: "rev", labelKey: null },
  { key: "crypto", labelKey: null },
  { key: "forensic", labelKey: null },
  { key: "misc", labelKey: null },
  { key: "weekly", labelKey: "weekly" },
  { key: "monthly", labelKey: "monthly" },
];

export default function RankingPage() {
  const t = useTranslations("ranking");
  const [tab, setTab] = useState<TabKey>("overall");
  const [rankings, setRankings] = useState<RankingEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestGeneration = useRef(0);
  const requestController = useRef<AbortController | null>(null);

  const fetchRankings = useCallback((currentTab: TabKey) => {
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    const generation = ++requestGeneration.current;
    const isCurrent = () => !controller.signal.aborted && generation === requestGeneration.current;
    setLoading(true);
    setError(null);
    apiFetch<RankingEntry[]>(`/api/auth/rankings?tab=${currentTab}`, { signal: controller.signal })
      .then((value) => { if (isCurrent()) setRankings(value); })
      .catch((err) => { if (isCurrent()) setError(err instanceof ApiError ? err.message : t("loadFailed")); })
      .finally(() => { if (isCurrent()) setLoading(false); });
  }, [t]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 데이터 페칭 트리거
    fetchRankings(tab);
    return () => { requestController.current?.abort(); requestGeneration.current += 1; };
  }, [tab, fetchRankings]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">{t("title")}</h1>
        <p className="mt-2 text-text-secondary">{t("description")}</p>
      </div>

      {/* 탭 */}
      <div className="flex flex-wrap gap-2">
        {TABS.map((tabItem) => (
          <button
            key={tabItem.key}
            onClick={() => setTab(tabItem.key)}
            className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
              tab === tabItem.key
                ? "bg-accent-primary text-white"
                : "bg-bg-surface text-text-secondary hover:text-text-primary"
            }`}
          >
            {tabItem.labelKey ? t(tabItem.labelKey) : tabItem.key.charAt(0).toUpperCase() + tabItem.key.slice(1)}
          </button>
        ))}
      </div>

      {/* 로딩 */}
      {loading ? (
        <div className="flex justify-center py-20">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
        </div>
      ) : error ? (
        <div className="py-20 text-center">
          <p className="text-accent-error">{error}</p>
          <button onClick={() => fetchRankings(tab)} className="mt-3 text-sm text-accent-primary hover:underline">{t("retry")}</button>
        </div>
      ) : (
        /* 랭킹 테이블 */
        <div className="overflow-hidden rounded-xl border border-border bg-bg-surface">
          <table className="w-full">
            <thead>
              <tr className="border-b border-border text-left text-sm text-text-muted">
                <th className="w-16 px-6 py-3 font-medium">{t("rank")}</th>
                <th className="px-6 py-3 font-medium">{t("username")}</th>
                <th className="px-6 py-3 text-center font-medium">{t("level")}</th>
                <th className="px-6 py-3 text-right font-medium">{t("xp")}</th>
                <th className="px-6 py-3 text-right font-medium">{t("solves")}</th>
                <th className="px-6 py-3 text-right font-medium">{t("streak")}</th>
              </tr>
            </thead>
            <tbody>
              {rankings.map((entry) => {
                const rankRowClass =
                  entry.rank === 1 ? "rank-gold" :
                  entry.rank === 2 ? "rank-silver" :
                  entry.rank === 3 ? "rank-bronze" : "";
                return (
                  <tr
                    key={entry.rank}
                    className={`border-b border-border transition-colors last:border-0 hover:bg-bg-primary/50 ${rankRowClass}`}
                  >
                    <td className="px-6 py-4">
                      {entry.rank <= 3 ? (
                        <span className={`inline-flex h-7 w-7 items-center justify-center rounded-full text-xs font-bold ${
                          entry.rank === 1 ? "bg-[#FFD700]/15 text-[#FFD700]" :
                          entry.rank === 2 ? "bg-[#C0C0C0]/15 text-[#C0C0C0]" :
                          "bg-[#CD7F32]/15 text-[#CD7F32]"
                        }`}>
                          {entry.rank}
                        </span>
                      ) : (
                        <span className="pl-1 font-mono text-sm text-text-muted">{entry.rank}</span>
                      )}
                    </td>
                    <td className="px-6 py-4">
                      <div className="flex items-center gap-3">
                        {entry.avatar_url ? (
                          <img src={entry.avatar_url} alt={entry.username} className="h-7 w-7 rounded-full object-cover" />
                        ) : (
                          <span className="flex h-7 w-7 items-center justify-center rounded-full bg-accent-primary/10 text-xs font-bold text-accent-primary">
                            {entry.username.charAt(0).toUpperCase()}
                          </span>
                        )}
                        {/* 플랫폼 티어 */}
                        {entry.tier_image && (
                          <img src={entry.tier_image} alt={entry.tier_name || ""} className="h-4 w-4 object-contain" />
                        )}
                        {/* 백준 티어 아이콘 */}
                        {entry.baekjoon_tier != null && entry.baekjoon_tier > 0 && (
                          <img
                            src={`https://static.solved.ac/tier_small/${entry.baekjoon_tier}.svg`}
                            alt={`BOJ ${entry.baekjoon_tier}`}
                            className="h-4 w-4"
                            title={entry.baekjoon_handle || ""}
                          />
                        )}
                        {/* 디스코드 인증마크 */}
                        {entry.discord_username && (
                          <span title={entry.discord_username}>
                            <svg className="h-4 w-4 text-[#5865F2]" viewBox="0 0 24 24" fill="currentColor">
                              <path d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
                            </svg>
                          </span>
                        )}
                        {entry.user_id ? (
                          <Link
                            href={`/profile/${entry.user_id}`}
                            className="font-medium hover:text-accent-primary hover:underline transition-colors"
                          >
                            {entry.username}
                          </Link>
                        ) : (
                          <span className="font-medium">{entry.username}</span>
                        )}
                      </div>
                    </td>
                    <td className="px-6 py-4 text-center">
                      <span className="rounded-full bg-accent-primary/10 px-2.5 py-0.5 text-sm font-medium text-accent-primary">
                        Lv.{entry.level}
                      </span>
                    </td>
                    <td className="px-6 py-4 text-right font-mono font-semibold text-accent-primary">
                      {entry.xp.toLocaleString()}
                    </td>
                    <td className="px-6 py-4 text-right text-text-secondary">
                      {t("solveCount", { count: entry.solves })}
                    </td>
                    <td className="px-6 py-4 text-right">
                      {entry.streak > 0 ? (
                        <span className="text-accent-warning">{t("streakDays", { n: entry.streak })}</span>
                      ) : (
                        <span className="text-text-muted">-</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
