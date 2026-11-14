"use client";

import { useTranslations } from "next-intl";
import type { ChallengeFormData } from "./ChallengeWizard";
import { Button } from "@/components/ui/Button";
import { RichTextEditor } from "@/components/ui/RichTextEditor";

interface Props {
  data: ChallengeFormData;
  onChange: (partial: Partial<ChallengeFormData>) => void;
}

export function StepHints({ data, onChange }: Props) {
  const t = useTranslations("challenge");
  const tc = useTranslations("common");
  const addHint = () => {
    onChange({ hints: [...data.hints, { content: "", cost: 50 }] });
  };

  const updateHint = (index: number, field: "content" | "cost", value: string | number) => {
    const updated = data.hints.map((h, i) =>
      i === index ? { ...h, [field]: value } : h
    );
    onChange({ hints: updated });
  };

  const removeHint = (index: number) => {
    onChange({ hints: data.hints.filter((_, i) => i !== index) });
  };

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">{t("hintsTitle")}</h2>
          <p className="text-sm text-text-secondary">
            {t("hintsDesc")}
          </p>
        </div>
        <Button variant="secondary" onClick={addHint}>
          {t("addHint")}
        </Button>
      </div>

      {data.hints.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-text-muted">
          {t("noHints")}
        </div>
      ) : (
        <div className="space-y-4">
          {data.hints.map((hint, i) => (
            <div
              key={i}
              className="rounded-lg border border-border bg-bg-surface p-4"
            >
              <div className="mb-3 flex items-center justify-between">
                <span className="text-sm font-medium">{t("hintN", { n: i + 1 })}</span>
                <button
                  onClick={() => removeHint(i)}
                  className="text-sm text-accent-error hover:underline"
                >
                  {tc("delete")}
                </button>
              </div>
              <div className="mb-3">
                <RichTextEditor
                  value={hint.content}
                  onChange={(value) => updateHint(i, "content", value)}
                  placeholder={t("hintPlaceholder")}
                  ariaLabel={t("hintPlaceholder")}
                />
              </div>
              <div className="flex items-center gap-2">
                <label htmlFor={`hint-cost-${i}`} className="text-sm text-text-secondary">
                  {t("deductPoints")}
                </label>
                <input
                  id={`hint-cost-${i}`}
                  type="number"
                  value={hint.cost}
                  onChange={(e) => updateHint(i, "cost", parseInt(e.target.value) || 0)}
                  className="w-24 rounded-lg border border-border bg-bg-primary px-3 py-1.5 text-sm"
                />
                <span className="text-sm text-text-muted">{tc("pointsUnit")}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
