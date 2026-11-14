"use client";

import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";

interface Hint {
  id: number;
  cost: number;
}

interface HintSectionProps {
  hints: Hint[];
  unlockedHints: Record<number, string>;
  hintLoading: number | null;
  hintConfirmId: number | null;
  onUnlock: (hintId: number) => void;
  onConfirmToggle: (hintId: number | null) => void;
}

export function HintSection({
  hints,
  unlockedHints,
  hintLoading,
  hintConfirmId,
  onUnlock,
  onConfirmToggle,
}: HintSectionProps) {
  const t = useTranslations("wargame");

  if (hints.length === 0) return null;

  return (
    <>
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <h2 className="mb-3 text-lg font-semibold">{t("hint")}</h2>
        <div className="space-y-3">
          {hints.map((hint, i) => (
            <div key={hint.id} className="rounded-lg border border-border bg-bg-primary p-4">
              {unlockedHints[hint.id] ? (
                <div className="text-sm" aria-live="polite">
                  <MarkdownRenderer content={unlockedHints[hint.id]} />
                </div>
              ) : (
                <div className="flex items-center justify-between">
                  <span className="text-sm text-text-muted">
                    {t("hintCost", { n: i + 1, cost: hint.cost })}
                  </span>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={hintLoading === hint.id}
                    onClick={() => onConfirmToggle(hint.id)}
                  >
                    {t("open")}
                  </Button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* 힌트 해금 확인 */}
      <ConfirmModal
        open={hintConfirmId !== null}
        title={t("hintUnlockTitle")}
        description={t("hintUnlockDesc", { cost: hints.find((h) => h.id === hintConfirmId)?.cost ?? 0 })}
        confirmLabel={t("hintUnlockConfirm")}
        variant="default"
        onConfirm={() => { if (hintConfirmId !== null) { onUnlock(hintConfirmId); } onConfirmToggle(null); }}
        onCancel={() => onConfirmToggle(null)}
      />
    </>
  );
}
