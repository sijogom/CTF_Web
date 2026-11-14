"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Search, SlidersHorizontal, X, RefreshCw, ArrowRight } from "lucide-react";
import { MAP_CONTENT_TYPES, MAP_RELATION_TYPES } from "@/lib/map-ui-state.mjs";
import type { KnowledgeMapV2Search, KnowledgeMapV2SearchItem } from "@/types/knowledge";

interface Props {
  filters: { types: string[]; relations: string[]; query: string };
  focusType?: string;
  input: string;
  counts: { nodes: number; edges: number } | null;
  search: { loading: boolean; error: boolean; result: KnowledgeMapV2Search | null };
  onSearch: (value: string) => void;
  onFilter: (key: "types" | "relations", value: string, checked: boolean) => void;
  onReset: () => void;
  onRetry: () => void;
  onPick: (item: KnowledgeMapV2SearchItem) => void;
}

export function KnowledgeMapTools({ filters, focusType, input, counts, search, onSearch, onFilter, onReset, onRetry, onPick }: Props) {
  const t = useTranslations("knowledgeMap");
  const [open, setOpen] = useState(false);
  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <button id="knowledge-map-tools-toggle" type="button" aria-expanded={open} aria-controls="knowledge-map-tools" title={t("searchAndFilter")} onClick={() => setOpen((value) => !value)} className="inline-flex h-10 items-center gap-2 rounded-lg border border-border bg-bg-surface px-3 text-sm font-medium text-text-primary">
          <SlidersHorizontal className="h-4 w-4" aria-hidden="true" />{t("searchAndFilter")}
        </button>
        {counts && <p role="status" className="min-w-0 break-words text-xs text-text-secondary">
          {t("visibleCount", counts)}
        </p>}
      </div>
      {open && <div id="knowledge-map-tools" className="space-y-4 border-y border-border py-4">
        <div className="relative max-w-xl">
          <Search className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-text-muted" aria-hidden="true" />
          <input aria-label={t("search")} type="search" maxLength={100} value={input} onChange={(event) => onSearch(event.target.value)} className="h-10 w-full rounded-lg border border-border bg-bg-primary pl-9 pr-10 text-sm [&::-webkit-search-cancel-button]:hidden" placeholder={t("search")} />
          {input && <button type="button" title={t("clearSearch")} aria-label={t("clearSearch")} onClick={() => onSearch("")} className="absolute right-1 top-1 flex h-8 w-8 items-center justify-center rounded-md text-text-secondary">
            <X className="h-4 w-4" aria-hidden="true" />
          </button>}
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          <fieldset>
            <legend className="mb-2 text-sm font-semibold">{t("contentTypes")}</legend>
            <div className="flex flex-wrap gap-x-4 gap-y-2">
              {MAP_CONTENT_TYPES.map((type) => <label key={type} className="inline-flex items-center gap-2 text-sm">
                <input type="checkbox" checked={filters.types.includes(type)} disabled={type === focusType || (filters.types.length === 1 && filters.types.includes(type))} title={type === focusType ? t("focusNode") : undefined} onChange={(event) => onFilter("types", type, event.target.checked)} />
                {t(`nodeTypes.${type}`)}
              </label>)}
            </div>
          </fieldset>
          <fieldset>
            <legend className="mb-2 text-sm font-semibold">{t("relations")}</legend>
            <div className="flex flex-wrap gap-x-4 gap-y-2">
              {MAP_RELATION_TYPES.map((type) => <label key={type} className="inline-flex items-center gap-2 text-sm">
                <input type="checkbox" checked={filters.relations.includes(type)} disabled={filters.relations.length === 1 && filters.relations.includes(type)} onChange={(event) => onFilter("relations", type, event.target.checked)} />
                {t(`relationTypes.${type}`)}
              </label>)}
            </div>
          </fieldset>
        </div>
        <button type="button" onClick={onReset} className="inline-flex items-center gap-1.5 text-sm text-accent-primary">
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />{t("resetFilters")}
        </button>
        {filters.query.trim() && <div aria-live="polite">
          {search.loading ? <p className="text-sm text-text-muted" role="status">{t("searchLoading")}</p>
            : search.error ? <div role="alert" className="flex flex-wrap items-center gap-2 text-sm text-accent-error">
              {t("searchFailed")}<button type="button" onClick={onRetry} className="text-accent-primary">{t("retry")}</button>
            </div>
            : search.result && <ul className="grid max-h-64 gap-1 overflow-y-auto sm:grid-cols-2">
              {search.result.items.length === 0 && <li className="text-sm text-text-muted">{t("searchEmpty")}</li>}
              {search.result.items.map((item) => <li key={item.id}>
                <button type="button" onClick={() => onPick(item)} className="flex w-full min-w-0 items-center justify-between gap-3 rounded-md px-3 py-2 text-left hover:bg-bg-elevated">
                  <span className="min-w-0 break-words text-sm font-medium">
                    {item.name}<span className="mt-0.5 block text-xs text-text-secondary">{t(`nodeTypes.${item.type}`)}</span>
                  </span>
                  <ArrowRight className="h-4 w-4 shrink-0" aria-hidden="true" />
                </button>
              </li>)}
            </ul>}
        </div>}
      </div>}
    </>
  );
}
