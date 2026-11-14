"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import { useTranslations } from "next-intl";
import { apiFetch, ApiError } from "@/lib/api-client";
import { Button } from "@/components/ui/Button";
import { competitionEntryIdentity, competitionEntryLabel, mergeCompetitionSeries } from "@/lib/competition-presentation";
import type { CompetitionGraph, CompetitionGraphSeries, CompetitionTeamContract } from "@/types/competition";

/** 상위 10명 색상 팔레트 */
const LINE_COLORS = [
  "#3b82f6", // blue
  "#ef4444", // red
  "#22c55e", // green
  "#f59e0b", // amber
  "#8b5cf6", // violet
  "#ec4899", // pink
  "#06b6d4", // cyan
  "#f97316", // orange
  "#14b8a6", // teal
  "#6366f1", // indigo
];

interface ScoreboardGraphProps {
  compId: string;
  teamContract?: CompetitionTeamContract;
}

export function ScoreboardGraph({ compId, teamContract = null }: ScoreboardGraphProps) {
  const t = useTranslations("ctf");
  const tc = useTranslations("common");
  const [series, setSeries] = useState<CompetitionGraphSeries[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const requestRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const key = `${compId}:${teamContract}`;
  const team = teamContract === "fixed_roster_v1";

  const fetchGraph = useCallback(async () => {
    const requestId = ++requestRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const data = await apiFetch<CompetitionGraph>(
        `/api/ctf/competitions/${compId}/scoreboard/graph`, { signal: controller.signal },
      );
      if ((data.team_contract ?? null) !== teamContract) throw new Error("competition_graph_mode_changed");
      mergeCompetitionSeries(data.series, team);
      if (controller.signal.aborted || requestRef.current !== requestId) return;
      setSeries(data.series);
      setError(null);
      setLoadedKey(key);
    } catch (err) {
      if (controller.signal.aborted || requestRef.current !== requestId) return;
      setError(err instanceof ApiError ? err.message : t("graphLoadFailed"));
      setLoadedKey(key);
    } finally {
      if (!controller.signal.aborted && requestRef.current === requestId) setLoading(false);
    }
  }, [compId, key, t, team, teamContract]);

  useEffect(() => {
    void fetchGraph();
    const timer = setInterval(fetchGraph, 30_000);
    return () => { clearInterval(timer); requestRef.current += 1; abortRef.current?.abort(); };
  }, [fetchGraph]);

  if (loading || loadedKey !== key) {
    return (
      <div className="flex h-64 items-center justify-center rounded-xl border border-border bg-bg-surface">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }

  if (error) return <div role="alert" className="border-y border-border py-4 text-sm text-accent-error"><p>{error}</p><Button type="button" variant="secondary" className="mt-2" onClick={() => { void fetchGraph(); }}>{tc("retry")}</Button></div>;
  if (series.length === 0) return null;

  const merged = mergeCompetitionSeries(series, team);
  if (merged.length === 0) return null;

  return (
    <div className="rounded-xl border border-border bg-bg-surface p-6">
      <h2 className="mb-4 text-lg font-semibold">{t("scoreGraph")}</h2>
      <ResponsiveContainer width="100%" height={320}>
        <LineChart data={merged}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
          <XAxis
            dataKey="time"
            tickFormatter={(value) => new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
            tick={{ fontSize: 11, fill: "var(--color-text-muted)" }}
            interval="preserveStartEnd"
          />
          <YAxis
            tick={{ fontSize: 11, fill: "var(--color-text-muted)" }}
            width={50}
          />
          <Tooltip
            labelFormatter={(value) => new Date(Number(value)).toLocaleString()}
            contentStyle={{
              background: "var(--color-bg-surface)",
              border: "1px solid var(--color-border)",
              borderRadius: "8px",
              fontSize: "12px",
            }}
          />
          <Legend wrapperStyle={{ fontSize: "12px", overflowWrap: "anywhere", maxWidth: "100%" }} />
          {series.map((s, i) => (
            <Line
              key={competitionEntryIdentity(s, team)}
              type="stepAfter"
              dataKey={competitionEntryIdentity(s, team)}
              name={competitionEntryLabel(s, team)}
              stroke={LINE_COLORS[i % LINE_COLORS.length]}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4 }}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
