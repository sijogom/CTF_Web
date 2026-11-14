"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { apiFetch, ApiError } from "@/lib/api-client";
import { ScoreboardGraph } from "@/components/ctf/ScoreboardGraph";
import { Button } from "@/components/ui/Button";
import { competitionEntryIdentity, competitionEntryLabel, isFixedRosterCompetition, validateCompetitionEntries } from "@/lib/competition-presentation";
import type { CompetitionScoreEntry as ScoreboardEntry, CompetitionScoreboard as ScoreboardData, CompetitionTeamContract } from "@/types/competition";

const REFRESH_INTERVAL = 30_000;

export default function ScoreboardPage() {
  const params = useParams();
  const t = useTranslations("ctf");
  const tc = useTranslations("common");
  const compId = params.id as string;

  const [entries, setEntries] = useState<ScoreboardEntry[]>([]);
  const [isFrozen, setIsFrozen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadedCompId, setLoadedCompId] = useState<string | null>(null);
  const [teamContract, setTeamContract] = useState<CompetitionTeamContract>(null);
  const requestRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const fetchScoreboard = useCallback(async () => {
    const requestId = ++requestRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const data = await apiFetch<ScoreboardData>(`/api/ctf/competitions/${compId}/scoreboard`, { signal: controller.signal });
      validateCompetitionEntries(data.entries, isFixedRosterCompetition(data));
      if (controller.signal.aborted || requestRef.current !== requestId) return;
      setEntries(data.entries);
      setIsFrozen(data.is_frozen);
      setTeamContract(data.team_contract ?? null);
      setLoadedCompId(compId);
      setError(null);
    } catch (err) {
      if (controller.signal.aborted || requestRef.current !== requestId) return;
      setLoadedCompId(compId);
      setError(err instanceof ApiError ? err.message : tc("errorOccurred"));
    } finally {
      if (!controller.signal.aborted && requestRef.current === requestId) setLoading(false);
    }
  }, [compId, tc]);

  useEffect(() => {
    fetchScoreboard();
    const timer = setInterval(fetchScoreboard, REFRESH_INTERVAL);
    return () => { clearInterval(timer); requestRef.current += 1; abortRef.current?.abort(); };
  }, [fetchScoreboard]);

  if (loading || loadedCompId !== compId) {
    return (
      <div className="flex justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }

  if (error) {
    return <div className="py-20 text-center text-status-error" role="alert"><p>{error}</p><Button variant="secondary" className="mt-3" onClick={() => { void fetchScoreboard(); }}>{tc("retry")}</Button></div>;
  }

  const topThree = entries.slice(0, 3);
  const team = teamContract === "fixed_roster_v1";

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      {/* 헤더 */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold">{t("scoreboard")}</h1>
          <p className="mt-1 text-sm text-text-muted">
            {team ? t("teamScoreboard") : t("scoreboardLiveDesc")}
          </p>
        </div>
        <Link
          href={`/ctf/${compId}`}
          className="rounded-lg bg-bg-surface px-4 py-2 text-sm text-text-secondary hover:text-text-primary"
        >
          {t("backToCompetition")}
        </Link>
      </div>

      {/* 프리즈 알림 */}
      {isFrozen && (
        <div className="rounded-lg border border-accent-warning/30 bg-accent-warning/10 p-4 text-center">
          <p className="text-sm font-medium text-accent-warning">
            {t("frozenMessage")}
          </p>
        </div>
      )}

      {/* Top 3 포디움 */}
      {topThree.length > 0 && (
        <div className="flex items-end justify-center gap-4">
          {/* 2등 */}
          {topThree[1] && (
            <div className="flex w-28 flex-col items-center">
              <div className="mb-2 text-3xl">{"\u{1F948}"}</div>
              <div className="w-full rounded-t-xl bg-bg-surface p-4 text-center" style={{ minHeight: "120px" }}>
                <p className="break-words text-sm font-semibold">{competitionEntryLabel(topThree[1], team)}</p>
                <p className="text-lg font-bold text-accent-primary">{topThree[1].score}</p>
                <p className="text-xs text-text-muted">{t("solvesCount", { count: topThree[1].solves })}</p>
              </div>
            </div>
          )}
          {/* 1등 */}
          {topThree[0] && (
            <div className="flex w-32 flex-col items-center">
              <div className="mb-2 text-4xl">{"\u{1F947}"}</div>
              <div className="w-full rounded-t-xl bg-bg-surface p-4 text-center" style={{ minHeight: "160px" }}>
                <p className="break-words text-base font-bold">{competitionEntryLabel(topThree[0], team)}</p>
                <p className="text-2xl font-bold text-accent-primary">{topThree[0].score}</p>
                <p className="text-xs text-text-muted">{t("solvesCount", { count: topThree[0].solves })}</p>
              </div>
            </div>
          )}
          {/* 3등 */}
          {topThree[2] && (
            <div className="flex w-28 flex-col items-center">
              <div className="mb-2 text-3xl">{"\u{1F949}"}</div>
              <div className="w-full rounded-t-xl bg-bg-surface p-4 text-center" style={{ minHeight: "100px" }}>
                <p className="break-words text-sm font-semibold">{competitionEntryLabel(topThree[2], team)}</p>
                <p className="text-lg font-bold text-accent-primary">{topThree[2].score}</p>
                <p className="text-xs text-text-muted">{t("solvesCount", { count: topThree[2].solves })}</p>
              </div>
            </div>
          )}
        </div>
      )}

      {/* 시계열 그래프 */}
      {!isFrozen && <ScoreboardGraph compId={compId} teamContract={teamContract} />}

      {/* 전체 순위 테이블 */}
      <div className="overflow-x-auto rounded-xl border border-border bg-bg-surface" role="region" tabIndex={0} aria-label={t("scoreboard")}>
        <table className="w-full min-w-[36rem]">
          <thead>
            <tr className="border-b border-border text-left text-sm text-text-muted">
              <th className="px-6 py-3 font-medium">{t("rank")}</th>
              <th className="px-6 py-3 font-medium">{team ? t("teamName") : t("participant")}</th>
              <th className="px-6 py-3 text-right font-medium">{t("score")}</th>
              <th className="px-6 py-3 text-right font-medium">{t("firstBloodShort")}</th>
              <th className="px-6 py-3 text-right font-medium">{t("solves")}</th>
            </tr>
          </thead>
          <tbody>
            {entries.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-6 py-12 text-center text-sm text-text-muted">
                  {t("noEntries")}
                </td>
              </tr>
            ) : (
              entries.map((entry) => (
                <tr
                  key={competitionEntryIdentity(entry, team)}
                  className="border-b border-border last:border-0 hover:bg-bg-primary/50"
                >
                  <td className="px-6 py-4">
                    <span className={`font-bold ${entry.rank <= 3 ? "text-accent-primary" : "text-text-muted"}`}>
                      #{entry.rank}
                    </span>
                  </td>
                  <td className="break-words px-6 py-4 font-medium">{competitionEntryLabel(entry, team)}</td>
                  <td className="px-6 py-4 text-right font-semibold text-accent-primary">
                    {isFrozen ? "???" : entry.score}
                  </td>
                  <td className="px-6 py-4 text-right">
                    {(entry.first_bloods ?? 0) > 0 && (
                      <span className="inline-flex items-center gap-1 text-red-400">
                        <span className="text-xs">{"\u{1FA78}"}</span>
                        <span className="text-sm font-semibold">{entry.first_bloods}</span>
                      </span>
                    )}
                  </td>
                  <td className="px-6 py-4 text-right text-text-secondary">
                    {t("solvesCount", { count: entry.solves })}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
