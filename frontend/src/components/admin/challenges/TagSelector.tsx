"use client";

import { useState, useEffect, useId, useRef } from "react";
import { LockKeyhole, X, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api-client";
import type { KnowledgeTag } from "@/types/knowledge";

interface Props {
  selectedIds: number[];
  onChange: (ids: number[]) => void;
  disabled?: boolean;
  lockedIds?: number[];
}

export function TagSelector({ selectedIds, onChange, disabled = false, lockedIds = [] }: Props) {
  const t = useTranslations("tagSelector");
  const [tags, setTags] = useState<KnowledgeTag[]>([]);
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  const inputId = useId();
  const listId = `${inputId}-results`;
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void apiFetch<KnowledgeTag[]>("/api/learn/knowledge-tags", { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setTags(value); })
      .catch(() => { if (!controller.signal.aborted) setError(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [retry]);

  const filtered = tags.filter(
    (tag) =>
      !selectedIds.includes(tag.id) &&
      (tag.name.toLowerCase().includes(search.toLowerCase()) ||
        tag.slug.toLowerCase().includes(search.toLowerCase()))
  );

  const options = filtered.slice(0, 10);
  const addTag = (id: number) => {
    if (disabled) return;
    onChange([...selectedIds, id]);
    setSearch("");
    setOpen(false);
    setActiveIndex(0);
    inputRef.current?.focus();
  };

  const removeTag = (id: number) => {
    if (disabled || lockedIds.includes(id)) return;
    onChange(selectedIds.filter((tid) => tid !== id));
    inputRef.current?.focus();
  };

  return (
    <div onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false); }}>
      <label htmlFor={inputId} className="mb-1.5 block text-sm font-medium">
        {t("label")}
      </label>

      {/* 선택된 태그 */}
      {selectedIds.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2">
          {selectedIds.map((id) => {
            const tag = tags.find((t) => t.id === id);
            return (
              <span
                key={id}
                className="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium"
                style={{
                  backgroundColor: tag?.color ? `${tag.color}20` : "rgba(var(--accent-primary-rgb), 0.1)",
                  color: tag?.color || "var(--accent-primary)",
                }}
              >
                {tag?.color && (
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ backgroundColor: tag.color }}
                  />
                )}
                {tag?.name ?? `#${id}`}
                {lockedIds.includes(id) ? <span className="inline-flex items-center gap-1" title={t("automatic")}><LockKeyhole className="h-3 w-3" aria-hidden="true" /><span className="sr-only">{t("automatic")}</span></span> : <button
                  type="button"
                  onClick={() => removeTag(id)}
                  disabled={disabled}
                  aria-label={t("remove", { name: tag?.name ?? `#${id}` })}
                  className="opacity-60 hover:opacity-100"
                >
                  <X className="h-3.5 w-3.5" aria-hidden="true" />
                </button>}
              </span>
            );
          })}
        </div>
      )}

      {/* 검색 */}
      <div className="relative">
        <input
          ref={inputRef}
          id={inputId}
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={open && Boolean(search)}
          aria-controls={listId}
          aria-activedescendant={open && search && options[activeIndex] ? `${listId}-${options[activeIndex].id}` : undefined}
          disabled={disabled || loading || error}
          value={search}
          onChange={(e) => { setSearch(e.target.value); setActiveIndex(0); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing) return;
            if (event.key === "Escape") { setOpen(false); return; }
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
              event.preventDefault(); setOpen(true);
              setActiveIndex((index) => options.length ? (index + (event.key === "ArrowDown" ? 1 : options.length - 1)) % options.length : 0);
            } else if (event.key === "Enter" && open && options[activeIndex]) {
              event.preventDefault(); addTag(options[activeIndex].id);
            }
          }}
          placeholder={loading ? t("loading") : t("search")}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm focus:border-accent-primary focus:outline-none"
        />
        {open && search && !disabled && !loading && !error && (
          <div id={listId} role="listbox" aria-label={t("results")} className="absolute z-20 mt-1 max-h-48 w-full overflow-y-auto rounded-lg border border-border bg-bg-surface shadow-lg">
            {options.length === 0 && <p role="status" className="px-3 py-2 text-sm text-text-muted">{t("empty")}</p>}
            {options.map((tag, index) => (
              <button
                key={tag.id}
                id={`${listId}-${tag.id}`}
                role="option"
                aria-selected={index === activeIndex}
                type="button"
                onClick={() => addTag(tag.id)}
                onFocus={() => setActiveIndex(index)}
                className={`flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-text-secondary hover:bg-bg-elevated hover:text-text-primary ${index === activeIndex ? "bg-bg-elevated" : ""}`}
              >
                {tag.color && (
                  <span
                    className="inline-block h-3 w-3 rounded-full"
                    style={{ backgroundColor: tag.color }}
                  />
                )}
                <span className="min-w-0 break-words">{tag.name}</span>
                <span className="ml-auto min-w-0 break-all text-xs text-text-muted">{tag.slug}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      {error && <div role="alert" className="mt-2 flex flex-wrap items-center gap-2 text-sm text-accent-error"><span>{t("failed")}</span><button type="button" disabled={disabled} onClick={() => { setLoading(true); setError(false); setRetry((value) => value + 1); }} className="inline-flex items-center gap-1 text-accent-primary"><RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />{t("retry")}</button></div>}
    </div>
  );
}
