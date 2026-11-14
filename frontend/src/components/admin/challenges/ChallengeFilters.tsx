"use client";

import { useTranslations } from "next-intl";

const TYPE_TABS = [
  { value: "", label: "all" },
  { value: "wargame", label: "wargame" },
  { value: "competition", label: "competition" },
] as const;

export const CATEGORIES = [
  "web",
  "pwn",
  "rev",
  "crypto",
  "forensic",
  "misc",
] as const;

interface ChallengeFiltersProps {
  search: string;
  categoryFilter: string;
  typeFilter: string;
  onSearchChange: (value: string) => void;
  onCategoryChange: (value: string) => void;
  onTypeChange: (value: string) => void;
}

export function ChallengeFilters({
  search,
  categoryFilter,
  typeFilter,
  onSearchChange,
  onCategoryChange,
  onTypeChange,
}: ChallengeFiltersProps) {
  const t = useTranslations("admin");
  const tc = useTranslations("common");

  return (
    <>
      {/* 유형 탭 */}
      <div className="flex min-w-0 max-w-full flex-wrap gap-1 rounded-lg border border-border bg-bg-surface p-1">
        {TYPE_TABS.map((tab) => (
          <button
            key={tab.value}
            onClick={() => onTypeChange(tab.value)}
            className={`min-w-0 max-w-full whitespace-normal break-words rounded-md px-4 py-2 text-sm font-medium transition-colors ${
              typeFilter === tab.value
                ? "bg-accent-primary text-white"
                : "text-text-secondary hover:text-text-primary"
            }`}
          >
            {tab.value === "" ? tc("all") : t(`challengeType_${tab.label}`)}
          </button>
        ))}
      </div>

      {/* 검색 + 필터 */}
      <div className="flex min-w-0 max-w-full flex-col gap-3 sm:flex-row">
        <input
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder={t("searchChallenges")}
          aria-label={t("searchChallenges")}
          className="min-w-0 w-full max-w-full flex-1 rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm focus:border-accent-primary focus:outline-none"
        />
        <select
          value={categoryFilter}
          onChange={(e) => onCategoryChange(e.target.value)}
          aria-label={t("allCategories")}
          className="min-w-0 w-full max-w-full rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-sm sm:w-auto"
        >
          <option value="all">{t("allCategories")}</option>
          {CATEGORIES.map((cat) => (
            <option key={cat} value={cat}>
              {cat.charAt(0).toUpperCase() + cat.slice(1)}
            </option>
          ))}
        </select>
      </div>
    </>
  );
}
