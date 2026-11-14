"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { apiFetch, ApiError } from "@/lib/api-client";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { useAuth } from "@/providers/AuthProvider";
import { Copy, RefreshCw, Trash2 } from "lucide-react";

interface TeamMember {
  user_id: string;
  username: string;
  role: string;
  score: number;
}

interface TeamDetail {
  id: string;
  name: string;
  description: string;
  invite_code: string | null;
  captain_id: string;
  total_score: number;
  members: TeamMember[];
}

export default function TeamDetailPage() {
  const params = useParams();
  const t = useTranslations("teams");
  const tc = useTranslations("common");
  const { user } = useAuth();
  const userId = user?.id;
  const teamId = params.id as string;

  const [team, setTeam] = useState<TeamDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [inviteInput, setInviteInput] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const generation = useRef(0);

  const loadTeam = useCallback(async (request = generation.current) => {
    const value = await apiFetch<TeamDetail>(`/api/auth/teams/${teamId}`);
    if (request === generation.current) setTeam(value);
  }, [teamId]);

  useEffect(() => {
    generation.current += 1;
    setLoading(true);
    setError(null);
    setActionError(null);
    setBusy(null);
    let active = true;
    apiFetch<TeamDetail>(`/api/auth/teams/${teamId}`)
      .then((value) => { if (active) setTeam(value); })
      .catch((err) => { if (active) setError(err instanceof ApiError ? err.message : t("loadFailed")); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; generation.current += 1; };
  }, [teamId, t, userId]);

  const runAction = async (name: string, action: () => Promise<void>) => {
    if (busy) return;
    const request = generation.current;
    setBusy(name);
    setActionError(null);
    try { await action(); } catch (failure) {
      if (request !== generation.current) return;
      setActionError(failure instanceof ApiError ? failure.message : tc("error"));
      try { await loadTeam(); } catch { /* 실패한 동작의 현재 상태를 확인할 수 없으면 기존 화면을 보존한다. */ }
    } finally { if (request === generation.current) setBusy(null); }
  };

  const join = async (event: React.FormEvent) => {
    event.preventDefault();
    await runAction("join", async () => {
      const request = generation.current;
      await apiFetch(`/api/auth/teams/${teamId}/join`, { method: "POST", body: {invite_code: inviteInput} });
      if (request !== generation.current) return;
      setInviteInput("");
      await loadTeam(request);
    });
  };

  const regenerate = () => runAction("invite", async () => {
    const request = generation.current;
    const result = await apiFetch<{invite_code: string}>(`/api/auth/teams/${teamId}/invite`, {method: "POST"});
    if (request !== generation.current) return;
    setTeam((value) => value ? {...value, invite_code: result.invite_code} : value);
    setCopied(false);
  });

  const remove = (memberId: string) => runAction(memberId, async () => {
    const request = generation.current;
    await apiFetch(`/api/auth/teams/${teamId}/members/${memberId}`, {method: "DELETE"});
    if (request === generation.current) await loadTeam(request);
  });

  const copyInvite = async () => {
    if (!team || !team.invite_code) return;
    try {
      await navigator.clipboard.writeText(team.invite_code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setActionError(tc("error"));
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }

  if (error) {
    return <div className="py-20 text-center text-status-error">{error}</div>;
  }

  if (!team) return null;
  const isCaptain = team.captain_id === userId;
  const isMember = team.members.some((member) => member.user_id === userId);

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      {actionError && <p role="alert" className="text-sm text-accent-error">{actionError}</p>}
      {/* 팀 헤더 */}
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <h1 className="break-all text-2xl font-bold">{team.name}</h1>
        {team.description && <MarkdownRenderer content={team.description} />}
        <div className="mt-4 flex flex-wrap items-center gap-4">
          <span className="text-sm text-text-muted">{t("totalScore", { score: team.total_score.toLocaleString() })}</span>
          <span className="text-sm text-text-muted">{t("memberCount", { count: team.members.length })}</span>
        </div>
      </div>

      {user && !isMember && <form onSubmit={(event) => void join(event)} className="flex flex-wrap items-end gap-3 border-y border-border py-4">
        <label className="min-w-0 flex-1 text-sm">
          {t("inviteCode")}
          <input value={inviteInput} onChange={(event) => setInviteInput(event.target.value)} required maxLength={32}
            disabled={busy !== null} autoComplete="off"
            className="mt-1 block w-full rounded-md border border-border bg-bg-primary px-3 py-2" />
        </label>
        <Button type="submit" disabled={busy !== null || !inviteInput.trim()}>{t("join")}</Button>
      </form>}

      {/* 초대 링크 — 소속 멤버에게만 표시 */}
      {team.invite_code && team.members.some((member) => member.user_id === userId) && (
        <div className="rounded-xl border border-border bg-bg-surface p-6">
          <h2 className="mb-3 text-lg font-semibold">{t("inviteCode")}</h2>
          <div className="flex flex-wrap items-center gap-3">
            <code className="min-w-0 flex-1 break-all rounded-lg bg-bg-primary px-4 py-2 font-mono text-sm">
              {team.invite_code}
            </code>
            <Button variant="secondary" size="sm" onClick={copyInvite} aria-label={copied ? t("copied") : t("copy")}
              title={copied ? t("copied") : t("copy")}>
              <Copy className="h-4 w-4" aria-hidden="true" />
            </Button>
            {isCaptain && <Button variant="secondary" size="sm" onClick={() => void regenerate()} disabled={busy !== null}
              aria-label={t("regenerateInvite")} title={t("regenerateInvite")}>
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
            </Button>}
          </div>
        </div>
      )}

      {/* 멤버 목록 */}
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <h2 className="mb-4 text-lg font-semibold">{t("members")}</h2>
        <div className="space-y-3">
          {team.members.map((member) => (
            <div
              key={member.user_id}
              className="flex items-center justify-between rounded-lg border border-border bg-bg-primary px-4 py-3"
            >
              <div className="flex min-w-0 items-center gap-3">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent-primary/10 text-sm font-bold text-accent-primary">
                  {member.username[0].toUpperCase()}
                </div>
                <span className="min-w-0 break-all font-medium">{member.username}</span>
                {member.role === "captain" && (
                  <span className="shrink-0 rounded-full bg-accent-warning/10 px-2 py-0.5 text-xs text-accent-warning">
                    {t("captain")}
                  </span>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-3"><span className="text-sm font-medium text-accent-primary">
                {member.score}{tc("pointsUnit")}
              </span>
              {isCaptain && member.user_id !== userId && <button type="button" disabled={busy !== null}
                onClick={() => void remove(member.user_id)} aria-label={`${tc("delete")} ${member.username}`} title={tc("delete")}
                className="rounded p-2 text-accent-error hover:bg-accent-error/10 disabled:opacity-50">
                <Trash2 className="h-4 w-4" aria-hidden="true" />
              </button>}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
