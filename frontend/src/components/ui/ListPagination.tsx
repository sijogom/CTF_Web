"use client";

import { useTranslations } from "next-intl";

interface ListPaginationProps {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  disabled?: boolean;
}

export function ListPagination({
  page,
  pageSize,
  total,
  onPageChange,
  disabled = false,
}: ListPaginationProps) {
  const t = useTranslations("common");
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <nav
      aria-label={t("paginationLabel")}
      className="flex flex-wrap items-center justify-center gap-3"
    >
      {totalPages > 1 && (
        <button
          type="button"
          onClick={() => onPageChange(Math.max(1, page - 1))}
          disabled={disabled || page <= 1}
          className="rounded-lg px-3 py-2 text-sm text-text-secondary hover:bg-bg-surface disabled:opacity-40"
        >
          {t("previousPage")}
        </button>
      )}
      <span className="text-sm text-text-muted">
        {t("pageStatus", { page, totalPages, total })}
      </span>
      {totalPages > 1 && (
        <button
          type="button"
          onClick={() => onPageChange(Math.min(totalPages, page + 1))}
          disabled={disabled || page >= totalPages}
          className="rounded-lg px-3 py-2 text-sm text-text-secondary hover:bg-bg-surface disabled:opacity-40"
        >
          {t("nextPage")}
        </button>
      )}
    </nav>
  );
}
