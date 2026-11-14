"use client";

import { useTranslations } from "next-intl";
import { useAuth } from "@/providers/AuthProvider";
import { useRouter } from "@/i18n/navigation";
import type { AdminChallenge } from "@/types/challenge";

const DIFFICULTY_COLOR: Record<string, string> = {
  beginner: "text-emerald-400",
  easy: "text-green-400",
  medium_low: "text-lime-400",
  medium: "text-yellow-400",
  medium_high: "text-amber-400",
  hard: "text-orange-400",
  expert: "text-red-400",
  impossible: "text-purple-400",
};

const CELL_LAYOUT = [
  "block min-w-0 px-4 py-2 text-left [overflow-wrap:anywhere]",
  "before:mb-1 before:block before:font-sans before:text-xs before:font-normal before:text-text-muted before:content-[attr(data-label)]",
  "@min-[56rem]/challenges:table-cell @min-[56rem]/challenges:px-3 @min-[56rem]/challenges:py-3 @min-[56rem]/challenges:before:hidden",
].join(" ");

interface ChallengeTableProps {
  challenges: AdminChallenge[];
  loading: boolean;
  actionLoading: number | null;
  onTogglePublish: (challenge: AdminChallenge) => void;
  onDelete: (challenge: AdminChallenge) => void;
}

export function ChallengeTable({
  challenges,
  loading,
  actionLoading,
  onTogglePublish,
  onDelete,
}: ChallengeTableProps) {
  const t = useTranslations("admin");
  const td = useTranslations("difficulty");
  const tc = useTranslations("common");
  const router = useRouter();
  const { isSuperAdmin } = useAuth();

  const difficultyLabel: Record<string, string> = {
    beginner: td("beginner"),
    easy: td("easy"),
    medium_low: td("medium_low"),
    medium: td("medium"),
    medium_high: td("medium_high"),
    hard: td("hard"),
    expert: td("expert"),
    impossible: td("impossible"),
  };

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }

  if (challenges.length === 0) {
    return null;
  }

  return (
    <div className="@container/challenges min-w-0 rounded-lg border border-border">
      <table className="block w-full text-sm @min-[56rem]/challenges:table @min-[56rem]/challenges:table-fixed">
        <thead className="sr-only border-b border-border bg-bg-surface @min-[56rem]/challenges:not-sr-only @min-[56rem]/challenges:table-header-group">
          <tr>
            <th scope="col" className="w-[26%] px-3 py-3 text-left font-medium">
              {t("colTitle")}
            </th>
            <th scope="col" className="w-[12%] px-3 py-3 text-left font-medium">
              {t("challengeTypeLabel")}
            </th>
            <th scope="col" className="w-[10%] px-3 py-3 text-left font-medium">
              {t("colCategory")}
            </th>
            <th scope="col" className="w-[10%] px-3 py-3 text-left font-medium">
              {t("colDifficulty")}
            </th>
            <th scope="col" className="w-[8%] px-3 py-3 text-right font-medium">
              {t("colPoints")}
            </th>
            <th scope="col" className="w-[8%] px-3 py-3 text-right font-medium">
              {t("colSolves")}
            </th>
            <th scope="col" className="w-[12%] px-3 py-3 text-center font-medium">
              {t("colStatus")}
            </th>
            <th scope="col" className="w-[14%] px-3 py-3 text-center font-medium">
              {t("colActions")}
            </th>
          </tr>
        </thead>
        <tbody className="block @min-[56rem]/challenges:table-row-group">
          {challenges.map((c) => (
            <tr
              key={c.id}
              className="grid grid-cols-2 border-b border-border py-2 last:border-0 hover:bg-bg-surface/50 @min-[56rem]/challenges:table-row @min-[56rem]/challenges:py-0"
            >
              <td data-label={t("colTitle")} className={`${CELL_LAYOUT} col-span-2 font-medium`}>{c.name}</td>
              <td data-label={t("challengeTypeLabel")} className={CELL_LAYOUT}>
                <span
                  className={`inline-block max-w-full rounded-full px-2.5 py-0.5 text-xs font-medium ${
                    c.challenge_type === "competition"
                      ? "bg-amber-500/10 text-amber-400"
                      : "bg-accent-primary/10 text-accent-primary"
                  }`}
                >
                  {t(
                    `challengeType_${c.challenge_type === "competition" ? "competition" : "wargame"}`,
                  )}
                </span>
              </td>
              <td data-label={t("colCategory")} className={CELL_LAYOUT}>
                <span className="inline-block max-w-full rounded bg-bg-surface px-2 py-0.5 text-xs font-medium uppercase">
                  {c.category}
                </span>
              </td>
              <td
                data-label={t("colDifficulty")}
                className={`${CELL_LAYOUT} ${DIFFICULTY_COLOR[c.difficulty ?? ""] || ""}`}
              >
                {difficultyLabel[c.difficulty ?? ""] || c.difficulty}
              </td>
              <td data-label={t("colPoints")} className={`${CELL_LAYOUT} font-mono @min-[56rem]/challenges:text-right`}>{c.value}</td>
              <td data-label={t("colSolves")} className={`${CELL_LAYOUT} font-mono @min-[56rem]/challenges:text-right`}>{c.solves}</td>
              <td data-label={t("colStatus")} className={`${CELL_LAYOUT} @min-[56rem]/challenges:text-center`}>
                <button
                  onClick={() => onTogglePublish(c)}
                  disabled={actionLoading === c.id}
                  className={`max-w-full rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                    c.is_published
                      ? "bg-accent-success/10 text-accent-success hover:bg-accent-success/20"
                      : "bg-text-muted/10 text-text-muted hover:bg-text-muted/20"
                  }`}
                >
                  {actionLoading === c.id
                    ? "..."
                    : c.is_published
                      ? t("published")
                      : t("unpublished")}
                </button>
              </td>
              <td data-label={t("colActions")} className={`${CELL_LAYOUT} col-span-2 @min-[56rem]/challenges:text-center`}>
                <div className="flex min-w-0 flex-wrap items-center gap-2 @min-[56rem]/challenges:justify-center">
                  <button
                    onClick={() =>
                      router.push(`/admin/challenges/${c.id}/edit`)
                    }
                    className="max-w-full rounded px-2 py-1 text-xs text-accent-primary hover:bg-accent-primary/10"
                  >
                    {t("chalEdit")}
                  </button>
                  {isSuperAdmin && <button
                    onClick={() => onDelete(c)}
                    className="max-w-full rounded px-2 py-1 text-xs text-accent-error hover:bg-accent-error/10"
                  >
                    {t("deleteChallenge")}
                  </button>}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
