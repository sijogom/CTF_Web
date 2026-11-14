"use client";

import { useTranslations } from "next-intl";
import { CATEGORIES } from "../challenges/ChallengeFilters";
import type { WargameChallenge } from "./CompetitionChallengeManager";

interface Props {
  chalSearch: string;
  onSearchChange: (value: string) => void;
  chalCategoryFilter: string;
  onCategoryChange: (value: string) => void;
  filteredWargame: WargameChallenge[];
  selectedChalId: number | null;
  onSelect: (id: number | null) => void;
  wargameError: boolean;
  wargameLoaded: boolean;
}

export function ChallengeSelector({
  chalSearch,
  onSearchChange,
  chalCategoryFilter,
  onCategoryChange,
  filteredWargame,
  selectedChalId,
  onSelect,
  wargameError,
  wargameLoaded,
}: Props) {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const td = useTranslations("difficulty");

  return (
    <>
      <div className="flex gap-2">
        <input
          type="text"
          value={chalSearch}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder={t("searchChallenges")}
          className="flex-1 rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
        />
        <select
          value={chalCategoryFilter}
          onChange={(e) => onCategoryChange(e.target.value)}
          aria-label={t("allCategories")}
          className="rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
        >
          <option value="all">{t("allCategories")}</option>
          {CATEGORIES.map((cat) => (
            <option key={cat} value={cat}>{cat.charAt(0).toUpperCase() + cat.slice(1)}</option>
          ))}
        </select>
      </div>

      <div className="max-h-48 overflow-y-auto rounded-lg border border-border bg-bg-primary">
        {filteredWargame.length === 0 ? (
          <p className="px-3 py-4 text-center text-xs text-text-muted">
            {wargameError ? tc("loadFailed") : !wargameLoaded ? tc("loading") : t("noSearchResults")}
          </p>
        ) : (
          filteredWargame.map((wc) => (
            <button
              key={wc.id}
              type="button"
              onClick={() => onSelect(selectedChalId === wc.id ? null : wc.id)}
              className={`flex w-full items-center gap-3 border-b border-border px-3 py-2.5 text-left text-sm transition-colors last:border-b-0 ${
                selectedChalId === wc.id
                  ? "bg-accent-primary/10 text-accent-primary"
                  : "hover:bg-bg-surface/50"
              }`}
            >
              <span className="flex-1 font-medium">{wc.name}</span>
              <span className="rounded bg-bg-elevated px-1.5 py-0.5 text-xs text-text-muted">{wc.category}</span>
              <span className="text-xs text-text-muted">{td(wc.difficulty)}</span>
              <span className="text-xs text-text-muted">{wc.value}{tc("pointsUnit")}</span>
              {!wc.is_published && (
                <span className="rounded bg-accent-warning/10 px-1.5 py-0.5 text-xs text-accent-warning">{t("unpublished")}</span>
              )}
            </button>
          ))
        )}
      </div>
    </>
  );
}
