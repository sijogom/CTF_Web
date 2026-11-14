"use client";

import {
  type KeyboardEvent,
  type MouseEvent,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { Button } from "@/components/ui/Button";

export interface RetentionDeletionImpact {
  target_id: string;
  target_name: string;
  is_deleted: boolean;
  related_counts: Record<string, number>;
  recovery_available: boolean;
  deleted_at?: string | null;
}

export interface RetentionDeletionLabels {
  title: string;
  resourceLabel: string;
  retainedDescription: string;
  impactTitle: string;
  impactLoading: string;
  impactEmpty: string;
  reasonLabel: string;
  reasonPlaceholder: string;
  targetNameLabel: string;
  targetNameHelp: string;
  next: string;
  cancel: string;
  execute: string;
  executing: string;
  stageLabel: string;
}

interface RetentionDeletionDialogProps {
  open: boolean;
  target: { id: string; name: string } | null;
  impact: RetentionDeletionImpact | null;
  loadingImpact: boolean;
  submitting: boolean;
  labels: RetentionDeletionLabels;
  impactLabels: Record<string, string>;
  onClose: () => void;
  onSubmit: (input: { reason: string; targetName: string }) => Promise<void> | void;
}

/**
 * 관리 대상 보관 삭제의 UI 3단계. 서버가 최종 토큰과 권한을 검증하므로 이 컴포넌트의
 * 단계만 우회해도 삭제는 실행되지 않는다.
 */
export function RetentionDeletionDialog({
  open,
  target,
  ...props
}: RetentionDeletionDialogProps) {
  if (!open || !target) return null;

  // 닫히면 이 내부 컴포넌트가 unmount된다. 같은 대상을 다시 열어도 1단계와 빈 입력값에서
  // 시작하므로 effect 안에서 상태를 동기화할 필요가 없다.
  return <RetentionDeletionDialogContent key={target.id} target={target} {...props} />;
}

interface RetentionDeletionDialogContentProps
  extends Omit<RetentionDeletionDialogProps, "open" | "target"> {
  target: { id: string; name: string };
}

function RetentionDeletionDialogContent({
  target,
  impact,
  loadingImpact,
  submitting,
  labels,
  impactLabels,
  onClose,
  onSubmit,
}: RetentionDeletionDialogContentProps) {
  const [stage, setStage] = useState<1 | 2 | 3>(1);
  const [reason, setReason] = useState("");
  const [targetName, setTargetName] = useState("");
  const dialogId = useId();
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const targetNameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (stage === 2) reasonRef.current?.focus();
    if (stage === 3) targetNameRef.current?.focus();
  }, [stage]);

  const impactEntries = impact
    ? Object.entries(impact.related_counts).filter(([, count]) => count > 0)
    : [];
  const canAdvanceFromImpact = Boolean(impact) && !loadingImpact && !submitting;
  const canAdvanceFromReason = reason.trim().length > 0 && !submitting;
  const canExecute = targetName === target.name && reason.trim().length > 0 && !submitting;

  const handleClose = () => {
    if (!submitting) onClose();
  };

  const handleFinalSubmit = () => {
    if (!canExecute) return;
    void onSubmit({ reason: reason.trim(), targetName });
  };

  const handleBackdropMouseDown = (event: MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) handleClose();
  };

  const handleDialogKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") handleClose();
  };

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4"
      role="presentation"
      onMouseDown={handleBackdropMouseDown}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${dialogId}-title`}
        aria-describedby={`${dialogId}-description`}
        className="w-full max-w-lg rounded-xl border border-border bg-bg-primary p-6 shadow-2xl"
        onKeyDown={handleDialogKeyDown}
      >
        <div className="mb-5 flex items-start justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-wide text-accent-error">
              {labels.resourceLabel}
            </p>
            <h3 id={`${dialogId}-title`} className="mt-1 text-lg font-semibold text-text-primary">
              {labels.title}
            </h3>
            <p id={`${dialogId}-description`} className="mt-2 text-sm text-text-secondary">
              {labels.retainedDescription}
            </p>
          </div>
          <span className="rounded-full bg-accent-error/10 px-2.5 py-1 text-xs font-semibold text-accent-error">
            {labels.stageLabel.replace("{stage}", String(stage))}
          </span>
        </div>

        <div className="mb-5 flex gap-1" aria-hidden="true">
          {[1, 2, 3].map((value) => (
            <div
              key={value}
              className={`h-1.5 flex-1 rounded-full ${value <= stage ? "bg-accent-error" : "bg-border"}`}
            />
          ))}
        </div>

        <div className="rounded-lg border border-border bg-bg-surface p-4">
          <p className="font-medium text-text-primary">{target.name}</p>

          {stage === 1 && (
            <div className="mt-4">
              <p className="text-sm font-medium text-text-primary">{labels.impactTitle}</p>
              {loadingImpact && <p className="mt-2 text-sm text-text-muted">{labels.impactLoading}</p>}
              {!loadingImpact && impactEntries.length === 0 && (
                <p className="mt-2 text-sm text-text-secondary">{labels.impactEmpty}</p>
              )}
              {!loadingImpact && impactEntries.length > 0 && (
                <dl className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
                  {impactEntries.map(([key, count]) => (
                    <div key={key} className="flex items-center justify-between rounded-md bg-bg-primary px-3 py-2 text-sm">
                      <dt className="text-text-secondary">{impactLabels[key] ?? key}</dt>
                      <dd className="font-semibold text-text-primary">{count}</dd>
                    </div>
                  ))}
                </dl>
              )}
            </div>
          )}

          {stage === 2 && (
            <label className="mt-4 block text-sm font-medium text-text-primary">
              {labels.reasonLabel}
              <textarea
                ref={reasonRef}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder={labels.reasonPlaceholder}
                rows={4}
                maxLength={2000}
                className="mt-2 w-full resize-y rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm font-normal text-text-primary outline-none focus:border-accent-primary"
              />
            </label>
          )}

          {stage === 3 && (
            <label className="mt-4 block text-sm font-medium text-text-primary">
              {labels.targetNameLabel}
              <span className="mt-1 block font-normal text-text-secondary">{labels.targetNameHelp}</span>
              <input
                ref={targetNameRef}
                value={targetName}
                onChange={(event) => setTargetName(event.target.value)}
                aria-invalid={targetName.length > 0 && targetName !== target.name}
                autoComplete="off"
                className="mt-3 w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm font-normal text-text-primary outline-none focus:border-accent-primary"
              />
            </label>
          )}
        </div>

        <div className="mt-6 flex justify-end gap-3">
          <Button variant="ghost" onClick={handleClose} disabled={submitting}>
            {labels.cancel}
          </Button>
          {stage === 1 && (
            <Button onClick={() => setStage(2)} disabled={!canAdvanceFromImpact}>
              {labels.next}
            </Button>
          )}
          {stage === 2 && (
            <Button onClick={() => setStage(3)} disabled={!canAdvanceFromReason}>
              {labels.next}
            </Button>
          )}
          {stage === 3 && (
            <Button
              onClick={handleFinalSubmit}
              disabled={!canExecute}
              className="border-accent-error bg-accent-error text-white hover:bg-accent-error/90"
            >
              {submitting ? labels.executing : labels.execute}
            </Button>
          )}
        </div>
      </section>
    </div>
  );
}
