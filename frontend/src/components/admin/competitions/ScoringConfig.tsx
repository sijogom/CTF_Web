"use client";

import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import type { WargameChallenge } from "./CompetitionChallengeManager";

interface Props {
  selectedChal: WargameChallenge;
  scoringType: string;
  onScoringTypeChange: (value: string) => void;
  initialValue: number;
  onInitialValueChange: (value: number) => void;
  decay: number;
  onDecayChange: (value: number) => void;
  minimumValue: number;
  onMinimumValueChange: (value: number) => void;
  onAdd: () => void;
  adding: boolean;
}

export function ScoringConfig({
  selectedChal,
  scoringType,
  onScoringTypeChange,
  initialValue,
  onInitialValueChange,
  decay,
  onDecayChange,
  minimumValue,
  onMinimumValueChange,
  onAdd,
  adding,
}: Props) {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const td = useTranslations("difficulty");

  return (
    <div className="space-y-3 rounded-lg border border-accent-primary/30 bg-accent-primary/5 p-3">
      <div className="flex items-center gap-2 text-sm">
        <span className="font-medium">{selectedChal.name}</span>
        <span className="rounded bg-bg-elevated px-1.5 py-0.5 text-xs">
          {selectedChal.category}
        </span>
        <span className="text-xs text-text-muted">
          {td(selectedChal.difficulty)}
        </span>
      </div>

      <div>
        <label className="mb-1 block text-xs text-text-muted">{t("scoringType")}</label>
        <div className="flex gap-2">
          {(["static", "dynamic"] as const).map((st) => (
            <button
              key={st}
              type="button"
              onClick={() => onScoringTypeChange(st)}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                scoringType === st
                  ? "bg-accent-primary text-white"
                  : "border border-border bg-bg-primary text-text-secondary hover:text-text-primary"
              }`}
            >
              {st === "static" ? t("scoringStatic") : t("scoringDynamic")}
            </button>
          ))}
        </div>
      </div>

      {scoringType === "dynamic" ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div>
            <label className="mb-1 block text-xs text-text-muted">{t("initialValue")}</label>
            <input
              type="number"
              value={initialValue}
              onChange={(e) => onInitialValueChange(parseInt(e.target.value) || 0)}
              min={1}
              className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-text-muted">{t("decayCount")}</label>
            <input
              type="number"
              value={decay}
              onChange={(e) => onDecayChange(parseInt(e.target.value) || 0)}
              min={1}
              className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-text-muted">{t("minimumValue")}</label>
            <input
              type="number"
              value={minimumValue}
              onChange={(e) => onMinimumValueChange(parseInt(e.target.value) || 0)}
              min={0}
              className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
            />
          </div>
        </div>
      ) : (
        <div className="w-1/3">
          <label className="mb-1 block text-xs text-text-muted">{t("initialValue")}</label>
          <input
            type="number"
            value={initialValue}
            onChange={(e) => onInitialValueChange(parseInt(e.target.value) || 0)}
            min={1}
            className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
          />
        </div>
      )}

      <Button
        onClick={onAdd}
        disabled={adding}
        className="mt-1"
      >
        {adding ? tc("processing") : t("addChallenge")}
      </Button>
    </div>
  );
}
