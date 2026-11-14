"use client";

import { useState } from "react";
import { useRouter } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { apiFetch, ApiError } from "@/lib/api-client";
import { isRichContentEmpty } from "@/lib/rich-content.mjs";

export default function NewCoursePage() {
  const t = useTranslations("admin");
  const td = useTranslations("difficulty");
  const tc = useTranslations("common");
  const router = useRouter();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState("web");
  const [difficulty, setDifficulty] = useState("introductory");
  const [estimatedHours, setEstimatedHours] = useState(4);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    if (!title.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const result = await apiFetch<{ id: string }>("/api/learn/courses", {
        method: "POST",
        body: { title, description: isRichContentEmpty(description) ? "" : description, category, difficulty, estimated_hours: parseInt(String(estimatedHours)) || 1 },
      });
      // 생성 후 코스 상세 페이지로 이동 (레슨 추가/편집 가능)
      router.push(`/admin/courses/${result.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : tc("errorOccurred"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <h1 className="text-2xl font-bold">{t("createCourseTitle")}</h1>

      {error && (
        <div className="rounded-lg border border-status-error/30 bg-status-error/10 px-4 py-3 text-sm text-status-error">
          {error}
        </div>
      )}

      <div>
        <label htmlFor="course-title" className="mb-1.5 block text-sm font-medium">
          {t("courseTitleLabel")} <span className="text-accent-error">*</span>
        </label>
        <input
          id="course-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder={t("coursePlaceholder")}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
        />
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <div>
          <label htmlFor="course-category" className="mb-1.5 block text-sm font-medium">
            {t("courseCategory")}
          </label>
          <select
            id="course-category"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="w-full rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm"
          >
            {(["web", "pwn", "rev", "crypto", "forensic", "misc"] as const).map((cat) => (
              <option key={cat} value={cat}>{cat.toUpperCase()}</option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="course-difficulty" className="mb-1.5 block text-sm font-medium">
            {t("difficultyLabel")}
          </label>
          <select
            id="course-difficulty"
            value={difficulty}
            onChange={(e) => setDifficulty(e.target.value)}
            className="w-full rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm"
          >
            <option value="introductory">{td("introductory")}</option>
            <option value="basic">{td("basic")}</option>
            <option value="intermediate">{td("intermediate")}</option>
            <option value="advanced">{td("advanced")}</option>
          </select>
        </div>
        <div>
          <label htmlFor="course-hours" className="mb-1.5 block text-sm font-medium">
            {t("estimatedHoursLabel")}
          </label>
          <input
            id="course-hours"
            type="number"
            min={1}
            max={100}
            value={estimatedHours}
            onChange={(e) => setEstimatedHours(parseInt(e.target.value) || 1)}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm"
          />
        </div>
      </div>

      <div>
        <label className="mb-1.5 block text-sm font-medium">{t("courseDescLabel")}</label>
        <RichTextEditor
          value={description}
          onChange={setDescription}
          placeholder={t("courseDescPlaceholder")}
        />
      </div>

      {/* 안내: 코스 생성 후 레슨 추가 가능 */}
      <div className="rounded-lg border border-accent-primary/20 bg-accent-primary/5 p-4 text-sm text-text-secondary">
        <p>{t("courseCreateGuide")}</p>
      </div>

      <div className="flex justify-end gap-3">
        <Button variant="ghost" onClick={() => router.back()}>
          {tc("cancel")}
        </Button>
        <Button onClick={handleSubmit} disabled={loading || !title.trim()}>
          {loading ? tc("processing") : t("createCourseBtn")}
        </Button>
      </div>
    </div>
  );
}
