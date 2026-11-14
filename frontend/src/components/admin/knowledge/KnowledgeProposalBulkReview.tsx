"use client";

import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";

export type ProposalReviewResult = { proposal_id: string; status: string; error?: string; name: string };
interface Props {
  selectedCount: number;
  allSelected: boolean;
  reason: string;
  disabled: boolean;
  canApprove: boolean;
  results: ProposalReviewResult[];
  onSelectPage: (selected: boolean) => void;
  onReasonChange: (reason: string) => void;
  onReview: (action: "approve" | "reject" | "retire") => void;
  onReload: () => void;
}

export function KnowledgeProposalBulkReview({ selectedCount, allSelected, reason, disabled, canApprove, results, onSelectPage, onReasonChange, onReview, onReload }: Props) {
  const t = useTranslations("knowledgeAdmin");
  return (
    <div className="space-y-3 border-b border-border p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm font-medium">{t("selected", { count: selectedCount })}</p>
        <label className="inline-flex items-center gap-2 text-sm">
          <input type="checkbox" disabled={disabled} checked={allSelected} onChange={(event) => onSelectPage(event.target.checked)} />
          {t("selectPage")}
        </label>
      </div>
      <div className="flex flex-wrap gap-2">
        <input value={reason} disabled={disabled} onChange={(event) => onReasonChange(event.target.value)} aria-label={t("reason")} placeholder={t("reason")} maxLength={1000} className="w-full min-w-0 flex-none rounded-md border border-border bg-bg-primary px-3 py-2 text-sm sm:w-auto sm:min-w-[min(100%,16rem)] sm:flex-1" />
        {(["approve", "reject", "retire"] as const).map((action) => (
          <Button key={action} type="button" size="sm" variant={action === "reject" ? "danger" : "secondary"} disabled={disabled || selectedCount === 0 || (action === "approve" && !canApprove)} onClick={() => onReview(action)}>
            {t(action)}
          </Button>
        ))}
      </div>
      {results.length > 0 && <div className="space-y-2">
        <ul aria-live="polite" className="space-y-2 text-sm">
          {results.map((item) => <li key={item.proposal_id} className="break-words">
            <span className={item.status === "succeeded" ? "text-accent-success" : "text-accent-error"}>
              {t(item.status === "succeeded" ? "succeeded" : item.status === "failed" ? "failed" : "unknown")}
            </span> · {item.name}
            {item.error && <span className="ml-2 text-text-secondary">{item.error}</span>}
          </li>)}
        </ul>
        <button type="button" disabled={disabled} onClick={onReload} className="text-sm font-medium text-accent-primary">{t("retry")}</button>
      </div>}
    </div>
  );
}
