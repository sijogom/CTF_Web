"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useTranslations } from "next-intl";
import { Trash2, Upload } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { TierImagePreview } from "@/components/admin/tiers/TierImagePreview";
import { apiFetch, apiFormFetch, ApiError, captureApiRequestOwner, isCurrentApiRequestOwner, type ApiRequestOwner } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import { useAuth } from "@/providers/AuthProvider";

interface TierConfig {
  name: string;
  image_url: string;
  required_xp: number;
}

interface TierRow extends TierConfig {
  rowId: string;
}

interface TierOperation {
  kind: "save" | "upload";
  generation: number;
  owner: ApiRequestOwner;
  controller: AbortController;
}

function tierPayload(rows: TierRow[]): TierConfig[] {
  return rows.map(({ name, image_url, required_xp }) => ({ name, image_url, required_xp }));
}

export default function TierManagementPage() {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const { toast } = useToast();
  const { user, isSuperAdmin } = useAuth();

  const [tiers, setTiers] = useState<TierRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [uploadingRowId, setUploadingRowId] = useState<string | null>(null);
  const tiersRef = useRef<TierRow[]>([]);
  const nextRowIdRef = useRef(0);
  const loadedForUserRef = useRef<string | null>(null);
  const mountedRef = useRef(false);
  const loadControllerRef = useRef<AbortController | null>(null);
  const operationRef = useRef<TierOperation | null>(null);
  const requestGenerationRef = useRef(0);
  const busy = saving || uploadingRowId !== null;

  const replaceTiers = useCallback((rows: TierRow[]) => {
    tiersRef.current = rows;
    setTiers(rows);
  }, []);

  const createTierRow = useCallback((tier: TierConfig): TierRow => ({
    ...tier, rowId: `tier-${++nextRowIdRef.current}`,
  }), []);

  const loadTiers = useCallback(async () => {
    const generation = ++requestGenerationRef.current;
    loadControllerRef.current?.abort();
    const controller = new AbortController();
    loadControllerRef.current = controller;
    loadedForUserRef.current = null;
    setLoading(true);
    setLoaded(false);
    setLoadError(null);
    let owner: ApiRequestOwner | null = null;
    try {
      if (!user?.id) throw new Error("tier_owner_unavailable");
      owner = captureApiRequestOwner(user.id);
      const data = await apiFetch<{ tiers?: TierConfig[] }>("/api/auth/admin/settings", { requestOwner: owner, signal: controller.signal });
      if (!mountedRef.current || generation !== requestGenerationRef.current || !isCurrentApiRequestOwner(owner)) return;
      if (!Array.isArray(data.tiers)) throw new Error("invalid_tier_response");
      replaceTiers([...data.tiers].sort((a, b) => a.required_xp - b.required_xp).map(createTierRow));
      loadedForUserRef.current = user.id;
      setLoaded(true);
    } catch (error) {
      if (!mountedRef.current || generation !== requestGenerationRef.current || (owner && !isCurrentApiRequestOwner(owner))) return;
      setLoadError(error instanceof ApiError ? error.message : t("settingsLoadFailed"));
    } finally {
      if (mountedRef.current && generation === requestGenerationRef.current) setLoading(false);
    }
  }, [createTierRow, replaceTiers, t, user?.id]);

  useEffect(() => {
    mountedRef.current = true;
    setSaving(false);
    setUploadingRowId(null);
    void loadTiers();
    return () => {
      mountedRef.current = false;
      requestGenerationRef.current += 1;
      loadedForUserRef.current = null;
      loadControllerRef.current?.abort();
      operationRef.current?.controller.abort();
      operationRef.current = null;
    };
  }, [loadTiers]);

  const canEdit = () => {
    if (!mountedRef.current || operationRef.current || !isSuperAdmin || !user?.id || loadedForUserRef.current !== user.id) return false;
    try {
      return isCurrentApiRequestOwner(captureApiRequestOwner(user.id));
    } catch {
      return false;
    }
  };

  const addTier = () => {
    if (!canEdit()) return;
    const current = tiersRef.current;
    const maxXp = current.length > 0 ? Math.max(...current.map((tier) => tier.required_xp)) + 1000 : 0;
    replaceTiers([...current, createTierRow({ name: "", image_url: "", required_xp: maxXp })]);
  };

  const removeTier = (rowId: string) => {
    if (!canEdit()) return;
    replaceTiers(tiersRef.current.filter((tier) => tier.rowId !== rowId));
  };

  const updateTier = (rowId: string, field: keyof TierConfig, value: string | number) => {
    if (!canEdit()) return;
    replaceTiers(tiersRef.current.map((tier) => tier.rowId === rowId ? { ...tier, [field]: value } : tier));
  };

  const beginOperation = (kind: TierOperation["kind"]): TierOperation | null => {
    if (!canEdit() || !user?.id) return null;
    const operation: TierOperation = { kind, generation: requestGenerationRef.current,
      owner: captureApiRequestOwner(user.id), controller: new AbortController() };
    operationRef.current = operation;
    return operation;
  };

  const isCurrentOperation = (operation: TierOperation) => mountedRef.current && operationRef.current === operation
    && operation.generation === requestGenerationRef.current && isCurrentApiRequestOwner(operation.owner);

  const finishOperation = (operation: TierOperation) => {
    if (operationRef.current !== operation) return;
    operationRef.current = null;
    if (mountedRef.current && operation.generation === requestGenerationRef.current) {
      setSaving(false);
      setUploadingRowId(null);
    }
  };

  const handleUpload = async (rowId: string, file: File) => {
    if (!tiersRef.current.some((tier) => tier.rowId === rowId)) return;
    const operation = beginOperation("upload");
    if (!operation) return;
    setUploadingRowId(rowId);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const response = await apiFormFetch<{ url: string }>("/api/learn/lessons/upload-image", formData,
        { requestOwner: operation.owner, signal: operation.controller.signal });
      if (!isCurrentOperation(operation)) return;
      if (typeof response.url !== "string" || !response.url.trim()) throw new Error("invalid_tier_image_response");
      replaceTiers(tiersRef.current.map((tier) => tier.rowId === rowId ? { ...tier, image_url: response.url } : tier));
      toast("success", t("tierImageUploaded"));
    } catch (error) {
      if (isCurrentOperation(operation)) toast("error", error instanceof ApiError ? error.message : t("tierImageUploadFailed"));
    } finally {
      finishOperation(operation);
    }
  };

  const handleSave = async () => {
    const operation = beginOperation("save");
    if (!operation) return;
    // XP 오름차순 정렬
    const sorted = [...tiersRef.current]
      .filter((t) => t.name.trim())
      .sort((a, b) => a.required_xp - b.required_xp);

    setSaving(true);
    try {
      await apiFetch("/api/auth/admin/settings", {
        method: "PATCH",
        body: { tiers: tierPayload(sorted) },
        requestOwner: operation.owner,
        signal: operation.controller.signal,
      });
      if (!isCurrentOperation(operation)) return;
      replaceTiers(sorted);
      toast("success", t("tierSaved"));
    } catch (err) {
      if (isCurrentOperation(operation)) toast("error", err instanceof ApiError ? err.message : t("tierSaveFailed"));
    } finally {
      finishOperation(operation);
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }

  if (!loaded || loadError) {
    return <div className="space-y-4">
      <h1 className="text-2xl font-bold">{t("tierManagement")}</h1>
      <p role="alert" className="text-sm text-accent-error">{loadError || t("settingsLoadFailed")}</p>
      <Button type="button" variant="secondary" onClick={() => void loadTiers()}>{tc("retry")}</Button>
    </div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold">{t("tierManagement")}</h1>
        <Button onClick={addTier} disabled={busy || !isSuperAdmin}>{t("addTier")}</Button>
      </div>

      <p className="text-sm text-text-muted">
        {t("tierAssignmentDescription")}
      </p>

      <fieldset disabled={busy || !isSuperAdmin} className="min-w-0">
      {tiers.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border bg-bg-surface p-8 text-center text-text-muted">
          {t("noTiers")}
        </div>
      ) : (
        <div className="space-y-3">
          {tiers.map((tier) => (
              <div
                key={tier.rowId}
                className="flex min-w-0 flex-wrap items-start gap-3 rounded-lg border border-border bg-bg-surface p-4"
              >
                <TierImagePreview key={JSON.stringify([tier.rowId, tier.image_url])}
                  imageUrl={tier.image_url} name={tier.name} errorLabel={t("tierImagePreviewFailed")} />

                <div className="min-w-0 flex-[1_1_10rem]">
                  <label htmlFor={`${tier.rowId}-name`} className="mb-1 block text-xs text-text-muted">{t("tierName")}</label>
                  <input
                    id={`${tier.rowId}-name`}
                    value={tier.name}
                    onChange={(e) => updateTier(tier.rowId, "name", e.target.value)}
                    placeholder="Bronze"
                    className="min-h-11 w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
                  />
                </div>

                <div className="min-w-0 flex-[2_1_16rem]">
                  <label htmlFor={`${tier.rowId}-image`} className="mb-1 block text-xs text-text-muted">{t("tierImageUrl")}</label>
                  <div className="flex items-center gap-2">
                    <input
                      id={`${tier.rowId}-image`}
                      value={tier.image_url}
                      onChange={(e) => updateTier(tier.rowId, "image_url", e.target.value)}
                      placeholder={t("tierImageUrlPlaceholder")}
                      className="min-h-11 min-w-0 flex-1 rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
                    />
                    <label className="relative flex min-h-11 shrink-0 cursor-pointer items-center gap-1.5 rounded-lg border border-dashed border-border px-3 py-2 text-xs text-text-muted hover:border-accent-primary hover:text-accent-primary focus-within:ring-2 focus-within:ring-accent-primary">
                      <Upload className="h-4 w-4" aria-hidden="true" />
                      {uploadingRowId === tier.rowId ? tc("processing") : t("tierImageUpload")}
                      <input
                        type="file"
                        accept="image/*"
                        className="sr-only"
                        onChange={(e) => {
                          const file = e.currentTarget.files?.[0];
                          e.currentTarget.value = "";
                          if (file) void handleUpload(tier.rowId, file);
                        }}
                      />
                    </label>
                  </div>
                </div>

                <div className="w-32 min-w-0 shrink-0">
                  <label htmlFor={`${tier.rowId}-xp`} className="mb-1 block text-xs text-text-muted">{t("requiredXp")}</label>
                  <input
                    id={`${tier.rowId}-xp`}
                    type="number"
                    value={tier.required_xp}
                    onChange={(e) => updateTier(tier.rowId, "required_xp", parseInt(e.target.value) || 0)}
                    className="min-h-11 w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm focus:border-accent-primary focus:outline-none"
                    min={0}
                  />
                </div>

                <button
                  type="button"
                  onClick={() => removeTier(tier.rowId)}
                  className="mt-5 flex h-11 w-11 shrink-0 items-center justify-center rounded-lg text-accent-error hover:bg-accent-error/10"
                  title={t("removeTier")}
                  aria-label={t("removeTier")}
                >
                  <Trash2 className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
            ))}
        </div>
      )}
      </fieldset>

      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={busy || !isSuperAdmin}>
          {saving ? tc("processing") : tc("save")}
        </Button>
      </div>
    </div>
  );
}
