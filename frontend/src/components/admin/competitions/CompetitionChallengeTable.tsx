"use client";

import { useTranslations } from "next-intl";
import type { CompChallenge, WargameChallenge } from "./CompetitionChallengeManager";

interface Props {
  challenges: CompChallenge[];
  chalNameMap: Map<number, WargameChallenge>;
  onRemove: (challengeId: number) => void;
}

export function CompetitionChallengeTable({ challenges, chalNameMap, onRemove }: Props) {
  const t = useTranslations("admin");
  const tch = useTranslations("challenge");
  const td = useTranslations("difficulty");

  if (challenges.length === 0) {
    return <p className="text-sm text-text-muted">{t("noChallengesAdded")}</p>;
  }

  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead className="bg-bg-surface">
          <tr>
            <th className="px-4 py-2.5 text-left font-medium text-text-secondary">{t("colTitle")}</th>
            <th className="px-4 py-2.5 text-left font-medium text-text-secondary">{tch("category")}</th>
            <th className="px-4 py-2.5 text-left font-medium text-text-secondary">{tch("difficulty")}</th>
            <th className="px-4 py-2.5 text-left font-medium text-text-secondary">{t("scoringType")}</th>
            <th className="px-4 py-2.5 text-right font-medium text-text-secondary">{t("initialValue")}</th>
            <th className="px-4 py-2.5 text-right font-medium text-text-secondary" />
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {challenges.map((c) => {
            const wc = chalNameMap.get(c.challenge_id);
            return (
              <tr key={c.challenge_id} className="hover:bg-bg-surface/50">
                <td className="px-4 py-2.5">
                  <span className="font-medium">{wc?.name ?? `#${c.challenge_id}`}</span>
                </td>
                <td className="px-4 py-2.5">
                  <span className="rounded bg-bg-elevated px-1.5 py-0.5 text-xs">{c.category}</span>
                </td>
                <td className="px-4 py-2.5 text-xs">{td(c.difficulty)}</td>
                <td className="px-4 py-2.5">
                  <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                    c.scoring_type === "dynamic"
                      ? "bg-accent-primary/10 text-accent-primary"
                      : "bg-bg-surface text-text-secondary"
                  }`}>
                    {c.scoring_type === "dynamic" ? t("scoringDynamic") : t("scoringStatic")}
                  </span>
                </td>
                <td className="px-4 py-2.5 text-right font-mono">
                  {c.scoring_type === "dynamic"
                    ? `${c.initial_value} → ${c.minimum_value}`
                    : c.initial_value}
                </td>
                <td className="px-4 py-2.5 text-right">
                  <button
                    onClick={() => onRemove(c.challenge_id)}
                    className="text-xs text-accent-error hover:underline"
                  >
                    {t("removeChallenge")}
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
