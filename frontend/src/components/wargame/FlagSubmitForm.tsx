"use client";

import { useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import type { SubmitResult } from "@/types/challenge";

interface FlagSubmitFormProps {
  challengeId: number;
  solved?: boolean;
  onSubmit: (challengeId: number, flag: string) => Promise<SubmitResult>;
}

interface ChallengeSubmitState {
  flag: string;
  loading: boolean;
  result: SubmitResult | null;
}

const EMPTY_STATE: ChallengeSubmitState = { flag: "", loading: false, result: null };

export function FlagSubmitForm({ challengeId, solved, onSubmit }: FlagSubmitFormProps) {
  const t = useTranslations("wargame");
  const [states, setStates] = useState<Record<number, ChallengeSubmitState>>({});
  const requestIdsRef = useRef(new Map<number, number>());
  const currentState = states[challengeId] ?? EMPTY_STATE;

  const updateChallengeState = (
    targetChallengeId: number,
    update: (current: ChallengeSubmitState) => ChallengeSubmitState,
  ) => {
    setStates((current) => ({
      ...current,
      [targetChallengeId]: update(current[targetChallengeId] ?? EMPTY_STATE),
    }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const submittedChallengeId = challengeId;
    const submittedFlag = currentState.flag.trim();
    if (!submittedFlag || currentState.loading) return;

    const requestId = (requestIdsRef.current.get(submittedChallengeId) ?? 0) + 1;
    requestIdsRef.current.set(submittedChallengeId, requestId);
    updateChallengeState(submittedChallengeId, (current) => ({
      ...current,
      loading: true,
      result: null,
    }));

    try {
      const res = await onSubmit(submittedChallengeId, submittedFlag);
      if (requestIdsRef.current.get(submittedChallengeId) !== requestId) return;
      updateChallengeState(submittedChallengeId, (current) => ({
        ...current,
        flag: res.status === "correct" || res.status === "already_solved" ? "" : current.flag,
        result: res,
      }));
      if (res.status === "correct") {
        requestIdsRef.current.delete(submittedChallengeId);
      }
    } catch {
      if (requestIdsRef.current.get(submittedChallengeId) === requestId) {
        updateChallengeState(submittedChallengeId, (current) => ({
          ...current,
          result: { status: "error", message: t("errorOccurred") },
        }));
      }
    } finally {
      if (
        requestIdsRef.current.get(submittedChallengeId) === requestId
        || !requestIdsRef.current.has(submittedChallengeId)
      ) {
        updateChallengeState(submittedChallengeId, (current) => ({ ...current, loading: false }));
      }
    }
  };

  if (solved) {
    return (
      <div className="rounded-lg border border-accent-success/30 bg-accent-success/10 p-4 text-center">
        <span className="text-lg font-medium text-accent-success">
          {t("alreadySolved")}
        </span>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-3">
      <div className="flex min-w-0 gap-3">
        <input
          value={currentState.flag}
          onChange={(e) => updateChallengeState(challengeId, (current) => ({
            ...current,
            flag: e.target.value,
          }))}
          placeholder={t("flagPlaceholder")}
          aria-label={t("submitFlag")}
          autoComplete="off"
          spellCheck={false}
          className="min-w-0 flex-1 rounded-lg border border-border bg-bg-primary px-4 py-3 font-mono text-sm focus:border-accent-primary focus:outline-none"
        />
        <Button
          type="submit"
          disabled={currentState.loading || !currentState.flag.trim()}
          className="shrink-0"
        >
          {currentState.loading ? t("submitting") : t("submitFlag")}
        </Button>
      </div>

      {currentState.result && (
        <div
          role="status"
          aria-live="polite"
          className={`rounded-lg p-3 text-sm font-medium ${
            currentState.result.status === "correct"
              ? "bg-accent-success/10 text-accent-success"
              : currentState.result.status === "already_solved"
                ? "bg-accent-primary/10 text-accent-primary"
                : currentState.result.status === "score_sync_pending"
                  ? "bg-accent-warning/10 text-accent-warning"
                : "bg-accent-error/10 text-accent-error"
          }`}
        >
          {currentState.result.status === "correct"
            ? t("correct")
            : currentState.result.status === "already_solved"
              ? t("alreadySolved")
              : currentState.result.message || t("incorrect")}
        </div>
      )}
    </form>
  );
}
