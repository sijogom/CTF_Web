"use client";

import { useTranslations } from "next-intl";
import type { SolveRecord } from "@/types/profile";

interface SolveHistoryProps {
  solves: SolveRecord[];
}

export function SolveHistory({ solves }: SolveHistoryProps) {
  const t = useTranslations("profile");
  const tc = useTranslations("common");

  return (
    <div className="rounded-xl border border-border bg-bg-surface p-6">
      <h2 className="mb-4 text-lg font-semibold">
        {t("solveHistory")} ({solves.length})
      </h2>
      {solves.length === 0 ? (
        <p className="py-8 text-center text-sm text-text-muted">
          {t("noSolveHistory")}
        </p>
      ) : (
        <div className="space-y-2">
          {solves.map((solve) => {
            const solvedDate = new Date(solve.solved_at);
            const dateStr = `${solvedDate.getFullYear()}.${String(solvedDate.getMonth() + 1).padStart(2, "0")}.${String(solvedDate.getDate()).padStart(2, "0")}`;
            return (
              <div
                key={solve.id}
                className="flex items-center justify-between rounded-lg border border-border bg-bg-primary px-4 py-3"
              >
                <div className="flex items-center gap-3">
                  <span className="rounded bg-bg-surface px-2 py-0.5 text-xs font-medium text-text-muted">
                    {solve.category}
                  </span>
                  <span className="font-medium">{solve.name}</span>
                </div>
                <div className="flex items-center gap-4 text-sm text-text-muted">
                  <span className="font-medium text-accent-primary">
                    {solve.value}
                    {tc("pointsUnit")}
                  </span>
                  <span>{dateStr}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
