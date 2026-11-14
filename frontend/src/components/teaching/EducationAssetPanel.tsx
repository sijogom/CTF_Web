"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { ApiError, apiFetch, apiFormFetch } from "@/lib/api-client";
import {
  EDUCATION_API,
  educationAssetDownloadHref,
  getAllAssignmentAssets,
  getAllClassAssets,
  getClassAssetUsage,
} from "@/components/teaching/education-api";
import { formatEducationDate } from "@/components/teaching/education-format";
import type { EducationAsset, EducationAssetUsage } from "@/types/education";

interface EducationAssetPanelProps {
  scope: "class" | "assignment";
  targetId: string;
  canManage: boolean;
}

function formatByteSize(value: number, locale: string): string {
  if (!Number.isFinite(value) || value < 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: index === 0 ? 0 : 1 }).format(size)} ${units[index]}`;
}

export function EducationAssetPanel({ scope, targetId, canManage }: EducationAssetPanelProps) {
  const t = useTranslations("education");
  const tc = useTranslations("common");
  const locale = useLocale();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [assets, setAssets] = useState<EducationAsset[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<EducationAsset | null>(null);
  const [usage, setUsage] = useState<EducationAssetUsage | null>(null);
  const [deleteReason, setDeleteReason] = useState("");
  const [deleting, setDeleting] = useState(false);
  const requestGenerationRef = useRef(0);

  const assetPath = scope === "class"
    ? `${EDUCATION_API}/classes/${targetId}/assets`
    : `${EDUCATION_API}/assignments/${targetId}/assets`;
  const title = t(scope === "class" ? "assets.classTitle" : "assets.assignmentTitle");

  const loadAssets = useCallback(async () => {
    const generation = ++requestGenerationRef.current;
    setLoading(true);
    setError(null);
    try {
      const [response, nextUsage] = await Promise.all([
        scope === "class" ? getAllClassAssets(targetId) : getAllAssignmentAssets(targetId),
        scope === "class" && canManage ? getClassAssetUsage(targetId) : Promise.resolve(null),
      ]);
      if (generation !== requestGenerationRef.current) return;
      setAssets(response);
      setUsage(nextUsage);
    } catch (requestError) {
      if (generation !== requestGenerationRef.current) return;
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      if (generation === requestGenerationRef.current) setLoading(false);
    }
  }, [scope, targetId, tc, canManage]);

  useEffect(() => {
    void loadAssets();
    return () => { requestGenerationRef.current += 1; };
  }, [loadAssets]);

  const uploadAsset = async (file: File) => {
    setUploading(true);
    setError(null);
    try {
      const data = new FormData();
      data.append("file", file);
      await apiFormFetch<EducationAsset>(assetPath, data);
      await loadAssets();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setUploading(false);
    }
  };

  const deleteAsset = async () => {
    if (!deleteTarget || !deleteReason.trim()) return;
    setDeleting(true);
    setError(null);
    try {
      await apiFetch(`/api/learn/education/assets/${deleteTarget.id}`, {
        method: "DELETE",
        body: { reason: deleteReason.trim() },
      });
      setDeleteTarget(null);
      setDeleteReason("");
      await loadAssets();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <section className="rounded-xl border border-border bg-bg-surface p-5" aria-labelledby={`education-assets-${scope}-${targetId}`}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 id={`education-assets-${scope}-${targetId}`} className="text-lg font-semibold">{title}</h2>
          <p className="mt-1 text-sm text-text-secondary">{canManage ? t("assets.managerDescription") : t("assets.studentDescription")}</p>
        </div>
        {canManage && (
          <>
            <input ref={fileInputRef} type="file" className="sr-only" onChange={(event) => {
              const file = event.currentTarget.files?.[0];
              if (file) void uploadAsset(file);
              event.currentTarget.value = "";
            }} />
            <Button type="button" onClick={() => fileInputRef.current?.click()} disabled={uploading}>{uploading ? tc("processing") : t("assets.upload")}</Button>
          </>
        )}
      </div>
      {error && <p role="alert" className="mt-4 text-sm text-accent-error">{error}</p>}
      {usage && <p className="mt-3 text-xs text-text-muted">{t("assets.usage", { count: usage.active_asset_count, maxAssets: usage.max_assets, used: formatByteSize(usage.active_byte_size, locale), maxBytes: formatByteSize(usage.max_byte_size, locale), maxFile: formatByteSize(usage.max_file_size, locale) })}</p>}
      {loading && <div className="mt-4 h-20 animate-pulse rounded-lg bg-bg-primary" aria-busy="true" />}
      {!loading && assets.length === 0 && <p className="mt-4 text-sm text-text-muted">{t("assets.empty")}</p>}
      {!loading && assets.length > 0 && <ul className="mt-4 divide-y divide-border rounded-lg border border-border bg-bg-primary">{assets.map((asset) => <li key={asset.id} className="flex flex-col gap-2 p-3 sm:flex-row sm:items-center sm:justify-between"><div className="min-w-0"><a href={educationAssetDownloadHref(asset.id)} className="block truncate text-sm font-medium text-accent-primary hover:underline">{asset.original_name}</a><p className="mt-1 text-xs text-text-muted">{formatByteSize(asset.byte_size, locale)} · {formatEducationDate(asset.created_at, locale)}</p></div>{canManage && <button type="button" onClick={() => setDeleteTarget(asset)} className="shrink-0 text-sm font-medium text-accent-error hover:underline">{tc("delete")}</button>}</li>)}</ul>}
      {deleteTarget && <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/50 p-4"><section role="alertdialog" aria-modal="true" aria-labelledby="education-asset-delete-title" className="w-full max-w-md rounded-xl border border-border bg-bg-primary p-6"><h3 id="education-asset-delete-title" className="text-lg font-semibold">{t("assets.deleteTitle")}</h3><p className="mt-2 text-sm text-text-secondary">{t("assets.deleteDescription", { name: deleteTarget.original_name })}</p><label htmlFor="education-asset-delete-reason" className="mt-4 block text-sm font-medium">{t("assets.deleteReason")}</label><textarea id="education-asset-delete-reason" rows={3} maxLength={1000} value={deleteReason} onChange={(event) => setDeleteReason(event.target.value)} className="mt-1 w-full resize-y rounded-lg border border-border bg-bg-surface px-3 py-2 text-sm focus:border-accent-primary focus:outline-none" /><div className="mt-5 flex justify-end gap-3"><Button type="button" variant="ghost" onClick={() => { setDeleteTarget(null); setDeleteReason(""); }} disabled={deleting}>{tc("cancel")}</Button><Button type="button" variant="danger" onClick={() => void deleteAsset()} disabled={deleting || !deleteReason.trim()}>{deleting ? tc("processing") : tc("delete")}</Button></div></section></div>}
    </section>
  );
}
