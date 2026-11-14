"use client";

import { useTranslations } from "next-intl";
import type { AdminChallenge } from "@/types/challenge";

interface ChallengeDeleteModalProps {
  challenge: AdminChallenge | null;
  step: number;
  deleting: boolean;
  onConfirm: () => void;
  onClose: () => void;
}

export function ChallengeDeleteModal({
  challenge,
  step,
  deleting,
  onConfirm,
  onClose,
}: ChallengeDeleteModalProps) {
  const t = useTranslations("admin");
  const tc = useTranslations("common");

  if (!challenge || step <= 0) return null;

  const deleteStepMessages = [
    "",
    t("chalDeleteConfirm1"),
    t("chalDeleteConfirm2"),
    t("chalDeleteConfirm3"),
  ];

  return (
    <>
      <div
        className="fixed inset-0 z-50 bg-black/50 backdrop-blur-sm"
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="chal-delete-title"
        className="fixed left-1/2 top-1/2 z-50 w-[90vw] max-w-md -translate-x-1/2 -translate-y-1/2 rounded-xl border border-border bg-bg-surface p-6 shadow-2xl"
      >
        {/* 경고 아이콘 */}
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-accent-error/20">
          <svg
            className="h-7 w-7 text-accent-error"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z"
            />
          </svg>
        </div>

        <h3
          id="chal-delete-title"
          className="mb-2 text-center text-lg font-bold text-text-primary"
        >
          {t("chalDeleteTitle")}
        </h3>

        {/* 대상 챌린지 정보 */}
        <div className="mb-4 rounded-lg bg-bg-primary p-3 text-sm">
          <p className="font-medium text-text-primary">{challenge.name}</p>
          <div className="mt-1 flex items-center gap-2 text-text-muted">
            <span className="rounded bg-bg-surface px-1.5 py-0.5 text-xs uppercase">
              {challenge.category}
            </span>
            <span>
              {challenge.value}
              {tc("pointsUnit")}
            </span>
            <span>
              {challenge.solves} {t("colSolves")}
            </span>
          </div>
        </div>

        {/* 단계별 확인 메시지 */}
        <p className="mb-4 text-sm font-medium text-accent-error">
          [{step}/3] {deleteStepMessages[step]}
        </p>

        {/* 진행도 바 */}
        <div className="mb-4 h-1.5 rounded-full bg-bg-primary">
          <div
            className="h-full rounded-full bg-accent-error transition-all"
            style={{ width: `${(step / 3) * 100}%` }}
          />
        </div>

        <div className="flex gap-3">
          <button
            onClick={onClose}
            className="flex-1 rounded-lg border border-border py-2.5 text-sm font-medium text-text-secondary hover:bg-bg-elevated"
          >
            {t("cancelDelete")}
          </button>
          <button
            onClick={onConfirm}
            disabled={deleting}
            className="flex-1 rounded-lg bg-accent-error py-2.5 text-sm font-semibold text-white transition-colors hover:bg-accent-error/80 disabled:opacity-50"
          >
            {deleting
              ? t("chalDeleting")
              : step < 3
                ? t("deleteNextStep", { step: step + 1 })
                : t("deleteFinal")}
          </button>
        </div>
      </div>
    </>
  );
}
