"use client";

import {
  type KeyboardEvent,
  type MouseEvent,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { ApiError, apiFetch } from "@/lib/api-client";
import { Button } from "@/components/ui/Button";

type ModerationAction = "hide" | "restore";

export interface ModerationTarget {
  type: "post" | "comment" | "marketplace_item" | "marketplace_comment";
  id: string;
}

interface ModerationImpact {
  target_type: ModerationTarget["type"];
  target_id: string;
  target_name: string;
  target_preview: string | null;
  target_author: string | null;
  is_hidden: boolean;
  is_deleted: boolean;
  impact_scope: string;
  related_counts: Record<string, number>;
  available_actions: ModerationAction[];
}

interface ModerationConfirmation {
  confirmation_token: string;
  expires_at: string;
  target_type: ModerationTarget["type"];
  target_id: string;
  target_name: string;
  impact_scope: string;
  action: ModerationAction;
}

interface ModerationActionResult {
  event_id: string;
  target_type: ModerationTarget["type"];
  target_id: string;
  action: ModerationAction;
  is_hidden: boolean;
  created_at: string;
}

export interface ModerationActionLabels {
  title: string;
  restoreTitle: string;
  resourceLabel: string;
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
  restore: string;
  executing: string;
  stageLabel: string;
  scopeLabel: string;
  deleted: string;
}

interface ModerationActionDialogProps {
  open: boolean;
  target: ModerationTarget | null;
  action: ModerationAction;
  labels: ModerationActionLabels;
  impactLabels: Record<string, string>;
  onClose: () => void;
  onCompleted: (result: ModerationActionResult) => Promise<void> | void;
}

/**
 * 관리자/모더레이터용 서버 강제 3단계 가시성 변경 UI.
 * 확인 토큰은 2단계가 끝난 뒤에만 발급해 즉시 실행 요청에 사용하며, 상태·URL·저장소에 보관하지 않는다.
 */
export function ModerationActionDialog({
  open,
  target,
  action,
  ...props
}: ModerationActionDialogProps) {
  if (!open || !target) return null;
  return <ModerationActionDialogContent key={`${target.type}:${target.id}:${action}`} target={target} action={action} {...props} />;
}

interface ModerationActionDialogContentProps
  extends Omit<ModerationActionDialogProps, "open" | "target" | "action"> {
  target: ModerationTarget;
  action: ModerationAction;
}

function ModerationActionDialogContent({
  target,
  action,
  labels,
  impactLabels,
  onClose,
  onCompleted,
}: ModerationActionDialogContentProps) {
  const [stage, setStage] = useState<1 | 2 | 3>(1);
  const [impact, setImpact] = useState<ModerationImpact | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [targetName, setTargetName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const targetNameRef = useRef<HTMLInputElement>(null);
  const dialogId = useId();

  useEffect(() => {
    let cancelled = false;
    const loadImpact = async () => {
      try {
        const data = await apiFetch<ModerationImpact>(
          `/api/community/moderation/targets/${target.type}/${target.id}/impact`,
        );
        if (cancelled) return;
        if (!data.available_actions.includes(action)) {
          setLoadError(data.is_deleted ? labels.deleted : (action === "hide" ? labels.title : labels.restoreTitle));
          return;
        }
        setImpact(data);
      } catch (error) {
        if (!cancelled) setLoadError(error instanceof ApiError ? error.message : labels.impactLoading);
      }
    };
    void loadImpact();
    return () => { cancelled = true; };
  }, [action, labels.deleted, labels.impactLoading, labels.restoreTitle, labels.title, target.id, target.type]);

  useEffect(() => {
    if (stage === 2) reasonRef.current?.focus();
    if (stage === 3) targetNameRef.current?.focus();
  }, [stage]);

  const handleClose = () => {
    if (!submitting) onClose();
  };

  const handleBackdropMouseDown = (event: MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) handleClose();
  };

  const handleDialogKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") handleClose();
  };

  const submitAction = async () => {
    if (!impact || targetName !== impact.target_name || !reason.trim() || submitting) return;
    setSubmitting(true);
    setLoadError(null);
    try {
      const confirmation = await apiFetch<ModerationConfirmation>("/api/community/moderation/confirmations", {
        method: "POST",
        body: {
          target_type: target.type,
          target_id: target.id,
          action,
          target_name: targetName,
          reason: reason.trim(),
        },
      });
      const result = await apiFetch<ModerationActionResult>("/api/community/moderation/actions", {
        method: "POST",
        body: {
          target_type: target.type,
          target_id: target.id,
          action,
          confirmation_token: confirmation.confirmation_token,
        },
      });
      await onCompleted(result);
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.message : labels.executing);
    } finally {
      setSubmitting(false);
    }
  };

  const impactEntries = impact
    ? Object.entries(impact.related_counts).filter(([, count]) => count > 0)
    : [];
  const canAdvanceFromImpact = Boolean(impact) && !loadError && !submitting;
  const canAdvanceFromReason = reason.trim().length > 0 && !submitting;
  const canExecute = impact !== null && targetName === impact.target_name && reason.trim().length > 0 && !submitting;
  const title = action === "hide" ? labels.title : labels.restoreTitle;
  const executeLabel = action === "hide" ? labels.execute : labels.restore;

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
        className="w-full max-w-lg rounded-xl border border-border bg-bg-primary p-6 shadow-2xl"
        onKeyDown={handleDialogKeyDown}
      >
        <div className="mb-5 flex items-start justify-between gap-4">
          <div>
            <p className={`text-xs font-semibold uppercase tracking-wide ${action === "hide" ? "text-accent-error" : "text-accent-success"}`}>
              {labels.resourceLabel}
            </p>
            <h3 id={`${dialogId}-title`} className="mt-1 text-lg font-semibold text-text-primary">
              {title}
            </h3>
          </div>
          <span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${action === "hide" ? "bg-accent-error/10 text-accent-error" : "bg-accent-success/10 text-accent-success"}`}>
            {labels.stageLabel.replace("{stage}", String(stage))}
          </span>
        </div>

        <div className="mb-5 flex gap-1" aria-hidden="true">
          {[1, 2, 3].map((value) => (
            <div
              key={value}
              className={`h-1.5 flex-1 rounded-full ${value <= stage ? (action === "hide" ? "bg-accent-error" : "bg-accent-success") : "bg-border"}`}
            />
          ))}
        </div>

        <div className="rounded-lg border border-border bg-bg-surface p-4">
          {impact && <p className="font-medium text-text-primary break-words">{impact.target_name}</p>}
          {loadError && <p role="alert" className="mt-2 text-sm text-accent-error">{loadError}</p>}

          {stage === 1 && (
            <div className="mt-4">
              <p className="text-sm font-medium text-text-primary">{labels.impactTitle}</p>
              {!impact && !loadError && <p className="mt-2 text-sm text-text-muted">{labels.impactLoading}</p>}
              {impact && (
                <>
                  <p className="mt-2 text-sm text-text-secondary">{labels.scopeLabel}: {impact.impact_scope}</p>
                  {impact.is_deleted && <p className="mt-2 text-sm text-accent-warning">{labels.deleted}</p>}
                  {impactEntries.length === 0 ? (
                    <p className="mt-2 text-sm text-text-secondary">{labels.impactEmpty}</p>
                  ) : (
                    <dl className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
                      {impactEntries.map(([key, count]) => (
                        <div key={key} className="flex items-center justify-between rounded-md bg-bg-primary px-3 py-2 text-sm">
                          <dt className="text-text-secondary">{impactLabels[key] ?? key}</dt>
                          <dd className="font-semibold text-text-primary">{count}</dd>
                        </div>
                      ))}
                    </dl>
                  )}
                </>
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
                maxLength={1000}
                className="mt-2 w-full resize-y rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm font-normal text-text-primary outline-none focus:border-accent-primary"
              />
            </label>
          )}

          {stage === 3 && impact && (
            <label className="mt-4 block text-sm font-medium text-text-primary">
              {labels.targetNameLabel}
              <span className="mt-1 block font-normal text-text-secondary">{labels.targetNameHelp}</span>
              <input
                ref={targetNameRef}
                value={targetName}
                onChange={(event) => setTargetName(event.target.value)}
                aria-invalid={targetName.length > 0 && targetName !== impact.target_name}
                autoComplete="off"
                className="mt-3 w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm font-normal text-text-primary outline-none focus:border-accent-primary"
              />
            </label>
          )}
        </div>

        <div className="mt-6 flex justify-end gap-3">
          <Button variant="ghost" onClick={handleClose} disabled={submitting}>{labels.cancel}</Button>
          {stage === 1 && <Button onClick={() => setStage(2)} disabled={!canAdvanceFromImpact}>{labels.next}</Button>}
          {stage === 2 && <Button onClick={() => setStage(3)} disabled={!canAdvanceFromReason}>{labels.next}</Button>}
          {stage === 3 && (
            <Button variant={action === "hide" ? "danger" : "primary"} onClick={() => void submitAction()} disabled={!canExecute}>
              {submitting ? labels.executing : executeLabel}
            </Button>
          )}
        </div>
      </section>
    </div>
  );
}
