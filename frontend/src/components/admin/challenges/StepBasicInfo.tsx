"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useTranslations } from "next-intl";
import { ImageUp } from "lucide-react";
import type { ChallengeFormData } from "./ChallengeWizard";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { apiFetch, apiFormFetch } from "@/lib/api-client";
import { TagSelector } from "./TagSelector";
import { LearningPrereqSelector } from "./LearningPrereqSelector";
import { RecommendedPrereqSelector } from "./RecommendedPrereqSelector";
import { KnowledgeTagMappingEditor } from "@/components/admin/knowledge/KnowledgeTagMappingEditor";
import type { MappingEditorController } from "@/lib/use-knowledge-tag-mapping-draft";
import { uploadEditorImage, type ImageEditOperation } from "@/lib/image-edit-operation.mjs";

const CATEGORIES = ["web", "pwn", "rev", "crypto", "forensic", "misc"];
const DIFFICULTIES = ["beginner", "easy", "medium_low", "medium", "medium_high", "hard", "expert", "impossible"];

interface Props {
  data: ChallengeFormData;
  onChange: (partial: Partial<ChallengeFormData>) => void;
  challengeId?: number;
  mode?: "create" | "edit";
  mappingController: MappingEditorController;
  operation: ImageEditOperation;
  thumbnailUploading: boolean;
  disabled?: boolean;
}

interface ChallengeOption {
  id: number;
  name: string;
  category: string;
}

export function StepBasicInfo({ data, onChange, challengeId, mode = "create", mappingController, operation, thumbnailUploading, disabled = false }: Props) {
  const tc = useTranslations("challenge");
  const tco = useTranslations("common");
  const td = useTranslations("difficulty");
  const ta = useTranslations("admin");
  const [allChallenges, setAllChallenges] = useState<ChallengeOption[]>([]);
  const [prereqSearch, setPrereqSearch] = useState("");

  const fetchChallenges = useCallback(async () => {
    try {
      const res = await apiFetch<{ items: ChallengeOption[] }>(
        "/api/learn/challenges?size=100&show_all=true",
      );
      setAllChallenges(res.items);
    } catch {
      // 목록 로딩 실패 — 무시
    }
  }, []);

  useEffect(() => {
    fetchChallenges();
  }, [fetchChallenges]);

  const filteredChallenges = allChallenges.filter(
    (c) =>
      !data.prerequisiteIds.includes(c.id) &&
      (prereqSearch === "" || c.name.toLowerCase().includes(prereqSearch.toLowerCase())),
  );

  const addPrerequisite = (id: number) => {
    onChange({ prerequisiteIds: [...data.prerequisiteIds, id] });
    setPrereqSearch("");
  };

  const removePrerequisite = (id: number) => {
    onChange({ prerequisiteIds: data.prerequisiteIds.filter((pid) => pid !== id) });
  };

  const thumbInputRef = useRef<HTMLInputElement>(null);
  const [thumbnailError, setThumbnailError] = useState<string | null>(null);

  const handleThumbnailUpload = (file: File) => {
    if (disabled) return;
    return uploadEditorImage({
      operation, file, kind: "thumbnail",
      onSuccess: (url) => onChange({ thumbnailUrl: url }),
      onError: setThumbnailError,
      messages: { type: tco("imageOnlyError"), size: tco("imageSizeError"), failed: ta("thumbnailUploadFailed") },
      upload: async (file, requestOwner) => {
        const formData = new FormData();
        formData.append("file", file);
        const res = await apiFormFetch<{ url: string }>(
          "/api/learn/lessons/upload-image", formData, { requestOwner },
        );
        return res.url;
      },
    });
  };

  const removeThumbnail = () => {
    if (disabled) return;
    let lease;
    try {
      lease = operation.claim("image-change");
      if (!lease) return;
      onChange({ thumbnailUrl: null });
      setThumbnailError(null);
    } catch {
      setThumbnailError(ta("thumbnailUploadFailed"));
    } finally {
      if (lease) operation.release(lease);
    }
  };

  return (
    <div className="space-y-5">
      {/* 제목 */}
      <div>
        <label htmlFor="title" className="mb-1.5 block text-sm font-medium">
          {tc("titleLabel")} <span className="text-accent-error">*</span>
        </label>
        <input
          id="title"
          value={data.title}
          onChange={(e) => onChange({ title: e.target.value })}
          placeholder={tc("titlePlaceholder")}
          className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none"
        />
      </div>

      {/* 문제 유형 */}
      <div>
        <label className="mb-1.5 block text-sm font-medium">
          {ta("challengeTypeLabel")}
        </label>
        <div className="flex gap-2">
          {(["wargame", "competition"] as const).map((ct) => (
            <button
              key={ct}
              type="button"
              onClick={() => onChange({ challengeType: ct })}
              className={`rounded-lg px-4 py-2.5 text-sm font-medium transition-colors ${
                data.challengeType === ct
                  ? "bg-accent-primary text-white"
                  : "border border-border bg-bg-surface text-text-secondary hover:text-text-primary"
              }`}
            >
              {ta(`challengeType_${ct}`)}
            </button>
          ))}
        </div>
      </div>

      {/* 카테고리 + 난이도 */}
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label htmlFor="category" className="mb-1.5 block text-sm font-medium">
            {tc("category")}
          </label>
          <select
            id="category"
            value={data.category}
            onChange={(e) => onChange({ category: e.target.value })}
            className="w-full rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm"
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c.charAt(0).toUpperCase() + c.slice(1)}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="difficulty" className="mb-1.5 block text-sm font-medium">
            {tc("difficulty")}
          </label>
          <select
            id="difficulty"
            value={data.difficulty}
            onChange={(e) => onChange({ difficulty: e.target.value })}
            className="w-full rounded-lg border border-border bg-bg-primary px-3 py-3 text-sm"
          >
            {DIFFICULTIES.map((d) => (
              <option key={d} value={d}>
                {td(d)}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* 포인트 + XP */}
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label htmlFor="points" className="mb-1.5 block text-sm font-medium">
            {tc("points")}
          </label>
          <input
            id="points"
            type="number"
            value={data.points}
            onChange={(e) => onChange({ points: parseInt(e.target.value) || 0 })}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm"
          />
        </div>
        <div>
          <label htmlFor="xp" className="mb-1.5 block text-sm font-medium">
            {tco("xpLabel")}
          </label>
          <input
            id="xp"
            type="number"
            value={data.xp}
            onChange={(e) => onChange({ xp: parseInt(e.target.value) || 0 })}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm"
          />
        </div>
      </div>

      {/* 대표 이미지 */}
      <div>
        <label className="mb-1.5 block text-sm font-medium">
          {ta("thumbnailImage")}
        </label>
        <button
          type="button"
          disabled={disabled}
          aria-label={data.thumbnailUrl ? ta("changeThumbnail") : ta("thumbnailImage")}
          aria-busy={thumbnailUploading}
          aria-describedby={thumbnailError ? "thumbnail-upload-error" : undefined}
          className="group relative flex h-40 w-full items-center justify-center overflow-hidden rounded-lg border-2 border-dashed border-border bg-bg-surface transition-colors hover:border-accent-primary/50 focus-visible:outline-2 focus-visible:outline-accent-primary disabled:cursor-wait"
          onClick={() => thumbInputRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); e.currentTarget.classList.add("border-accent-primary"); }}
          onDragLeave={(e) => { e.currentTarget.classList.remove("border-accent-primary"); }}
          onDrop={(e) => {
            e.preventDefault();
            e.currentTarget.classList.remove("border-accent-primary");
            const file = e.dataTransfer.files[0];
            if (file) handleThumbnailUpload(file);
          }}
        >
          {data.thumbnailUrl ? (
            <>
              <img src={data.thumbnailUrl} alt={ta("thumbnailImage")} loading="lazy" className="h-full w-full object-cover" />
              <div className="absolute inset-0 flex items-center justify-center bg-black/50 opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
                <span className="text-sm font-medium text-white">{thumbnailUploading ? tco("uploadingImage") : ta("changeThumbnail")}</span>
              </div>
            </>
          ) : (
            <div className="flex flex-col items-center gap-2 p-4 text-center">
              <ImageUp aria-hidden="true" className="h-8 w-8 text-text-muted" />
              <p className="text-xs text-text-muted">{thumbnailUploading ? tco("uploadingImage") : ta("thumbnailUploadHint")}</p>
            </div>
          )}
        </button>
        <input
            ref={thumbInputRef}
            type="file"
            disabled={disabled}
            accept="image/jpeg,image/png,image/webp,image/gif,.png,.jpg,.jpeg,.gif,.webp"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) handleThumbnailUpload(file);
              e.target.value = "";
            }}
          />
        {thumbnailUploading && <p role="status" className="mt-1 text-sm text-text-muted">{tco("uploadingImage")}</p>}
        {thumbnailError && <p id="thumbnail-upload-error" role="alert" className="mt-1 text-sm text-accent-error">{thumbnailError}</p>}
        {data.thumbnailUrl && (
          <button
            type="button"
            disabled={disabled}
            onClick={removeThumbnail}
            className="mt-1 text-xs text-accent-error hover:underline disabled:cursor-not-allowed disabled:opacity-50"
          >
            {ta("removeThumbnail")}
          </button>
        )}
      </div>

      {/* 설명 (HTML) */}
      <div>
        <label className="mb-1.5 block text-sm font-medium">
          {tc("descriptionLabel")} <span className="text-accent-error">*</span>
        </label>
        <RichTextEditor
          value={data.description}
          onChange={(v) => onChange({ description: v })}
          placeholder={tc("descriptionPlaceholder")}
          ariaLabel={tc("descriptionLabel")}
          enableImageUpload
          imageUploadUrl="/api/learn/lessons/upload-image"
        />
      </div>

      {/* 지식 태그 */}
      {mode === "edit" && challengeId ? (
        <KnowledgeTagMappingEditor
          entityType="challenge"
          entityId={String(challengeId)}
          controller={mappingController}
          disabled={disabled}
        />
      ) : (
        <TagSelector
          selectedIds={data.knowledgeTagIds}
          onChange={(ids) => onChange({ knowledgeTagIds: ids })}
        />
      )}

      {/* ── 필수 선행조건 블록 ── */}
      <div className="rounded-xl border border-border bg-bg-surface/50 p-5 space-y-4">
        <h3 className="text-sm font-semibold text-text-primary">🔒 {ta("requiredPrerequisites") ?? "필수 선행조건"}</h3>
        <p className="text-xs text-text-muted">{ta("requiredPrerequisitesDesc") ?? "이 조건을 충족하지 않으면 문제가 잠깁니다."}</p>

        {/* 선행 문제 */}
        <div>
          <label className="mb-1.5 block text-sm font-medium">
            {tc("prerequisites")}
          </label>
          <p className="mb-2 text-xs text-text-muted">{tc("prerequisitesDesc")}</p>

          {data.prerequisiteIds.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-2">
              {data.prerequisiteIds.map((pid) => {
                const ch = allChallenges.find((c) => c.id === pid);
                return (
                  <span
                    key={pid}
                    className="inline-flex items-center gap-1.5 rounded-full bg-accent-primary/10 px-3 py-1 text-xs font-medium text-accent-primary"
                  >
                    #{pid} {ch?.name ?? ""}
                    <button
                      type="button"
                      onClick={() => removePrerequisite(pid)}
                      className="text-accent-primary/60 hover:text-accent-error"
                    >
                      &times;
                    </button>
                  </span>
                );
              })}
            </div>
          )}

          <div className="relative">
            <input
              value={prereqSearch}
              onChange={(e) => setPrereqSearch(e.target.value)}
              onBlur={() => setTimeout(() => setPrereqSearch(""), 200)}
              placeholder={tc("prerequisitesSearch")}
              aria-label={tc("prerequisitesSearch")}
              className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm focus:border-accent-primary focus:outline-none"
            />
            {prereqSearch && filteredChallenges.length > 0 && (
              <div className="absolute z-20 mt-1 max-h-40 w-full overflow-y-auto rounded-lg border border-border bg-bg-surface shadow-lg">
                {filteredChallenges.slice(0, 10).map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => addPrerequisite(c.id)}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-text-secondary hover:bg-bg-elevated hover:text-text-primary"
                  >
                    <span className="text-text-muted">#{c.id}</span>
                    <span>{c.name}</span>
                    <span className="ml-auto text-xs text-text-muted">{c.category}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* 선행 학습 */}
        <LearningPrereqSelector
          selected={data.learningPrerequisites}
          onChange={(items) => onChange({ learningPrerequisites: items })}
        />
      </div>

      {/* ── 추천 선행조건 블록 ── */}
      <div className="rounded-xl border border-accent-warning/20 bg-accent-warning/5 p-5 space-y-4">
        <h3 className="text-sm font-semibold text-accent-warning">💡 {ta("recommendedPrerequisites") ?? "추천 선행조건"}</h3>
        <p className="text-xs text-text-muted">{ta("recommendedPrerequisitesDesc") ?? "강제 아님 — 이 문제를 풀기 전에 도움이 되는 문제나 학습을 안내합니다."}</p>

        <RecommendedPrereqSelector
          recommendedChallenges={data.recommendedChallenges}
          recommendedLearning={data.recommendedLearning}
          onChangeChallenges={(ids) => onChange({ recommendedChallenges: ids })}
          onChangeLearning={(items) => onChange({ recommendedLearning: items })}
          allChallenges={allChallenges}
        />
      </div>
    </div>
  );
}
