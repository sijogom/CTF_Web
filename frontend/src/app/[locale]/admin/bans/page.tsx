"use client";

import { useCallback, useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { ApiError, apiFetch } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";

type BanState = "" | "active" | "lifted" | "expired";

interface AdminUser {
  id: string;
  username: string;
  email: string;
  role: string;
  is_active: boolean;
}

interface BanAppeal {
  id: string;
  ban_id: string;
  user_id: string;
  message: string;
  status: "pending" | "accepted" | "rejected";
  reviewer_id: string | null;
  reviewer_note: string | null;
  reviewed_at: string | null;
  created_at: string;
}

interface BanItem {
  id: string;
  user_id: string;
  username: string | null;
  email: string | null;
  public_reason: string;
  policy_version: string;
  source: "manual" | "automated";
  started_at: string;
  expires_at: string | null;
  created_at: string;
  lifted_at: string | null;
  lift_reason: string | null;
  appeal_allowed: boolean;
  status: Exclude<BanState, "">;
}

interface BanDetail extends BanItem {
  evidence_summary: string;
  appeals: BanAppeal[];
}

interface BanListResponse {
  total: number;
  items: BanItem[];
}

interface UserListResponse {
  items: AdminUser[];
}

const DEFAULT_POLICY_VERSION = "manual-review-v1";

export default function AdminBansPage() {
  const t = useTranslations("admin");
  const locale = useLocale();
  const { toast } = useToast();
  const [bans, setBans] = useState<BanItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [stateFilter, setStateFilter] = useState<BanState>("");
  const [targetSearch, setTargetSearch] = useState("");
  const [targetCandidates, setTargetCandidates] = useState<AdminUser[]>([]);
  const [target, setTarget] = useState<AdminUser | null>(null);
  const [publicReason, setPublicReason] = useState("");
  const [evidenceSummary, setEvidenceSummary] = useState("");
  const [policyVersion, setPolicyVersion] = useState(DEFAULT_POLICY_VERSION);
  const [durationHours, setDurationHours] = useState("12");
  const [permanent, setPermanent] = useState(false);
  const [appealAllowed, setAppealAllowed] = useState(true);
  const [creating, setCreating] = useState(false);
  const [details, setDetails] = useState<BanDetail | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [liftReason, setLiftReason] = useState("");
  const [reviewNotes, setReviewNotes] = useState<Record<string, string>>({});
  const [mutating, setMutating] = useState(false);

  const formatAt = useCallback(
    (value: string | null) =>
      value
        ? new Intl.DateTimeFormat(locale, {
            dateStyle: "medium",
            timeStyle: "short",
          }).format(new Date(value))
        : "-",
    [locale],
  );

  const getStatusLabel = useCallback(
    (state: BanItem["status"] | BanAppeal["status"]) => {
      if (state === "active") return t("banActive");
      if (state === "lifted") return t("banLifted");
      if (state === "expired") return t("banExpired");
      if (state === "accepted") return t("banAppealAccepted");
      if (state === "rejected") return t("banAppealRejected");
      return t("banAppealPending");
    },
    [t],
  );

  const statusClass = (state: BanItem["status"] | BanAppeal["status"]) => {
    if (state === "active" || state === "pending") return "bg-yellow-500/10 text-yellow-500";
    if (state === "lifted" || state === "accepted") return "bg-accent-success/10 text-accent-success";
    return "bg-text-muted/10 text-text-muted";
  };

  const loadBans = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({ size: "50" });
      if (search.trim()) params.set("search", search.trim());
      if (stateFilter) params.set("state", stateFilter);
      const data = await apiFetch<BanListResponse>(`/api/auth/admin/bans?${params}`);
      setBans(data.items);
      setTotal(data.total);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : t("banLoadFailed");
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [search, stateFilter, t]);

  const loadDetails = useCallback(async (banId: string) => {
    setDetailsLoading(true);
    try {
      const data = await apiFetch<BanDetail>(`/api/auth/admin/bans/${banId}`);
      setDetails(data);
      setLiftReason("");
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : t("banLoadFailed"));
    } finally {
      setDetailsLoading(false);
    }
  }, [t, toast]);

  useEffect(() => {
    void loadBans();
  }, [loadBans]);

  useEffect(() => {
    const keyword = targetSearch.trim();
    if (keyword.length < 2) {
      setTargetCandidates([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void apiFetch<UserListResponse>(`/api/auth/admin/users?search=${encodeURIComponent(keyword)}&size=10`)
        .then((data) => {
          if (!cancelled) setTargetCandidates(data.items.filter((user) => user.is_active));
        })
        .catch(() => {
          if (!cancelled) setTargetCandidates([]);
        });
    }, 300);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [targetSearch]);

  const handleCreate = async () => {
    if (!target) {
      toast("error", t("banPleaseSelectTarget"));
      return;
    }
    const parsedDuration = Number(durationHours);
    if (!permanent && (!Number.isInteger(parsedDuration) || parsedDuration < 1)) {
      toast("error", t("banDurationHours"));
      return;
    }
    setCreating(true);
    try {
      await apiFetch("/api/auth/admin/bans", {
        method: "POST",
        body: {
          target_user_id: target.id,
          public_reason: publicReason,
          evidence_summary: evidenceSummary,
          policy_version: policyVersion,
          duration_hours: permanent ? null : parsedDuration,
          permanent,
          appeal_allowed: appealAllowed,
        },
      });
      toast("success", t("banCreateSuccess"));
      setPublicReason("");
      setEvidenceSummary("");
      setDurationHours("12");
      setPermanent(false);
      setTarget(null);
      setTargetSearch("");
      setTargetCandidates([]);
      await loadBans();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : t("banCreateFailed"));
    } finally {
      setCreating(false);
    }
  };

  const handleLift = async () => {
    if (!details || liftReason.trim().length < 3) {
      toast("error", t("banLiftReason"));
      return;
    }
    setMutating(true);
    try {
      await apiFetch(`/api/auth/admin/bans/${details.id}/lift`, {
        method: "POST",
        body: { reason: liftReason },
      });
      toast("success", t("banLiftedSuccess"));
      await Promise.all([loadBans(), loadDetails(details.id)]);
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : t("banCreateFailed"));
    } finally {
      setMutating(false);
    }
  };

  const handleReview = async (appeal: BanAppeal, decision: "accepted" | "rejected", lift = false) => {
    if (!details) return;
    const reviewerNote = reviewNotes[appeal.id]?.trim() || "";
    if (reviewerNote.length < 3) {
      toast("error", t("banReviewNote"));
      return;
    }
    setMutating(true);
    try {
      await apiFetch(`/api/auth/admin/bans/${details.id}/appeals/${appeal.id}/review`, {
        method: "POST",
        body: {
          decision,
          reviewer_note: reviewerNote,
          lift_ban: lift,
          lift_reason: lift ? reviewerNote : null,
        },
      });
      toast("success", t("banReviewSuccess"));
      await Promise.all([loadBans(), loadDetails(details.id)]);
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : t("banReviewFailed"));
    } finally {
      setMutating(false);
    }
  };

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">{t("banTitle")}</h1>
        <p className="mt-1 text-sm text-text-secondary">{t("banDescription")}</p>
      </header>

      <section className="rounded-xl border border-border bg-bg-surface p-5">
        <h2 className="text-lg font-semibold">{t("banCreate")}</h2>
        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          <label className="space-y-1 text-sm">
            <span>{t("banTarget")}</span>
            <input value={targetSearch} onChange={(event) => setTargetSearch(event.target.value)} placeholder={t("banTargetSearch")} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2" />
            {target && <span className="block rounded bg-accent-primary/10 px-2 py-1 text-accent-primary">{target.username} ({target.email})</span>}
            {!target && <span className="block text-text-muted">{t("banSelectTarget")}</span>}
            {targetCandidates.length > 0 && (
              <div className="max-h-36 overflow-y-auto rounded-lg border border-border bg-bg-primary">
                {targetCandidates.map((candidate) => (
                  <button key={candidate.id} type="button" onClick={() => { setTarget(candidate); setTargetSearch(candidate.username); setTargetCandidates([]); }} className="flex w-full flex-col border-b border-border px-3 py-2 text-left text-sm last:border-0 hover:bg-bg-elevated">
                    <span>{candidate.username}</span><span className="text-xs text-text-muted">{candidate.email}</span>
                  </button>
                ))}
              </div>
            )}
          </label>
          <label className="space-y-1 text-sm"><span>{t("banPolicyVersion")}</span><input value={policyVersion} onChange={(event) => setPolicyVersion(event.target.value)} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2" /></label>
          <label className="space-y-1 text-sm"><span>{t("banPublicReason")}</span><textarea value={publicReason} onChange={(event) => setPublicReason(event.target.value)} rows={3} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2" /></label>
          <label className="space-y-1 text-sm"><span>{t("banEvidence")}</span><textarea value={evidenceSummary} onChange={(event) => setEvidenceSummary(event.target.value)} rows={3} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2" /></label>
          <label className="space-y-1 text-sm"><span>{t("banDurationHours")}</span><input type="number" min={1} max={8760} disabled={permanent} value={durationHours} onChange={(event) => setDurationHours(event.target.value)} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 disabled:opacity-50" /></label>
          <div className="space-y-3 text-sm">
            <label className="flex items-center gap-2"><input type="checkbox" checked={permanent} onChange={(event) => setPermanent(event.target.checked)} />{t("banPermanent")}</label>
            {permanent && <p className="text-xs text-yellow-500">{t("banPermanentWarning")}</p>}
            <label className="flex items-center gap-2"><input type="checkbox" checked={appealAllowed} onChange={(event) => setAppealAllowed(event.target.checked)} />{t("banAppealAllowed")}</label>
          </div>
        </div>
        <button type="button" disabled={creating} onClick={() => void handleCreate()} className="mt-5 rounded-lg bg-accent-error px-4 py-2 font-medium text-white disabled:opacity-50">{t("banCreateAction")}</button>
      </section>

      <section className="rounded-xl border border-border bg-bg-surface p-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div className="flex flex-1 flex-col gap-3 sm:flex-row">
            <label className="flex-1 space-y-1 text-sm"><span>{t("banSearch")}</span><input value={search} onChange={(event) => setSearch(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void loadBans(); }} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2" /></label>
            <label className="space-y-1 text-sm"><span>{t("banStatus")}</span><select value={stateFilter} onChange={(event) => setStateFilter(event.target.value as BanState)} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2"><option value="">{t("banAll")}</option><option value="active">{t("banActive")}</option><option value="lifted">{t("banLifted")}</option><option value="expired">{t("banExpired")}</option></select></label>
          </div>
          <button type="button" onClick={() => void loadBans()} className="rounded-lg border border-border px-4 py-2 text-sm hover:bg-bg-elevated">{t("banRefresh")}</button>
        </div>
        {error && <p className="mt-4 rounded-lg border border-accent-error/30 bg-accent-error/10 px-3 py-2 text-sm text-accent-error">{error}</p>}
        <div className="mt-4 overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-bg-primary text-left"><tr><th className="px-3 py-2">{t("colUsername")}</th><th className="px-3 py-2">{t("banPublicReason")}</th><th className="px-3 py-2">{t("banStatus")}</th><th className="px-3 py-2">{t("createdAt")}</th><th className="px-3 py-2">{t("actions")}</th></tr></thead>
            <tbody>
              {loading ? <tr><td colSpan={5} className="px-3 py-8 text-center text-text-muted">{t("loadingData")}</td></tr> : bans.length === 0 ? <tr><td colSpan={5} className="px-3 py-8 text-center text-text-muted">{t("banNoRecords")}</td></tr> : bans.map((ban) => (
                <tr key={ban.id} className="border-b border-border last:border-0"><td className="px-3 py-3"><div className="font-medium">{ban.username || ban.user_id}</div><div className="text-xs text-text-muted">{ban.email || ban.user_id}</div></td><td className="max-w-xs px-3 py-3 text-text-secondary">{ban.public_reason}</td><td className="px-3 py-3"><span className={`rounded-full px-2 py-1 text-xs ${statusClass(ban.status)}`}>{getStatusLabel(ban.status)}</span></td><td className="whitespace-nowrap px-3 py-3 text-text-muted">{formatAt(ban.started_at)}</td><td className="px-3 py-3"><button type="button" onClick={() => void loadDetails(ban.id)} className="text-accent-primary hover:underline">{t("banDetails")}</button></td></tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs text-text-muted">{total}</p>
      </section>

      {(details || detailsLoading) && (
        <section className="rounded-xl border border-border bg-bg-surface p-5">
          <h2 className="text-lg font-semibold">{t("banDetails")}</h2>
          {detailsLoading || !details ? <p className="mt-3 text-sm text-text-muted">{t("loadingData")}</p> : (
            <div className="mt-4 space-y-5">
              <dl className="grid gap-3 text-sm md:grid-cols-2">
                <div><dt className="text-text-muted">{t("banTarget")}</dt><dd>{details.username || details.user_id}</dd></div>
                <div><dt className="text-text-muted">{t("banStatus")}</dt><dd>{getStatusLabel(details.status)}</dd></div>
                <div><dt className="text-text-muted">{t("banPublicReason")}</dt><dd className="whitespace-pre-wrap">{details.public_reason}</dd></div>
                <div><dt className="text-text-muted">{t("banEvidence")}</dt><dd className="whitespace-pre-wrap">{details.evidence_summary}</dd></div>
                <div><dt className="text-text-muted">{t("banPolicyVersion")}</dt><dd>{details.policy_version}</dd></div>
                <div><dt className="text-text-muted">{t("banDurationHours")}</dt><dd>{formatAt(details.started_at)} — {formatAt(details.expires_at)}</dd></div>
              </dl>
              {details.status === "active" && <div className="rounded-lg border border-accent-error/30 p-4"><label className="block text-sm"><span>{t("banLiftReason")}</span><textarea value={liftReason} onChange={(event) => setLiftReason(event.target.value)} rows={2} className="mt-1 w-full rounded-lg border border-border bg-bg-primary px-3 py-2" /></label><button type="button" disabled={mutating} onClick={() => void handleLift()} className="mt-3 rounded-lg border border-accent-error px-4 py-2 text-sm text-accent-error disabled:opacity-50">{t("banLiftAction")}</button></div>}
              <div>
                <h3 className="font-semibold">{t("banAppeals")}</h3>
                {details.appeals.length === 0 ? <p className="mt-2 text-sm text-text-muted">{t("banNoAppeals")}</p> : <div className="mt-3 space-y-3">{details.appeals.map((appeal) => (
                  <article key={appeal.id} className="rounded-lg border border-border p-4 text-sm">
                    <div className="flex flex-wrap items-center justify-between gap-2"><span className={`rounded-full px-2 py-1 text-xs ${statusClass(appeal.status)}`}>{getStatusLabel(appeal.status)}</span><time className="text-xs text-text-muted">{formatAt(appeal.created_at)}</time></div>
                    <p className="mt-3 whitespace-pre-wrap">{appeal.message}</p>
                    {appeal.status === "pending" ? <div className="mt-3 space-y-2"><textarea value={reviewNotes[appeal.id] || ""} onChange={(event) => setReviewNotes((previous) => ({ ...previous, [appeal.id]: event.target.value }))} placeholder={t("banReviewNote")} rows={2} className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2" /><div className="flex flex-wrap gap-2"><button type="button" disabled={mutating} onClick={() => void handleReview(appeal, "accepted")} className="rounded-lg border border-accent-success px-3 py-2 text-accent-success disabled:opacity-50">{t("banAccept")}</button><button type="button" disabled={mutating || details.status !== "active"} onClick={() => void handleReview(appeal, "accepted", true)} className="rounded-lg bg-accent-success px-3 py-2 text-white disabled:opacity-50">{t("banAcceptAndLift")}</button><button type="button" disabled={mutating} onClick={() => void handleReview(appeal, "rejected")} className="rounded-lg border border-accent-error px-3 py-2 text-accent-error disabled:opacity-50">{t("banReject")}</button></div></div> : <p className="mt-3 whitespace-pre-wrap text-text-secondary">{appeal.reviewer_note || "-"}</p>}
                  </article>
                ))}</div>}
              </div>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
