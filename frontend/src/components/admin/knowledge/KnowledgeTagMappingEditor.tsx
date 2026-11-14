"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { RefreshCw } from "lucide-react";
import { apiFetch, ApiError } from "@/lib/api-client";
import { isEditableMapping } from "@/lib/knowledge-tag-mapping-state.mjs";
import { useKnowledgeTagMappingDraft, type MappingEditorController, type MappingEntityType } from "@/lib/use-knowledge-tag-mapping-draft";
import { useUnsavedChangesGuard } from "@/lib/use-unsaved-changes-guard";
import { useToast } from "@/providers/ToastProvider";
import { Button } from "@/components/ui/Button";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { TagSelector } from "@/components/admin/challenges/TagSelector";
import type { ContentTagMappingAuditEvent, KnowledgeTag } from "@/types/knowledge";

interface Props {
  entityType: MappingEntityType;
  entityId: string;
  controller?: MappingEditorController;
  disabled?: boolean;
  independentSave?: boolean;
}

function formatDate(value: string | null | undefined, missing: string): string {
  return value ? new Date(value).toLocaleString() : missing;
}

function StandaloneMappingEditor(props: Omit<Props, "controller">) {
  const controller = useKnowledgeTagMappingDraft(props.entityType, props.entityId);
  return <IndependentMappingEditor {...props} controller={controller} />;
}

function IndependentMappingEditor(props: Props & { controller: MappingEditorController }) {
  const { controller } = props;
  const guard = useUnsavedChangesGuard(controller.dirty, { busy: controller.busy });
  const t = useTranslations("admin");
  return <>
    <MappingEditorView {...props} controller={controller} standalone />
    <ConfirmModal open={guard.confirmDiscardOpen} title={t("lessonUnsavedTitle")}
      description={t("lessonUnsavedDescription")} confirmLabel={t("discardLessonChanges")}
      variant="danger" onConfirm={guard.confirmDiscard} onCancel={guard.cancelDiscard} />
  </>;
}

export function KnowledgeTagMappingEditor(props: Props) {
  return props.controller
    ? props.independentSave
      ? <IndependentMappingEditor {...props} controller={props.controller} />
      : <MappingEditorView {...props} controller={props.controller} />
    : <StandaloneMappingEditor {...props} />;
}

function MappingEditorView({ entityType, entityId, controller, disabled = false, standalone = false }:
  Props & { controller: MappingEditorController; standalone?: boolean }) {
  const tc = useTranslations("common");
  const tm = useTranslations("knowledgeMapping");
  const ta = useTranslations("admin");
  const { toast } = useToast();
  const [history, setHistory] = useState<{ key: string; events: ContentTagMappingAuditEvent[]; failed: boolean } | null>(null);
  const [tags, setTags] = useState<KnowledgeTag[]>([]);
  const [catalogError, setCatalogError] = useState(false);
  const [historyAttempt, setHistoryAttempt] = useState(0);
  const [confirmReload, setConfirmReload] = useState(false);
  const historyKey = `${entityType}:${entityId}:${historyAttempt}`;
  const events = history?.key === historyKey ? history.events : [];
  const historyError = history?.key === historyKey && history.failed;
  const { draft, saving } = controller;
  const ready = draft?.status === "ready";
  const blocked = disabled || saving || !ready;
  const mappings = draft?.mappings ?? [];

  useEffect(() => {
    const abort = new AbortController();
    void apiFetch<ContentTagMappingAuditEvent[]>(`/api/learn/knowledge-tags/mapping-events/${entityType}/${entityId}`, { signal: abort.signal })
      .then((value) => { if (!abort.signal.aborted) setHistory({ key: historyKey, events: value, failed: false }); })
      .catch(() => { if (!abort.signal.aborted) setHistory({ key: historyKey, events: [], failed: true }); });
    return () => abort.abort();
  }, [entityId, entityType, historyKey]);

  useEffect(() => {
    const abort = new AbortController();
    void apiFetch<KnowledgeTag[]>("/api/learn/knowledge-tags", { signal: abort.signal })
      .then((value) => { if (!abort.signal.aborted) setTags(value); })
      .catch(() => { if (!abort.signal.aborted) setCatalogError(true); });
    return () => abort.abort();
  }, []);

  const save = async () => {
    if (blocked || !controller.dirty) return;
    try {
      if (await controller.save()) {
        setHistoryAttempt((value) => value + 1);
        toast("success", tc("save"));
      }
    } catch (error) {
      toast("error", error instanceof ApiError ? error.message : tc("error"));
    }
  };

  return (
    <section className="space-y-4 border-t border-border py-4" aria-busy={draft?.status === "loading" || saving}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <h3 className="text-sm font-semibold">{tm("title")}</h3>
        <div className="flex items-center gap-2">
        <button type="button" title={tc("retry")} aria-label={tc("retry")} disabled={disabled || saving || !ready}
          onClick={() => controller.dirty ? setConfirmReload(true) : controller.retry()}
          className="rounded p-2 text-text-muted hover:text-text-primary disabled:opacity-50"><RefreshCw size={16} /></button>
        {standalone && <Button type="button" size="sm" onClick={save} disabled={blocked || !controller.dirty}>
          {saving ? tc("processing") : tc("save")}
        </Button>}
        </div>
      </div>
      {(!draft || draft.status === "loading") && <p role="status" className="text-xs text-text-muted">{tc("loading")}</p>}
      {draft?.status === "error" && <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-accent-error">
        <span>{draft.error || tm("loadFailed")}</span>
        <Button type="button" size="sm" onClick={controller.retry}>{tc("retry")}</Button>
      </div>}
      <TagSelector selectedIds={mappings.map((mapping) => mapping.tag_id)} onChange={controller.selectTags}
        disabled={blocked} lockedIds={mappings.filter((mapping) => !isEditableMapping(mapping)).map((mapping) => mapping.tag_id)} />
      {catalogError && <p role="status" className="text-xs text-text-muted">{tm("catalogFailed")}</p>}
      {ready && mappings.length === 0 && <p className="text-xs text-text-muted">{tm("empty")}</p>}
      <div className="space-y-3">
        {mappings.map((mapping) => {
          const tag = tags.find((item) => item.id === mapping.tag_id);
          const readOnly = !isEditableMapping(mapping);
          return <article key={mapping.tag_id} className="rounded-md border border-border/70 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0 text-sm font-medium break-words">{tag?.name ?? tm("tag", { id: mapping.tag_id })}</div>
              <span className="text-xs text-text-muted">{tm(`origins.${mapping.origin}`)}</span>
            </div>
            {readOnly && <p className="mt-1 text-xs text-text-muted">{mapping.origin === "rule" && !("source_relation_id" in mapping && mapping.source_relation_id)
              ? tm("ruleReadOnly") : tm("candidateReadOnly")}</p>}
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <label className="text-xs text-text-secondary">{tm("strength")}
                <input type="number" min={0} max={1} step={0.05} value={mapping.strength} disabled={blocked || readOnly}
                  onChange={(event) => controller.updateMapping(mapping.tag_id, { strength: Number(event.target.value) })}
                  className="mt-1 w-full rounded-md border border-border bg-bg-primary px-2 py-1.5 text-sm disabled:opacity-60" />
              </label>
              <label className="text-xs text-text-secondary">{tm("confidence")}
                <input type="number" min={0} max={1} step={0.05} value={mapping.confidence} disabled={blocked || readOnly}
                  onChange={(event) => controller.updateMapping(mapping.tag_id, { confidence: Number(event.target.value) })}
                  className="mt-1 w-full rounded-md border border-border bg-bg-primary px-2 py-1.5 text-sm disabled:opacity-60" />
              </label>
            </div>
            <label className="mt-3 block text-xs text-text-secondary">{tm("evidence")}
              <textarea value={mapping.evidence_summary} maxLength={1000} disabled={blocked || readOnly}
                onChange={(event) => controller.updateMapping(mapping.tag_id, { evidence_summary: event.target.value })}
                className="mt-1 min-h-16 w-full rounded-md border border-border bg-bg-primary px-2 py-1.5 text-sm disabled:opacity-60" />
            </label>
            {(mapping.created_at || mapping.approved_at) && <p className="mt-2 text-[11px] text-text-muted">
              {tm("dates", { created: formatDate(mapping.created_at, tm("noRecord")), approved: formatDate(mapping.approved_at, tm("noRecord")) })}
            </p>}
          </article>;
        })}
      </div>
      {historyError && <div className="flex flex-wrap items-center gap-2 text-xs text-text-muted" role="status">
        {tm("historyFailed")}
        <button type="button" onClick={() => setHistoryAttempt((value) => value + 1)} className="underline">{tc("retry")}</button>
      </div>}
      {events.length > 0 && <details className="border-t border-border/70 pt-3">
        <summary className="cursor-pointer text-xs font-semibold">{tm("history", { count: events.length })}</summary>
        <ul className="mt-2 max-h-48 space-y-2 overflow-auto text-[11px] text-text-muted">
          {events.slice(0, 30).map((event) => <li key={event.id} className="flex flex-wrap gap-2 border-b border-border/60 pb-1.5 last:border-0">
            <span>{event.action === "attach" ? tm("attached") : tm("detached")}</span><span>{tm("tag", { id: event.tag_id })}</span><span>{formatDate(event.created_at, tm("noRecord"))}</span>
          </li>)}
        </ul>
      </details>}
      <ConfirmModal open={confirmReload} title={ta("lessonUnsavedTitle")} description={ta("lessonUnsavedDescription")}
        confirmLabel={ta("discardLessonChanges")} variant="danger" onCancel={() => setConfirmReload(false)}
        onConfirm={() => { setConfirmReload(false); controller.retry(); }} />
    </section>
  );
}
