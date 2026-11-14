"use client";

import { useState } from "react";
import { useRouter } from "@/i18n/navigation";
import { useTranslations, useLocale } from "next-intl";
import { Button } from "@/components/ui/Button";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { apiFetch, ApiError } from "@/lib/api-client";
import { isRichContentEmpty } from "@/lib/rich-content.mjs";
import { useAuth } from "@/providers/AuthProvider";
import { isAdminRole } from "@/lib/roles";

const BOARD_OPTION_KEYS = [
  { value: "free", i18nKey: "boardFree", adminOnly: false },
  { value: "qna", i18nKey: "boardQna", adminOnly: false },
  { value: "writeup", i18nKey: "boardWriteup", adminOnly: false },
  { value: "notice", i18nKey: "boardNotice", adminOnly: true },
] as const;

export default function WritePage() {
  const t = useTranslations("community");
  const tc = useTranslations("common");
  const locale = useLocale();
  const router = useRouter();
  const { user } = useAuth();
  const canPostNotice = isAdminRole(user?.role);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [boardType, setBoardType] = useState("free");
  const [isAnonymous, setIsAnonymous] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    const trimmedTitle = title.trim();
    if (!trimmedTitle || isRichContentEmpty(content) || trimmedTitle.length > 200) return;
    setLoading(true);
    setError(null);

    try {
      await apiFetch("/api/community/posts", {
        method: "POST",
        body: { title, content, board_type: boardType, is_anonymous: isAnonymous, language: locale },
      });
      router.push("/community");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("submitFailed"));
      setLoading(false);
    }
  };

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <h1 className="text-2xl font-bold">{t("writePost")}</h1>

      {/* 게시판 선택 */}
      <div>
        <label className="mb-2 block text-sm font-medium text-text-secondary">{t("boardLabel")}</label>
        <div className="flex gap-2">
          {BOARD_OPTION_KEYS.filter((opt) => !opt.adminOnly || canPostNotice).map((opt) => (
            <button
              key={opt.value}
              onClick={() => setBoardType(opt.value)}
              className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
                boardType === opt.value
                  ? "bg-accent-primary text-white"
                  : "bg-bg-surface text-text-secondary hover:text-text-primary"
              }`}
            >
              {t(opt.i18nKey)}
            </button>
          ))}
        </div>
      </div>

      {/* 제목 */}
      <div>
        <label className="mb-2 block text-sm font-medium text-text-secondary">{t("titleLabel")}</label>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder={t("titlePlaceholder")}
          maxLength={200}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
        />
      </div>

      {/* 내용 (리치 텍스트 에디터) */}
      <div>
        <label className="mb-2 block text-sm font-medium text-text-secondary">{t("contentLabel")}</label>
        <RichTextEditor
          value={content}
          onChange={setContent}
          placeholder={t("contentPlaceholder")}
        />
      </div>

      {/* 에러 메시지 */}
      {error && (
        <div className="rounded-lg bg-accent-error/10 px-4 py-3 text-sm text-accent-error">
          {error}
        </div>
      )}

      {/* 익명 + 제출 */}
      <div className="flex items-center justify-between">
        <label className="flex cursor-pointer items-center gap-2 text-sm text-text-secondary">
          <input
            type="checkbox"
            checked={isAnonymous}
            onChange={(e) => setIsAnonymous(e.target.checked)}
            className="h-4 w-4 rounded border-border accent-accent-primary"
          />
          {t("postAsAnonymous")}
        </label>
        <div className="flex gap-3">
          <Button variant="ghost" onClick={() => router.back()}>
            {tc("cancel")}
          </Button>
          <Button onClick={handleSubmit} disabled={loading || !title.trim() || isRichContentEmpty(content)}>
            {loading ? t("writing") : t("submitPost")}
          </Button>
        </div>
      </div>
    </div>
  );
}
