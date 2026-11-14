"use client";

import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";

const DIFFICULTIES = [
  { key: "", label: "all" },
  { key: "beginner", label: "beginner" },
  { key: "easy", label: "easy" },
  { key: "medium_low", label: "medium_low" },
  { key: "medium", label: "medium" },
  { key: "medium_high", label: "medium_high" },
  { key: "hard", label: "hard" },
  { key: "expert", label: "expert" },
  { key: "impossible", label: "impossible" },
] as const;

const DIFF_COLORS: Record<string, string> = {
  beginner: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  easy: "bg-green-500/20 text-green-400 border-green-500/30",
  medium_low: "bg-lime-500/20 text-lime-400 border-lime-500/30",
  medium: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
  medium_high: "bg-amber-500/20 text-amber-400 border-amber-500/30",
  hard: "bg-orange-500/20 text-orange-400 border-orange-500/30",
  expert: "bg-red-500/20 text-red-400 border-red-500/30",
  impossible: "bg-purple-500/20 text-purple-400 border-purple-500/30",
};

interface DifficultyFilterProps {
  selected: string;
  onSelect: (difficulty: string) => void;
}

export function DifficultyFilter({ selected, onSelect }: DifficultyFilterProps) {
  const tc = useTranslations("common");
  const td = useTranslations("difficulty");

  return (
    <div className="flex flex-wrap gap-1.5">
      {DIFFICULTIES.map(({ key, label }) => {
        const isSelected = key === selected;
        const colorClass = key && isSelected ? DIFF_COLORS[key] : "";

        return (
          <button
            key={key}
            onClick={() => onSelect(key)}
            className={cn(
              "rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors",
              isSelected
                ? key
                  ? colorClass
                  : "bg-accent-primary text-white border-accent-primary"
                : "border-border bg-bg-surface text-text-secondary hover:text-text-primary"
            )}
          >
            {key === "" ? tc("all") : td(label)}
          </button>
        );
      })}
    </div>
  );
}
