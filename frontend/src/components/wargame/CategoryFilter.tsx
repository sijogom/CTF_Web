"use client";

import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";

const CATEGORIES = [
  "All",
  "Web",
  "Pwn",
  "Rev",
  "Crypto",
  "Forensic",
  "Misc",
] as const;

interface CategoryFilterProps {
  selected: string;
  onSelect: (category: string) => void;
}

export function CategoryFilter({ selected, onSelect }: CategoryFilterProps) {
  const tc = useTranslations("common");
  return (
    <div className="flex flex-wrap gap-2">
      {CATEGORIES.map((cat) => (
        <button
          key={cat}
          onClick={() => onSelect(cat === "All" ? "" : cat.toLowerCase())}
          className={cn(
            "rounded-lg px-4 py-2 text-sm font-medium transition-colors",
            (cat === "All" && !selected) || cat.toLowerCase() === selected
              ? "bg-accent-primary text-white"
              : "bg-bg-surface text-text-secondary hover:text-text-primary"
          )}
        >
          {cat === "All" ? tc("all") : cat}
        </button>
      ))}
    </div>
  );
}
