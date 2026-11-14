"use client";

import { useRef } from "react";
import { useTranslations } from "next-intl";
import { ImageUp } from "lucide-react";
import { apiFormFetch } from "@/lib/api-client";
import { uploadEditorImage, type ImageEditOperation } from "@/lib/image-edit-operation.mjs";

interface BannerUploadZoneProps {
  bannerUrl: string | null;
  bannerUploading: boolean;
  disabled: boolean;
  operation: ImageEditOperation;
  onBannerChange: (url: string) => void;
  onError: (msg: string | null) => void;
}

export function BannerUploadZone({
  bannerUrl,
  bannerUploading,
  disabled,
  operation,
  onBannerChange,
  onError,
}: BannerUploadZoneProps) {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const inputRef = useRef<HTMLInputElement>(null);

  const handleUpload = (file: File) => {
    if (disabled) return;
    return uploadEditorImage({
      operation, file, kind: "banner", onSuccess: onBannerChange, onError,
      messages: { type: tc("imageOnlyError"), size: t("bannerTooLarge"), failed: t("bannerUploadFailed") },
      upload: async (file, requestOwner) => {
        const formData = new FormData();
        formData.append("file", file);
        const data = await apiFormFetch<{ banner_url: string }>(
          "/api/ctf/competitions/upload-banner", formData, { requestOwner },
        );
        return data.banner_url;
      },
    });
  };

  return (
    <div>
      <label className="mb-1.5 block text-sm font-medium">{t("bannerImage")}</label>
      <button
        type="button"
        disabled={disabled}
        aria-label={bannerUrl ? t("changeBanner") : t("bannerImage")}
        aria-busy={bannerUploading}
        className="group relative flex h-40 w-full items-center justify-center overflow-hidden rounded-lg border-2 border-dashed border-border bg-bg-surface transition-colors hover:border-accent-primary/50 focus-visible:outline-2 focus-visible:outline-accent-primary disabled:cursor-wait"
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => { e.preventDefault(); e.currentTarget.classList.add("border-accent-primary"); }}
        onDragLeave={(e) => { e.currentTarget.classList.remove("border-accent-primary"); }}
        onDrop={(e) => {
          e.preventDefault();
          e.currentTarget.classList.remove("border-accent-primary");
          const file = e.dataTransfer.files[0];
          if (file) handleUpload(file);
        }}
      >
        {bannerUrl ? (
          <>
            <img src={bannerUrl} alt={t("bannerPreview")} loading="lazy" className="h-full w-full object-cover" />
            <div className="absolute inset-0 flex items-center justify-center bg-black/50 opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
              <span className="text-sm font-medium text-white">{bannerUploading ? tc("uploadingImage") : t("changeBanner")}</span>
            </div>
          </>
        ) : (
          <div className="flex flex-col items-center gap-2 p-6 text-center">
            <ImageUp aria-hidden="true" className="h-10 w-10 text-text-muted" />
            <p className="text-sm text-text-muted">
              {bannerUploading ? tc("uploadingImage") : t("bannerUploadHint")}
            </p>
          </div>
        )}
      </button>
      <input
        ref={inputRef}
        type="file"
        disabled={disabled}
        accept="image/jpeg,image/png,image/webp,image/gif"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) handleUpload(file);
          e.target.value = "";
        }}
      />
      {bannerUploading && <p role="status" className="mt-1 text-sm text-text-muted">{tc("uploadingImage")}</p>}
    </div>
  );
}
