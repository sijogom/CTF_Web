"use client";

import { useEffect, useId, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Search, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api-client";
import { parseOffsetListPage } from "@/lib/list-response.mjs";
import type { KnowledgeContentSummary, KnowledgeContentType } from "@/types/knowledge";

const CONTENT_TYPES: KnowledgeContentType[] = ["challenge", "course", "lesson", "knowledge_tag", "hint", "event", "assignment"];
const PAGE_SIZE = 20;

function displayIdentifier(id: string): string {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)
    ? `${id.slice(0, 8)}...${id.slice(-4)}`
    : id;
}

interface Props {
  label: string;
  type: KnowledgeContentType;
  value: KnowledgeContentSummary | null;
  disabled?: boolean;
  onTypeChange: (type: KnowledgeContentType) => void;
  onChange: (content: KnowledgeContentSummary | null) => void;
}

export function KnowledgeContentPicker({ label, type, value, disabled = false, onTypeChange, onChange }: Props) {
  const t = useTranslations("knowledgeAdmin.management");
  const id = useId();
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<KnowledgeContentSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [retry, setRetry] = useState(0);
  const requestRef = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open || disabled) return;
    const generation = ++requestRef.current;
    const controller = new AbortController();
    setLoading(true);
    setError(false);
    setItems([]);
    setActive(-1);
    const timer = window.setTimeout(async () => {
      try {
        const params = new URLSearchParams({ type, q: query.trim(), offset: String(offset), limit: String(PAGE_SIZE) });
        const raw = await apiFetch<unknown>(`/api/learn/admin/knowledge-contents?${params}`, { signal: controller.signal });
        const page = parseOffsetListPage<KnowledgeContentSummary>(raw, offset, PAGE_SIZE);
        if (generation !== requestRef.current || controller.signal.aborted) return;
        if (page.items.some((item) => item.type !== type || !item.id || typeof item.name !== "string")) throw new Error("Invalid content response");
        setItems(page.items);
        setTotal(page.total);
      } catch {
        if (generation !== requestRef.current || controller.signal.aborted) return;
        setError(true);
      } finally {
        if (generation === requestRef.current && !controller.signal.aborted) setLoading(false);
      }
    }, 200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [type, query, offset, open, disabled, retry]);

  const choose = (item: KnowledgeContentSummary) => {
    onChange(item);
    setQuery("");
    setOffset(0);
    inputRef.current?.focus();
    setOpen(false);
  };

  return (
    <fieldset disabled={disabled} className="min-w-0 space-y-3 rounded-lg border border-border p-4">
      <legend className="px-1 text-sm font-medium">{label}</legend>
      <select aria-label={t("contentType", { name: label })} value={type} disabled={disabled} onChange={(event) => {
        ++requestRef.current;
        onTypeChange(event.target.value as KnowledgeContentType);
        onChange(null);
        setQuery(""); setOffset(0); setItems([]); setOpen(true);
      }} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm">
        {CONTENT_TYPES.map((item) => <option key={item} value={item}>{t(`contentTypes.${item}`)}</option>)}
      </select>
      {value && <div className="flex min-w-0 items-start gap-2 rounded-md bg-bg-elevated p-3">
        <div className="min-w-0 flex-1">
          <p className="break-words text-sm font-medium text-text-primary">{value.name}</p>
          <p className="mt-1 text-xs text-text-secondary">{t(`contentTypes.${value.type}`)}{!value.is_active && ` · ${t("inactiveContent")}`}</p>
          <p className="mt-1 break-all text-xs text-text-muted">{t("contentId")}: <span title={value.id} aria-label={`${t("contentId")}: ${value.id}`}>{displayIdentifier(value.id)}</span></p>
        </div>
        {!disabled && <button type="button" title={t("clearSelection")} aria-label={t("clearSelection")} onClick={() => { onChange(null); setOpen(true); inputRef.current?.focus(); }} className="shrink-0 rounded p-1 text-text-secondary hover:bg-bg-primary"><X size={16} /></button>}
      </div>}
      {!disabled && <>
        <div className="relative">
          <Search size={16} className="pointer-events-none absolute left-3 top-3 text-text-muted" />
          <input ref={inputRef} role="combobox" aria-label={t("searchContent", { name: label })} aria-autocomplete="list" aria-controls={`${id}-results`} aria-expanded={open} aria-activedescendant={open && active >= 0 ? `${id}-item-${active}` : undefined} value={query}
            onFocus={() => setOpen(true)} onChange={(event) => { ++requestRef.current; setQuery(event.target.value); setOffset(0); setItems([]); setActive(-1); setOpen(true); }}
            onKeyDown={(event) => {
              if (event.key === "Escape") { event.preventDefault(); setOpen(false); return; }
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault(); setOpen(true);
                if (!loading && !error && items.length) {
                  const next = event.key === "ArrowDown" ? Math.min(active + 1, items.length - 1) : Math.max(active - 1, 0);
                  setActive(next);
                  document.getElementById(`${id}-item-${next}`)?.scrollIntoView({ block: "nearest" });
                }
              }
              if (event.key === "Enter" && open && !loading && !error && active >= 0 && items[active]) { event.preventDefault(); choose(items[active]); }
            }} placeholder={t("searchPlaceholder")} className="w-full rounded-lg border border-border bg-bg-primary py-2 pl-9 pr-3 text-sm" />
        </div>
        {open && <div className="overflow-hidden rounded-md border border-border bg-bg-surface">
          {loading ? <p role="status" className="p-3 text-sm text-text-muted">{t("searchLoading")}</p> : error ? <div role="alert" className="space-y-2 p-3 text-sm text-accent-error"><p>{t("searchFailed")}</p><button type="button" onClick={() => setRetry((count) => count + 1)} className="text-accent-primary">{t("retry")}</button></div> : <>
            {items.length === 0 && <p className="p-3 text-sm text-text-muted">{t("searchEmpty")}</p>}
            <ul id={`${id}-results`} role="listbox" aria-label={label} className="max-h-64 overflow-y-auto">
              {items.map((item, index) => <li key={`${item.type}:${item.id}`} id={`${id}-item-${index}`} role="option" aria-selected={active === index}>
                <button type="button" onMouseEnter={() => setActive(index)} onClick={() => choose(item)} className={`w-full break-words px-3 py-2 text-left text-sm ${active === index ? "bg-accent-primary/10 text-accent-primary" : "hover:bg-bg-elevated"}`}>
                  <span className="block font-medium">{item.name}</span><span className="text-xs text-text-secondary">{t(`contentTypes.${item.type}`)}{!item.is_active && ` · ${t("inactiveContent")}`}</span>
                  <span className="block break-all text-xs text-text-muted">{t("contentId")}: <span title={item.id} aria-label={`${t("contentId")}: ${item.id}`}>{displayIdentifier(item.id)}</span></span>
                </button>
              </li>)}
            </ul>
            <div className="flex items-center justify-between gap-2 border-t border-border p-2 text-xs text-text-secondary">
              <button type="button" disabled={offset === 0} aria-label={t("previousResults")} title={t("previousResults")} className="rounded p-1 disabled:opacity-40" onClick={() => { ++requestRef.current; setItems([]); setOffset((current) => Math.max(0, current - PAGE_SIZE)); }}><ChevronLeft size={18} /></button>
              <span>{t("resultRange", { start: total === 0 ? 0 : offset + 1, end: Math.min(offset + PAGE_SIZE, total), total })}</span>
              <button type="button" disabled={offset + PAGE_SIZE >= total} aria-label={t("nextResults")} title={t("nextResults")} className="rounded p-1 disabled:opacity-40" onClick={() => { ++requestRef.current; setItems([]); setOffset((current) => current + PAGE_SIZE); }}><ChevronRight size={18} /></button>
            </div>
          </>}
        </div>}
      </>}
    </fieldset>
  );
}
