"use client";

import { useState, useRef } from "react";
import { useTranslations } from "next-intl";
import { useRouter } from "@/i18n/navigation";
import { Link } from "@/i18n/navigation";
import { apiFormFetch, ApiError } from "@/lib/api-client";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { isRichContentEmpty } from "@/lib/rich-content.mjs";

const CATEGORIES = [
  { value: "material", labelKey: "categoryMaterial" },
  { value: "etc", labelKey: "categoryEtc" },
] as const;

export default function MarketSellPage() {
  const t = useTranslations("market");
  const tc = useTranslations("common");
  const router = useRouter();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [price, setPrice] = useState(100);
  const [category, setCategory] = useState("etc");
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = e.target.files?.[0] ?? null;
    if (selected && selected.size > 50 * 1024 * 1024) {
      setError(t("fileSizeLimit"));
      return;
    }
    setFile(selected);
    setError("");
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;

    setSubmitting(true);
    setError("");
    try {
      const formData = new FormData();
      formData.append("title", title.trim());
      formData.append("description", isRichContentEmpty(description) ? "" : description);
      formData.append("price", String(price));
      formData.append("category", category);
      if (file) {
        formData.append("file", file);
      }

      await apiFormFetch("/api/community/marketplace/items", formData);
      router.push("/market");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("registerFailed"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="mx-auto max-w-2xl px-4 py-8">
      <Link
        href="/market"
        className="mb-4 inline-flex items-center gap-1 text-sm text-text-secondary hover:text-text-primary"
      >
        <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M19 12H5M12 19l-7-7 7-7" />
        </svg>
        {tc("back")}
      </Link>

      <h1 className="mb-6 text-2xl font-bold text-text-primary">{t("registerItem")}</h1>

      {/* 수익 안내 */}
      <div className="mb-6 rounded-lg border border-accent-warning/30 bg-accent-warning/10 p-4 text-sm text-text-secondary">
        <p className="font-medium text-text-primary">{t("sellerEarns")}</p>
        <p>{t("serverReturns")}</p>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-accent-error/30 bg-accent-error/10 p-3 text-sm text-accent-error">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-5">
        {/* 제목 */}
        <div>
          <label htmlFor="title" className="mb-1.5 block text-sm font-medium text-text-primary">
            {t("itemTitle")} <span className="text-accent-error">*</span>
          </label>
          <input
            id="title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={t("itemTitlePlaceholder")}
            maxLength={200}
            required
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
          />
        </div>

        {/* 카테고리 + 가격 */}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor="category" className="mb-1.5 block text-sm font-medium text-text-primary">
              {t("itemCategory")}
            </label>
            <select
              id="category"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="w-full rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm"
            >
              {CATEGORIES.map((cat) => (
                <option key={cat.value} value={cat.value}>
                  {t(cat.labelKey)}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="price" className="mb-1.5 block text-sm font-medium text-text-primary">
              {t("itemPrice")} <span className="text-accent-error">*</span>
            </label>
            <input
              id="price"
              type="number"
              min={1}
              max={100000}
              value={price}
              onChange={(e) => setPrice(parseInt(e.target.value) || 1)}
              className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
            />
          </div>
        </div>

        {/* 설명 */}
        <div>
          <label htmlFor="desc" className="mb-1.5 block text-sm font-medium text-text-primary">
            {t("itemDescription")}
          </label>
          <RichTextEditor
            value={description}
            onChange={setDescription}
            placeholder={t("itemDescPlaceholder")}
          />
        </div>

        {/* 파일 첨부 */}
        <div>
          <label className="mb-1.5 block text-sm font-medium text-text-primary">
            {t("attachFile")}
          </label>
          <p className="mb-2 text-xs text-text-muted">{t("attachFileDesc")}</p>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="rounded-lg border border-border bg-bg-elevated px-4 py-2 text-sm text-text-secondary transition-colors hover:bg-bg-primary"
            >
              <svg className="mr-1.5 inline-block h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
              </svg>
              {t("attachFile")}
            </button>
            <span className="text-sm text-text-muted">
              {file ? t("fileSelected", { name: file.name }) : t("noFile")}
            </span>
          </div>
          <input
            ref={fileInputRef}
            type="file"
            onChange={handleFileChange}
            className="hidden"
          />
        </div>

        {/* 등록 버튼 */}
        <button
          type="submit"
          disabled={submitting || !title.trim()}
          className="w-full rounded-lg bg-accent-primary py-3 text-sm font-semibold text-white transition-colors hover:bg-accent-primary-hover disabled:opacity-50"
        >
          {submitting ? tc("loading") : t("registerItem")}
        </button>
      </form>
    </div>
  );
}
