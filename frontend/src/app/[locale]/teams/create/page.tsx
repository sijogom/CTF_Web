"use client";

import { useState } from "react";
import { useRouter } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { apiFetch, ApiError } from "@/lib/api-client";
import { isRichContentEmpty } from "@/lib/rich-content.mjs";

export default function CreateTeamPage() {
  const t = useTranslations("teams");
  const tc = useTranslations("common");
  const router = useRouter();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleCreate = async () => {
    if (!name.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const result = await apiFetch<{ id: string }>("/api/auth/teams", {
        method: "POST",
        body: { name: name.trim(), description: isRichContentEmpty(description) ? null : description },
      });
      router.push(`/teams/${result.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : tc("errorOccurred"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mx-auto max-w-lg space-y-6">
      <h1 className="text-2xl font-bold">{t("createTeam")}</h1>

      {error && (
        <div className="rounded-lg border border-status-error/30 bg-status-error/10 px-4 py-3 text-sm text-status-error">
          {error}
        </div>
      )}

      <div>
        <label className="mb-2 block text-sm font-medium text-text-secondary">{t("teamName")}</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={t("teamNamePlaceholder")}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
        />
      </div>

      <div>
        <label className="mb-2 block text-sm font-medium text-text-secondary">{t("teamDesc")}</label>
        <RichTextEditor
          value={description}
          onChange={setDescription}
          placeholder={t("teamDescPlaceholder")}
          ariaLabel={t("teamDesc")}
        />
      </div>

      <div className="flex justify-end gap-3">
        <Button variant="ghost" onClick={() => router.back()}>{tc("cancel")}</Button>
        <Button onClick={handleCreate} disabled={loading || !name.trim()}>
          {loading ? t("creating") : t("create")}
        </Button>
      </div>
    </div>
  );
}
