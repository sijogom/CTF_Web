"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ArrowRight, RefreshCw } from "lucide-react";
import { Link } from "@/i18n/navigation";
import { useAuth } from "@/providers/AuthProvider";
import { apiFetch } from "@/lib/api-client";
import { internalContentHref } from "@/lib/map-ui-state.mjs";

interface Consent { enabled: boolean; policy_version: string }
interface Recommendation {
  type: "challenge" | "course" | "lesson";
  id: string;
  name: string;
  href: string;
  is_locked?: boolean;
  prerequisites?: Array<{ type: string; id: string; name: string; href: string; completed: boolean }>;
  next_action?: { type: string; id: string; name: string; href: string } | null;
  evidence: Array<{ source_name: string; relation_type: "helps" | "recommends" | "prepares_for"; strength: number; confidence: number; evidence_summary: string | null }>;
}
interface Recommendations {
  enabled: boolean;
  consent_required: boolean;
  empty_reason?: "no_history" | "no_candidates" | null;
  items: Recommendation[];
}
interface State { owner: string; consent: Consent; recommendations: Recommendations | null }

function RecommendationItem({ item }: { item: Recommendation }) {
  const t = useTranslations("learningRecommendations");
  const tm = useTranslations("knowledgeMap");
  const href = internalContentHref(item.href);
  const nextHref = internalContentHref(item.next_action?.href);
  return (
    <li className="space-y-2 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs text-text-secondary">
            {tm(`nodeTypes.${item.type}`)}{item.is_locked ? ` · ${t("locked")}` : ""}
          </p>
          <p className="break-words text-sm font-semibold">{item.name}</p>
        </div>
        <div className="flex flex-wrap gap-3 text-sm text-accent-primary">
          {href && <Link href={href} className="inline-flex items-center gap-1">
            {t("open")}<ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
          </Link>}
          <Link href={`/learn?view=map&focus_type=${item.type}&focus_id=${encodeURIComponent(item.id)}`}>
            {t("map")}
          </Link>
        </div>
      </div>
      {item.is_locked && <ul className="space-y-1 text-xs text-text-secondary">
        {item.prerequisites?.map((step) => <li key={`${step.type}:${step.id}`} className="break-words">
          {step.completed ? "✓" : "○"} {step.name}
        </li>)}
      </ul>}
      {item.is_locked && item.next_action && nextHref && nextHref !== href && <Link href={nextHref} className="inline-flex max-w-full flex-wrap items-center gap-1.5 text-sm font-medium text-accent-primary">
        {t("nextAction")}: <span className="break-words">{item.next_action.name}</span>
        <ArrowRight className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      </Link>}
      <ul className="space-y-2">
        {item.evidence.map((evidence, index) => <li key={index} className="break-words text-xs leading-5 text-text-secondary">
          <p>{evidence.source_name} · {tm(`relationTypes.${evidence.relation_type}`)}</p>
          {evidence.evidence_summary && <p>{evidence.evidence_summary}</p>}
          <p>{tm("relationStrength")} {Math.round(evidence.strength * 100)}% · {tm("evidenceConfidence")} {Math.round(evidence.confidence * 100)}%</p>
        </li>)}
      </ul>
    </li>
  );
}

export function LearningRecommendations() {
  const t = useTranslations("learningRecommendations");
  const { user, loading: authLoading, sessionStatus } = useAuth();
  const [state, setState] = useState<State | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(false);
  const [consentUncertain, setConsentUncertain] = useState(false);
  const [consentDraft, setConsentDraft] = useState(false);
  const generation = useRef(0);
  const readController = useRef<AbortController | null>(null);
  const mutationController = useRef<AbortController | null>(null);
  const consentWriteBusy = useRef(false);
  const owner = user?.id;
  const visible = state?.owner === owner ? state : null;

  const load = useCallback(async () => {
    readController.current?.abort();
    if (!owner || sessionStatus !== "authenticated") return;
    const controller = new AbortController();
    readController.current = controller;
    const request = ++generation.current;
    setLoading(true);
    setError(false);
    try {
      const consent = await apiFetch<Consent>("/api/learn/me/recommendation-consent", { signal: controller.signal });
      if (controller.signal.aborted || request !== generation.current) return;
      setConsentUncertain(false);
      setConsentDraft(consent.enabled);
      setState({ owner, consent, recommendations: null });
      if (consent.enabled) {
        const recommendations = await apiFetch<Recommendations>("/api/learn/me/recommendations", { signal: controller.signal });
        if (controller.signal.aborted || request !== generation.current) return;
        setState({ owner, consent: { ...consent, enabled: recommendations.enabled }, recommendations });
        setConsentDraft(recommendations.enabled);
      }
    } catch {
      if (!controller.signal.aborted && request === generation.current) setError(true);
    } finally {
      if (!controller.signal.aborted && request === generation.current) setLoading(false);
    }
  }, [owner, sessionStatus]);

  useEffect(() => {
    consentWriteBusy.current = false;
    setSaving(false);
    setState(null);
    void load();
    return () => { generation.current += 1; readController.current?.abort(); mutationController.current?.abort(); };
  }, [load]);

  const saveConsent = async () => {
    if (!owner || !visible || consentWriteBusy.current || consentUncertain) return;
    consentWriteBusy.current = true;
    const request = ++generation.current;
    readController.current?.abort();
    const controller = new AbortController();
    mutationController.current = controller;
    setSaving(true);
    setError(false);
    try {
      const consent = await apiFetch<Consent>("/api/learn/me/recommendation-consent", { method: "PUT", body: { enabled: consentDraft }, signal: controller.signal });
      if (controller.signal.aborted || request !== generation.current) return;
      setState({ owner, consent, recommendations: null });
      setConsentDraft(consent.enabled);
      await load();
    } catch {
      if (!controller.signal.aborted && request === generation.current) {
        setError(true);
        setConsentUncertain(true);
      }
    } finally {
      if (!controller.signal.aborted) {
        consentWriteBusy.current = false;
        setSaving(false);
      }
    }
  };

  if (authLoading || sessionStatus !== "authenticated" || !owner) return null;

  return (
    <section className="space-y-4 border-t border-border pt-6" aria-labelledby="learning-recommendations-title">
      <h2 id="learning-recommendations-title" className="text-lg font-semibold">{t("title")}</h2>
      <>
          {visible && <div className="flex flex-wrap items-center gap-3">
            <label className="inline-flex min-w-0 items-start gap-2 text-sm">
              <input type="checkbox" checked={consentDraft} disabled={saving || loading} onChange={(event) => setConsentDraft(event.target.checked)} className="mt-1" />
              <span className="min-w-0 break-words">{t("consent")}</span>
            </label>
            <button type="button" disabled={saving || loading || consentUncertain || consentDraft === visible.consent.enabled} onClick={() => void saveConsent()} className="rounded-md border border-border px-3 py-1.5 text-sm font-medium text-accent-primary disabled:opacity-50">
              {saving ? t("saving") : t("save")}
            </button>
          </div>}
          {error && <div role="alert" className="flex flex-wrap items-center gap-2 text-sm text-accent-error">
            <span>{t("failed")}</span>
            <button type="button" disabled={saving || loading} onClick={() => void load()} className="inline-flex items-center gap-1 text-accent-primary">
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />{t("retry")}
            </button>
          </div>}
          {loading && <p role="status" className="text-sm text-text-muted">{t("loading")}</p>}
          {!error && !loading && visible && (!visible.consent.enabled
            ? <p className="text-sm text-text-secondary">{t("disabled")}</p>
            : visible.recommendations?.items.length === 0
              ? <p role="status" className="text-sm text-text-secondary">{t(visible.recommendations.empty_reason === "no_history" ? "noHistory" : "noCandidates")}</p>
              : <ul className="divide-y divide-border">
                {visible.recommendations?.items.map((item) => <RecommendationItem key={`${item.type}:${item.id}`} item={item} />)}
              </ul>)}
        </>
    </section>
  );
}
