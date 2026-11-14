"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useLocale, useTranslations } from "next-intl";
import { notFound } from "next/navigation";
import { Pencil, Plus, Save } from "lucide-react";
import { Link } from "@/i18n/navigation";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useUnsavedChangesGuard } from "@/lib/use-unsaved-changes-guard";
import { useEventCategories } from "@/components/events/useEventCategories";
import { Button } from "@/components/ui/Button";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { eventCategoryLabel, type EventCategoryDefinition } from "@/types/event";
import { eventCategoryManagementEnabled } from "@/lib/event-category-release-stage.mjs";

const LANGUAGES = [{ code: "ko", label: "한국어" }, { code: "en", label: "English" }, { code: "ja", label: "日本語" }, { code: "zh", label: "中文" }] as const;
type Draft = { labels: Record<string, string>; sortOrder: string };
const emptyDraft = (): Draft => ({ labels: { ko: "", en: "", ja: "", zh: "" }, sortOrder: "0" });

export default function EventCategoriesPage() {
  if (!eventCategoryManagementEnabled()) notFound();
  return <EventCategoriesEditor />;
}

function EventCategoriesEditor() {
  const t = useTranslations("events.categoryManagement");
  const te = useTranslations("events");
  const tc = useTranslations("common");
  const locale = useLocale();
  const listing = useEventCategories(true);
  const [editing, setEditing] = useState<EventCategoryDefinition | "new" | null>(null);
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [initial, setInitial] = useState<Draft>(emptyDraft);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [conflicted, setConflicted] = useState(false);
  const [registrationPending, setRegistrationPending] = useState(false);
  const operationRef = useRef(false);
  const createOperationRef = useRef<string | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const dirty = editing !== null && JSON.stringify(draft) !== JSON.stringify(initial);
  const guard = useUnsavedChangesGuard(dirty, { busy });

  useEffect(() => {
    if (editing) formRef.current?.querySelector<HTMLInputElement>("input")?.focus();
  }, [editing]);

  function open(category: EventCategoryDefinition | "new") {
    if (!eventCategoryManagementEnabled() || operationRef.current || registrationPending) return;
    guard.requestDiscard(() => {
      const next = category === "new" ? { ...emptyDraft(), sortOrder: String(Math.min(1000000, Math.max(-1, ...listing.categories.map((item) => item.sort_order)) + 1)) } : { labels: { ...category.labels }, sortOrder: String(category.sort_order) };
      setEditing(category);
      setDraft(next);
      setInitial(next);
      setError(null);
      setSaved(false);
      setConflicted(false);
      setRegistrationPending(false);
      createOperationRef.current = category === "new" ? crypto.randomUUID() : null;
    });
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!eventCategoryManagementEnabled() || !editing || operationRef.current || listing.loading || listing.error || conflicted) return;
    const order = Number(draft.sortOrder);
    if (!draft.sortOrder.trim() || !Number.isInteger(order) || order < 0 || order > 1000000 || LANGUAGES.some(({ code }) => !draft.labels[code]?.trim())) {
      setError(t("required"));
      return;
    }
    operationRef.current = true;
    setBusy(true);
    setError(null);
    setSaved(false);
    const target = editing;
    try {
      await apiFetch<EventCategoryDefinition>(target === "new" ? "/api/learn/admin/event-categories" : `/api/learn/admin/event-categories/${encodeURIComponent(target.key)}`, {
        method: target === "new" ? "POST" : "PATCH",
        body: { labels: Object.fromEntries(LANGUAGES.map(({ code }) => [code, draft.labels[code].trim()])), sort_order: order, ...(target === "new" ? { operation_id: createOperationRef.current } : { expected_revision: target.revision }) },
      });
      setEditing(null);
      setSaved(true);
      setRegistrationPending(false);
      await listing.reload();
    } catch (cause) {
      if (target === "new" && (!(cause instanceof ApiError) || cause.status >= 500)) {
        setRegistrationPending(true);
        setError(t("registrationPending"));
      } else if (cause instanceof ApiError && cause.code === "event_category_revision_conflict" && target !== "new") {
        setConflicted(true);
        setError(t("conflict"));
        await listing.reload();
      } else {
        setRegistrationPending(false);
        setError(cause instanceof ApiError ? cause.message : tc("errorOccurred"));
      }
    } finally {
      operationRef.current = false;
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3"><div><Link href="/admin/events" className="inline-flex min-h-11 items-center text-sm text-accent-primary hover:underline">{te("title")}</Link><h1 className="mt-2 text-2xl font-bold">{t("title")}</h1></div><Button className="h-11 shrink-0 gap-2 whitespace-nowrap" disabled={busy || registrationPending || listing.loading || Boolean(listing.error)} onClick={() => open("new")}><Plus size={16} aria-hidden="true" />{t("add")}</Button></div>
      {saved && <p role="status" className="text-sm text-accent-success">{t("saved")}</p>}
      {listing.error && <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-accent-error"><span>{listing.error}</span><button type="button" disabled={busy} onClick={() => void listing.reload()} className="min-h-11 shrink-0 whitespace-nowrap underline">{tc("retry")}</button></div>}
      {editing && <form ref={formRef} onSubmit={save} className="space-y-4 border-y border-border py-5" aria-busy={busy}>
        <h2 className="text-lg font-semibold">{editing === "new" ? t("add") : eventCategoryLabel(editing.labels, locale, editing.key)}</h2>
        {error && <p role="alert" className="text-sm text-accent-error">{error}</p>}
        <fieldset disabled={busy || registrationPending} className="grid min-w-0 gap-4 sm:grid-cols-2">
          {LANGUAGES.map(({ code, label }) => <div key={code}><label htmlFor={`category-name-${code}`} className="mb-1.5 block text-sm font-medium">{t("name")} · {label}</label><input id={`category-name-${code}`} required maxLength={80} value={draft.labels[code] ?? ""} onChange={(event) => setDraft((prior) => ({ ...prior, labels: { ...prior.labels, [code]: event.target.value } }))} className="h-11 w-full min-w-0 rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm" /></div>)}
          <div><label htmlFor="category-order" className="mb-1.5 block text-sm font-medium">{t("order")}</label><input id="category-order" type="number" min={0} max={1000000} step={1} required value={draft.sortOrder} onChange={(event) => setDraft((prior) => ({ ...prior, sortOrder: event.target.value }))} className="h-11 w-full min-w-0 rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm" /></div>
        </fieldset>
        <div className="flex flex-wrap justify-end gap-2"><Button className="h-11 shrink-0 whitespace-nowrap" type="button" variant="ghost" disabled={busy || registrationPending} onClick={() => guard.requestDiscard(() => { setEditing(null); setError(null); })}>{tc("cancel")}</Button><Button className="h-11 shrink-0 gap-2 whitespace-nowrap" type="submit" disabled={busy || listing.loading || Boolean(listing.error) || conflicted}><Save size={16} aria-hidden="true" />{busy ? tc("processing") : registrationPending ? tc("retry") : tc("save")}</Button></div>
      </form>}
      {listing.loading && <p role="status" className="text-sm text-text-muted">{tc("loading")}</p>}
      <div className="divide-y divide-border border-y border-border" aria-busy={listing.loading}>
        {listing.categories.map((category) => <div key={category.key} className="flex items-start justify-between gap-3 py-4"><div className="min-w-0 flex-1"><h2 className="break-words font-medium">{eventCategoryLabel(category.labels, locale, category.key)}</h2><div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-muted"><span>{t("order")}: {category.sort_order}</span>{LANGUAGES.map(({ code, label }) => <span key={code} lang={code}>{label}: {category.labels[code]}</span>)}</div></div><Button className="h-11 w-11 shrink-0 p-0" variant="ghost" disabled={busy || registrationPending || listing.loading || Boolean(listing.error)} onClick={() => open(category)} aria-label={`${eventCategoryLabel(category.labels, locale, category.key)} ${tc("edit")}`} title={tc("edit")}><Pencil size={16} aria-hidden="true" /></Button></div>)}
      </div>
      <ConfirmModal open={guard.confirmDiscardOpen} title={t("unsavedTitle")} description={t("unsavedDescription")} confirmLabel={t("discard")} variant="danger" onConfirm={guard.confirmDiscard} onCancel={guard.cancelDiscard} />
    </div>
  );
}
