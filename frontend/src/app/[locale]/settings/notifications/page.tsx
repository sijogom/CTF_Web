"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiFetch, ApiError } from "@/lib/api-client";
import { Button } from "@/components/ui/Button";
import { useAuth } from "@/providers/AuthProvider";
import { useToast } from "@/providers/ToastProvider";

interface ToggleProps {
  label: string;
  description: string;
  checked: boolean;
  disabled: boolean;
  onChange: (v: boolean) => void;
}

function Toggle({ label, description, checked, disabled, onChange }: ToggleProps) {
  return (
    <div className="flex items-center justify-between gap-4 border-b border-border py-4">
      <div className="min-w-0 [overflow-wrap:anywhere]">
        <p className="text-sm font-medium">{label}</p>
        <p className="text-xs text-text-muted">{description}</p>
      </div>
      <button
        type="button"
        role="switch"
        disabled={disabled}
        aria-checked={checked}
        aria-label={label}
        onClick={() => onChange(!checked)}
        className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50 ${
          checked ? "bg-accent-primary" : "bg-text-muted/30"
        }`}
      >
        <span
          className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
            checked ? "translate-x-5" : ""
          }`}
        />
      </button>
    </div>
  );
}

interface Preferences {
  comment: boolean;
  competition: boolean;
  badge: boolean;
  email: boolean;
}

export default function NotificationSettingsPage() {
  const t = useTranslations("settings");
  const tc = useTranslations("common");
  const { user } = useAuth();
  const { toast } = useToast();
  const [preferences, setPreferences] = useState<Preferences | null>(null);
  const [saved, setSaved] = useState<Preferences | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const saveInFlight = useRef(false);
  const userId = user?.id;

  const load = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const value = await apiFetch<Preferences>("/api/auth/me/notification-preferences");
      if (request !== generation.current) return;
      setPreferences(value);
      setSaved(value);
    } catch (failure) {
      if (request === generation.current) setError(failure instanceof ApiError ? failure.message : tc("loadFailed"));
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, [tc]);

  useEffect(() => {
    setPreferences(null);
    setSaved(null);
    void load();
    return () => { generation.current += 1; };
  }, [load, userId]);

  const save = async () => {
    if (!preferences || saveInFlight.current) return;
    saveInFlight.current = true;
    setSaving(true);
    setError(null);
    const request = generation.current;
    try {
      const changed = Object.fromEntries((Object.keys(preferences) as (keyof Preferences)[])
        .filter((key) => preferences[key] !== saved?.[key]).map((key) => [key, preferences[key]]));
      const value = await apiFetch<Preferences>("/api/auth/me/notification-preferences", {method: "PATCH", body: changed});
      if (request !== generation.current) return;
      setPreferences(value);
      setSaved(value);
      toast("success", t("saveSuccess"));
    } catch (failure) {
      if (request === generation.current) setError(failure instanceof ApiError ? failure.message : t("saveFailed"));
    } finally {
      saveInFlight.current = false;
      setSaving(false);
    }
  };
  const dirty = preferences !== null && JSON.stringify(preferences) !== JSON.stringify(saved);

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">{t("notificationTitle")}</h2>

      {loading && <p role="status" className="text-sm text-text-muted">{tc("loading")}</p>}
      {error && <div role="alert" className="space-y-2 text-sm text-accent-error"><p>{error}</p>
        {!preferences && <Button variant="secondary" size="sm" onClick={() => void load()}>{tc("retry")}</Button>}
      </div>}
      {preferences && <><div>
        <Toggle
          label={t("commentNotif")}
          description={t("commentNotifDesc")}
          checked={preferences.comment}
          disabled={saving || loading}
          onChange={(value) => setPreferences({...preferences, comment: value})}
        />
        <Toggle
          label={t("competitionNotif")}
          description={t("competitionNotifDesc")}
          checked={preferences.competition}
          disabled={saving || loading}
          onChange={(value) => setPreferences({...preferences, competition: value})}
        />
        <Toggle
          label={t("badgeNotif")}
          description={t("badgeNotifDesc")}
          checked={preferences.badge}
          disabled={saving || loading}
          onChange={(value) => setPreferences({...preferences, badge: value})}
        />
        <Toggle
          label={t("emailNotif")}
          description={t("emailNotifDesc")}
          checked={preferences.email}
          disabled={saving || loading}
          onChange={(value) => setPreferences({...preferences, email: value})}
        />
      </div>
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => void save()} disabled={!dirty || saving || loading}>{saving ? t("saving") : tc("save")}</Button>
        <Button variant="secondary" onClick={() => {setPreferences(saved); setError(null);}} disabled={!dirty || saving}>{tc("cancel")}</Button>
      </div></>}
    </div>
  );
}
