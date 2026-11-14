"use client";

import { Link } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import type { Challenge } from "@/types/challenge";
import { CategoryBadge } from "@/components/ui/Badge";
import { CategoryIcon } from "@/components/ui/CategoryIcon";

/** 난이도별 스타일 매핑 (8단계) */
const DIFFICULTY_STYLE: Record<string, { label: string; class: string }> = {
  beginner:    { label: "beginner",    class: "diff-beginner bg-emerald-500/10" },
  easy:        { label: "easy",        class: "diff-easy bg-green-500/10" },
  medium_low:  { label: "medium_low",  class: "diff-medium-low bg-lime-500/10" },
  medium:      { label: "medium",      class: "diff-medium bg-yellow-500/10" },
  medium_high: { label: "medium_high", class: "diff-medium-high bg-amber-500/10" },
  hard:        { label: "hard",        class: "diff-hard bg-orange-500/10" },
  expert:      { label: "expert",      class: "diff-expert bg-red-500/10" },
  impossible:  { label: "impossible",  class: "diff-impossible bg-purple-500/10" },
};

/** 카테고리 → 글로우 CSS 클래스 */
const CATEGORY_GLOW: Record<string, string> = {
  web: "card-glow-web",
  pwn: "card-glow-pwn",
  rev: "card-glow-rev",
  crypto: "card-glow-crypto",
  forensic: "card-glow-forensic",
  misc: "card-glow-misc",
};

interface ChallengeCardProps {
  challenge: Challenge;
  solved?: boolean;
}

export function ChallengeCard({ challenge, solved }: ChallengeCardProps) {
  const t = useTranslations("wargame");
  const td = useTranslations("difficulty");

  const catKey = challenge.category.toLowerCase();
  const glowClass = CATEGORY_GLOW[catKey] ?? "card-glow";
  const diffStyle = challenge.difficulty ? DIFFICULTY_STYLE[challenge.difficulty] : null;
  const isLocked = challenge.locked === true;

  return (
    <Link
      href={`/wargame/${challenge.id}`}
      className={`group block rounded-xl border p-5 transition-all ${
        isLocked
          ? "border-border bg-bg-surface opacity-60"
          : solved
            ? "border-accent-success/30 bg-accent-success/5 " + glowClass
            : "border-border bg-bg-surface " + glowClass
      }`}
    >
      {/* 썸네일 */}
      {challenge.thumbnail_url && !isLocked && (
        <div className="mb-3 -mx-5 -mt-5 h-32 overflow-hidden rounded-t-xl">
          <img
            src={challenge.thumbnail_url}
            alt={challenge.name}
            loading="lazy"
            className="h-full w-full object-cover"
          />
        </div>
      )}

      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <CategoryIcon
            category={catKey as "web" | "pwn" | "rev" | "crypto" | "forensic" | "misc"}
            size="sm"
          />
          <CategoryBadge category={challenge.category} />
        </div>
        <div className="flex items-center gap-2">
          {isLocked && (
            <span className="rounded-full bg-text-muted/10 px-2 py-0.5 text-[11px] font-semibold text-text-muted">
              🔒 {t("locked")}
            </span>
          )}
          {isLocked && (challenge.prerequisite_ids?.length ?? 0) + (challenge.learning_prerequisites_detail?.length ?? 0) > 0 && (
            <span className="rounded-full bg-accent-warning/10 px-2 py-0.5 text-[10px] font-medium text-accent-warning">
              {t("prerequisiteCount", { count: (challenge.prerequisite_ids?.length ?? 0) + (challenge.learning_prerequisites_detail?.length ?? 0) })}
            </span>
          )}
          {diffStyle && (
            <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${diffStyle.class}`}>
              {td(diffStyle.label)}
            </span>
          )}
          {solved && (
            <span className="flex items-center gap-1 rounded-full bg-accent-success/20 px-2 py-0.5 text-xs font-semibold text-accent-success">
              <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={3}>
                <polyline points="20 6 9 17 4 12" />
              </svg>
              {t("solved")}
            </span>
          )}
        </div>
      </div>

      <h3 className="mb-2 text-lg font-semibold group-hover:text-accent-primary transition-colors">
        {challenge.name}
      </h3>

      <div className="flex items-center justify-between text-sm text-text-secondary">
        <div className="flex items-center gap-2">
          <span className="text-base font-bold text-accent-warning">
            {challenge.value.toLocaleString()}P
          </span>
          {challenge.xp != null && challenge.xp > 0 && (
            <span className="text-xs font-medium text-accent-primary">
              +{challenge.xp}XP
            </span>
          )}
        </div>
        <span>{t("solveCount", { count: challenge.solves })}</span>
      </div>

      {challenge.tags.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1">
          {challenge.tags.map((tag) => (
            <span
              key={tag}
              className="rounded bg-bg-primary px-1.5 py-0.5 text-xs text-text-muted"
            >
              {tag}
            </span>
          ))}
        </div>
      )}
    </Link>
  );
}
