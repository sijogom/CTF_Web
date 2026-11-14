"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { apiFetch, ApiError, captureApiRequestOwner, isCurrentApiRequestOwner, type ApiRequestOwner } from "@/lib/api-client";
import { useAuth } from "@/providers/AuthProvider";
import type {
  AdminSettings,
  AdminSettingsUpdate,
  SettingsSaveResponse,
} from "@/types/admin";
import {
  DEFAULT_SITE_NAME,
  SITE_SETTINGS_UPDATED_EVENT,
} from "@/providers/SiteSettingsProvider";

interface SettingsOperation {
  generation: number;
  owner: ApiRequestOwner;
  controller: AbortController;
}

export default function AdminSettingsPage() {
  const t = useTranslations("admin");
  const { user, isSuperAdmin } = useAuth();
  const [siteName, setSiteName] = useState(DEFAULT_SITE_NAME);
  const [registrationOpen, setRegistrationOpen] = useState(true);
  const [maxContainers, setMaxContainers] = useState(50);
  const [containerTimeout, setContainerTimeout] = useState(30);
  const [cacheRefreshPending, setCacheRefreshPending] = useState(false);

  const [loading, setLoading] = useState(true);
  const [loadedForUser, setLoadedForUser] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const mountedRef = useRef(false);
  const requestGenerationRef = useRef(0);
  const loadRequestIdRef = useRef(0);
  const loadingRef = useRef(true);
  const loadedForUserRef = useRef<string | null>(null);
  const loadControllerRef = useRef<AbortController | null>(null);
  const operationRef = useRef<SettingsOperation | null>(null);
  const controlsDisabled = loading || Boolean(loadError) || saving || !isSuperAdmin || !user?.id || loadedForUser !== user.id;

  const isCurrentOperation = useCallback((operation: SettingsOperation) => {
    const current = mountedRef.current && operationRef.current === operation && operation.generation === requestGenerationRef.current
      && !operation.controller.signal.aborted && isCurrentApiRequestOwner(operation.owner);
    if (!current) operation.controller.abort();
    return current;
  }, []);

  const loadSettings = useCallback(async (operation?: SettingsOperation) => {
    if (!mountedRef.current || !user?.id || operationRef.current && operationRef.current !== operation) return false;
    const generation = requestGenerationRef.current, requestId = ++loadRequestIdRef.current;
    loadControllerRef.current?.abort();
    const controller = new AbortController();
    loadControllerRef.current = controller;
    loadingRef.current = true;
    loadedForUserRef.current = null;
    setLoadedForUser(null);
    setLoading(true);
    setLoadError(null);
    let owner: ApiRequestOwner | null = null;
    const current = () => mountedRef.current && generation === requestGenerationRef.current && requestId === loadRequestIdRef.current
      && !controller.signal.aborted && (!owner || isCurrentApiRequestOwner(owner)) && (!operation || isCurrentOperation(operation));
    try {
      owner = operation?.owner ?? captureApiRequestOwner(user.id);
      if (!current()) { controller.abort(); return false; }
      const data = await apiFetch<AdminSettings>("/api/auth/admin/settings", { requestOwner: owner, signal: controller.signal });
      if (!current()) { controller.abort(); return false; }
      setSiteName(data.site_name ?? DEFAULT_SITE_NAME);
      setRegistrationOpen(data.registration_open ?? true);
      setMaxContainers(data.max_containers ?? 50);
      setContainerTimeout(data.container_timeout ?? 30);
      setCacheRefreshPending(Boolean(data.cache_refresh_pending));
      loadedForUserRef.current = user.id;
      setLoadedForUser(user.id);
      return true;
    } catch (err) {
      if (!current()) { controller.abort(); return false; }
      setLoadError(err instanceof ApiError ? err.message : t("settingsLoadFailed"));
      return false;
    } finally {
      if (mountedRef.current && generation === requestGenerationRef.current && requestId === loadRequestIdRef.current) {
        loadingRef.current = false;
        setLoading(false);
      }
    }
  }, [isCurrentOperation, t, user?.id]);

  useEffect(() => {
    mountedRef.current = true;
    setSaving(false);
    setMessage(null);
    void loadSettings();
    return () => {
      mountedRef.current = false;
      requestGenerationRef.current += 1;
      loadedForUserRef.current = null;
      loadControllerRef.current?.abort();
      operationRef.current?.controller.abort();
      operationRef.current = null;
    };
  }, [loadSettings]);

  const canEdit = () => {
    if (!mountedRef.current || loading || loadingRef.current || loadError || saving || operationRef.current || !isSuperAdmin
      || !user?.id || loadedForUserRef.current !== user.id) return false;
    try { return isCurrentApiRequestOwner(captureApiRequestOwner(user.id)); } catch { return false; }
  };

  const handleSave = async () => {
    if (!canEdit() || !user?.id) return;
    let operation: SettingsOperation;
    try { operation = { generation: requestGenerationRef.current, owner: captureApiRequestOwner(user.id), controller: new AbortController() }; }
    catch { return; }
    operationRef.current = operation;

    setSaving(true);
    setMessage(null);
    try {
      const body: AdminSettingsUpdate = {
        site_name: siteName,
        registration_open: registrationOpen,
        max_containers: maxContainers,
        container_timeout: containerTimeout,
      };
      const response = await apiFetch<SettingsSaveResponse>("/api/auth/admin/settings", {
        method: "PATCH",
        body,
        requestOwner: operation.owner,
        signal: operation.controller.signal,
      });
      if (!isCurrentOperation(operation)) return;
      const cachePending = Boolean(response.cache_refresh_pending);
      setCacheRefreshPending(cachePending);
      if (!await loadSettings(operation) || !isCurrentOperation(operation)) return;
      window.dispatchEvent(new Event(SITE_SETTINGS_UPDATED_EVENT));
      setMessage({
        type: "success",
        text: cachePending ? t("settingsSavedCachePending") : t("settingsSaved"),
      });
    } catch (err) {
      if (!isCurrentOperation(operation)) return;
      const text = err instanceof ApiError ? err.message : t("settingsSaveFailed");
      setMessage({ type: "error", text });
    } finally {
      if (operationRef.current === operation) {
        operationRef.current = null;
        if (mountedRef.current && operation.generation === requestGenerationRef.current) setSaving(false);
      }
    }
  };

  const inputClass =
    "w-full rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none";
  const shortInputClass =
    "w-32 rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm focus:border-accent-primary focus:outline-none";

  if (loading) {
    return (
      <div className="mx-auto max-w-2xl space-y-6">
        <h1 className="text-2xl font-bold">{t("siteSettings")}</h1>
        <div className="animate-pulse space-y-4">
          <div className="h-40 rounded-xl bg-bg-surface" />
          <div className="h-40 rounded-xl bg-bg-surface" />
        </div>
      </div>
    );
  }

  if (loadError || loadedForUser !== user?.id) {
    return (
      <div className="mx-auto max-w-2xl space-y-6">
        <h1 className="text-2xl font-bold">{t("siteSettings")}</h1>
        <div className="rounded-xl border border-accent-error/30 bg-accent-error/10 p-5 text-sm text-accent-error">
          <p>{loadError || t("settingsLoadFailed")}</p>
          <div className="mt-4">
            <Button variant="secondary" onClick={() => void loadSettings()}>
              {t("retryAction")}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <h1 className="text-2xl font-bold">{t("siteSettings")}</h1>

      {message && (
        <div
          className={`rounded-lg border p-3 text-sm ${
            message.type === "success"
              ? "border-accent-success/30 bg-accent-success/10 text-accent-success"
              : "border-accent-error/30 bg-accent-error/10 text-accent-error"
          }`}
        >
          {message.text}
        </div>
      )}

      {cacheRefreshPending && (
        <p className="rounded-lg border border-accent-warning/30 bg-accent-warning/10 p-3 text-sm text-accent-warning">
          {t("settingsCacheRefreshPending")}
        </p>
      )}

      <div className="rounded-xl border border-border bg-bg-surface p-5">
        <h2 className="mb-4 text-lg font-semibold">{t("settingsGeneral")}</h2>
        <div className="space-y-4">
          <div>
            <label htmlFor="site-name" className="mb-1.5 block text-sm font-medium">
              {t("settingsSiteName")}
            </label>
            <input
              id="site-name"
              value={siteName}
              onChange={(event) => { if (canEdit()) setSiteName(event.target.value); }}
              disabled={controlsDisabled}
              className={inputClass}
            />
          </div>
          <div>
            <label className="flex cursor-pointer items-center justify-between">
              <span className="text-sm">{t("settingsRegistration")}</span>
              <button
                type="button"
                role="switch"
                aria-checked={registrationOpen}
                onClick={() => { if (canEdit()) setRegistrationOpen((current) => !current); }}
                disabled={controlsDisabled}
                className={`relative h-6 w-11 rounded-full transition-colors ${
                  registrationOpen ? "bg-accent-primary" : "bg-bg-primary"
                }`}
              >
                <span
                  className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                    registrationOpen ? "translate-x-5" : ""
                  }`}
                />
              </button>
            </label>
            <p className="mt-1.5 text-xs text-text-muted">{t("settingsRegistrationHint")}</p>
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-border bg-bg-surface p-5">
        <h2 className="mb-4 text-lg font-semibold">{t("settingsContainer")}</h2>
        <div className="space-y-4">
          <div>
            <label htmlFor="max-containers" className="mb-1.5 block text-sm font-medium">
              {t("settingsMaxContainers")}
            </label>
            <input
              id="max-containers"
              type="number"
              min={1}
              max={100000}
              value={maxContainers}
              onChange={(event) => { if (canEdit()) setMaxContainers(Number.parseInt(event.target.value, 10) || 0); }}
              disabled={controlsDisabled}
              className={shortInputClass}
            />
            <p className="mt-1.5 text-xs text-text-muted">{t("settingsMaxContainersHint")}</p>
          </div>
          <div>
            <label htmlFor="container-timeout" className="mb-1.5 block text-sm font-medium">
              {t("settingsContainerTimeout")}
            </label>
            <input
              id="container-timeout"
              type="number"
              min={1}
              max={1440}
              value={containerTimeout}
              onChange={(event) => { if (canEdit()) setContainerTimeout(Number.parseInt(event.target.value, 10) || 0); }}
              disabled={controlsDisabled}
              className={shortInputClass}
            />
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-border bg-bg-surface p-5">
        <h2 className="mb-2 text-lg font-semibold">{t("settingsEmail")}</h2>
        <p className="text-sm text-text-muted">{t("settingsEmailManagedElsewhere")}</p>
        {isSuperAdmin && user?.id && <Link
          href="/admin/email"
          className="mt-4 inline-flex text-sm font-medium text-accent-primary hover:underline"
        >
          {t("emailManagement")}
        </Link>}
      </div>

      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={controlsDisabled}>
          {saving ? t("settingsSaving") : t("settingsSave")}
        </Button>
      </div>
    </div>
  );
}
