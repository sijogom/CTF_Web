"use client";

import { useTranslations } from "next-intl";
import type { ChallengeFormData } from "./ChallengeWizard";
import { Button } from "@/components/ui/Button";
import { CategoryIcon } from "@/components/ui/CategoryIcon";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { FileText } from "lucide-react";

interface Props {
  data: ChallengeFormData;
  onChange: (partial: Partial<ChallengeFormData>) => void;
  onSubmit: () => void;
  submitLabel?: string;
}

export function StepPreview({ data, onChange, onSubmit, submitLabel }: Props) {
  const t = useTranslations("challenge");
  const tc = useTranslations("common");
  const td = useTranslations("difficulty");
  const attachments = data.attachments ?? [];
  const previewFiles = [
    ...data.files.map((name, index) => ({
      key: `legacy-${index}`, name: name.split("/").pop() || name,
      unavailable: attachments.find((item) => item.legacy_index === index)?.state !== "ready",
    })),
    ...attachments.filter((item) => item.id !== null).map((item) => ({
      key: item.id as string, name: item.name, unavailable: item.state !== "ready",
    })),
  ];
  return (
    <div className="space-y-5">
      <h2 className="text-lg font-semibold">{t("previewTitle")}</h2>
      <p className="text-sm text-text-secondary">
        {t("previewDesc")}
      </p>

      <div className="rounded-xl border border-border bg-bg-surface p-6">
        {/* 제목 + 메타 */}
        <div className="mb-4 flex items-start justify-between">
          <div>
            <h3 className="text-xl font-bold">
              {data.title || t("noTitle")}
            </h3>
            <div className="mt-2 flex items-center gap-3 text-sm text-text-secondary">
              <CategoryIcon
                category={data.category as "web" | "pwn" | "rev" | "crypto" | "forensic" | "misc"}
                size="sm"
              />
              <span>{data.category.toUpperCase()}</span>
              <span>|</span>
              <span>{td(data.difficulty)}</span>
              <span>|</span>
              <span>{data.points}{tc("pointsUnit")}</span>
            </div>
          </div>
        </div>

        {/* 설명 */}
        <div className="mb-4 rounded-lg bg-bg-primary p-4">
          <p className="mb-1 text-xs font-medium text-text-muted">{t("descriptionLabel")}</p>
          {data.description ? (
            <MarkdownRenderer content={data.description} />
          ) : (
            <p className="text-sm text-text-muted">{t("noDescription")}</p>
          )}
        </div>

        {/* 플래그 */}
        <div className="mb-4 rounded-lg bg-bg-primary p-4">
          <p className="mb-1 text-xs font-medium text-text-muted">{t("flagLabel")}</p>
          {data.flagType === "dynamic_instance" ? (
            <div className="space-y-1 text-sm text-text-secondary">
              <p className="font-medium text-accent-primary">{t("flagTypeDynamic")}</p>
              <p>
                {t("dynamicFlagPreview", {
                  path: data.dynamicFlag?.runtimePath ?? "/flag.txt",
                  ttl: data.dynamicFlag?.ttlSeconds ?? 0,
                })}
              </p>
              <p className="text-xs text-text-muted">{t("dynamicFlagPreviewNoRaw")}</p>
            </div>
          ) : data.flags.length > 0 ? (
            <ul className="space-y-1">
              {data.flags.map((f, i) => (
                <li key={i}>
                  <code className="text-sm text-accent-primary">
                    [{f.type}] {f.value || t("notSet")}
                    {f.caseSensitive ? "" : ` (${t("caseSensitive")}: OFF)`}
                  </code>
                </li>
              ))}
            </ul>
          ) : (
            <code className="text-sm text-accent-primary">
              [{data.flagType}] {data.flagValue || t("notSet")}
            </code>
          )}
        </div>

        {/* 파일 */}
        {previewFiles.length > 0 && (
          <section className="mb-4 border-t border-border pt-4">
            <p className="mb-1 text-xs font-medium text-text-muted">
              {t("attachmentFiles")} ({previewFiles.length})
            </p>
            <ul className="text-sm">
              {previewFiles.map((file) => (
                <li key={file.key} className="flex min-w-0 items-start gap-2 py-1">
                  <FileText size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
                  <span className="min-w-0 break-all">{file.name}</span>
                  {file.unavailable && <span className="text-xs text-accent-warning">{t("attachmentUnavailable")}</span>}
                </li>
              ))}
            </ul>
          </section>
        )}

        {/* 환경 */}
        {data.envType !== "none" && (
          <div className="mb-4 rounded-lg bg-bg-primary p-4">
            <p className="mb-1 text-xs font-medium text-text-muted">
              {t("practiceEnvTitle")}
            </p>
            <p className="text-sm">
              {data.envType === "dockerfile"
                ? t("envDockerfileBased")
                : t("envImageName", { name: data.imageName })}
            </p>
          </div>
        )}

        {/* 힌트 */}
        {data.hints.length > 0 && (
          <div className="rounded-lg bg-bg-primary p-4">
            <p className="mb-1 text-xs font-medium text-text-muted">
              {t("hintsTitle")} ({data.hints.length})
            </p>
            <ul className="space-y-1 text-sm">
              {data.hints.map((h, i) => (
                <li key={i}>
                  <div className="rounded-lg border border-border/70 p-3">
                    <p className="mb-2 font-medium">💡 {t("hintCostLine", { n: i + 1, cost: h.cost })}</p>
                    {h.content ? <MarkdownRenderer content={h.content} /> : null}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* 공개 설정 */}
      <label className="flex cursor-pointer items-center gap-2">
        <input
          type="checkbox"
          checked={data.isPublished}
          onChange={(e) => onChange({ isPublished: e.target.checked })}
          className="h-4 w-4 rounded"
        />
        <span className="text-sm">{t("publishImmediately")}</span>
      </label>

      <Button onClick={onSubmit} size="lg" className="w-full">
        {submitLabel ?? t("submitChallenge")}
      </Button>
    </div>
  );
}
