"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { CheckCircle2, Download } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { FlagSubmitForm } from "@/components/wargame/FlagSubmitForm";
import { EnvInstanceSection } from "@/components/wargame/EnvInstanceSection";
import { Link } from "@/i18n/navigation";
import { apiFetch, ApiError, captureApiRequestOwner, isCurrentApiRequestOwner } from "@/lib/api-client";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import type { EnvInstance, SubmitResult } from "@/types/challenge";
import { useAuth } from "@/providers/AuthProvider";
import { captainTeams, isFixedRosterCompetition } from "@/lib/competition-presentation";
import { attachmentByteLabel, challengeAttachmentHref } from "@/lib/challenge-attachment-download";
import type { CaptainTeam, CompetitionAssignmentCategory as AssignmentCategory, CompetitionChallenge,
  CompetitionChallengeDetail, CompetitionDetail as Competition, CompetitionEnvironmentStatus as EnvironmentStatus } from "@/types/competition";
import type { ChallengeAttachmentDescriptor } from "@/types/challenge-attachment";

type AssignmentState = "not_joined" | "select_category" | "assigned";

interface EnvInstanceResponse extends EnvInstance {
  extend_count: number;
  max_extend_count: number;
}

interface EnvInstanceSummary {
  id: string;
  challenge_id: string | number;
  port: string | number;
  status: string;
  expires_at: string;
  extend_count?: string | number;
  max_extend_count?: string | number;
}

interface EnvExtendResponse {
  expires_at: string;
  extend_count: number;
  max_extend_count: number;
}

const ENV_ACCESS_MODE = process.env.NEXT_PUBLIC_ENV_ACCESS_MODE || "proxy";

function parseNonNegativeCounter(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
}

function normalizeEnvironmentStatus(value: unknown): EnvironmentStatus {
  switch (value) {
    case "not_required":
    case "ready":
    case "legacy_unverified":
    case "verification_required":
      return value;
    default:
      return "verification_required";
  }
}

export default function CompetitionDetailPage() {
  const params = useParams();
  const t = useTranslations("ctf");
  const tw = useTranslations("wargame");
  const tc = useTranslations("common");
  const compId = params.id as string;
  const { user, sessionStatus, loading: authLoading } = useAuth();
  const userId = user?.id ?? null;
  const ownerKey = `${compId}:${userId ?? "anonymous"}:${sessionStatus}`;
  const scopeRef = useRef(ownerKey);
  scopeRef.current = ownerKey;

  const [loadedComp, setComp] = useState<Competition | null>(null);
  const [loadedOwnerKey, setLoadedOwnerKey] = useState<string | null>(null);
  const comp = loadedOwnerKey === ownerKey ? loadedComp : null;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedChallenge, setSelectedChallenge] = useState<number | null>(null);
  const [joining, setJoining] = useState(false);
  const [categorySelecting, setCategorySelecting] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [teams, setTeams] = useState<CaptainTeam[]>([]);
  const [teamsLoading, setTeamsLoading] = useState(false);
  const [teamsError, setTeamsError] = useState<string | null>(null);
  const [teamsVersion, setTeamsVersion] = useState(0);
  const [selectedTeamId, setSelectedTeamId] = useState("");
  const [challengeDetail, setChallengeDetail] = useState<CompetitionChallengeDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailRefreshVersion, setDetailRefreshVersion] = useState(0);
  const [unlockedHints, setUnlockedHints] = useState<Record<number, string>>({});
  const [hintLoading, setHintLoading] = useState<number | null>(null);
  const [hintError, setHintError] = useState<string | null>(null);

  // env instance 상태
  const [envInstance, setEnvInstance] = useState<EnvInstance | null>(null);
  const [envLoading, setEnvLoading] = useState(false);
  const [envError, setEnvError] = useState<string | null>(null);
  const [timeRemaining, setTimeRemaining] = useState("");
  const [extendCount, setExtendCount] = useState<number | null>(null);
  const [maxExtendCount, setMaxExtendCount] = useState<number | null>(null);
  const [showStopConfirm, setShowStopConfirm] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const joinRequestRef = useRef(false);
  const categoryRequestRef = useRef(false);
  const envRequestRef = useRef(false);
  const envChallengeRef = useRef<number | null>(null);
  const competitionRequestRef = useRef(0);
  const competitionAbortRef = useRef<AbortController | null>(null);
  const detailRequestRef = useRef(0);
  const hintRequestRef = useRef<number | null>(null);

  const visibleChallenges = useMemo(
    () => comp?.visible_challenges ?? comp?.challenges ?? [],
    [comp],
  );
  const assignmentState: AssignmentState = comp?.assignment?.state ?? (comp?.joined ? "assigned" : "not_joined");
  const isJoined = comp?.joined ?? (assignmentState !== "not_joined");
  const needsCategorySelection = assignmentState === "select_category";
  const canAccessChallenges = assignmentState === "assigned";
  const assignmentCategories = comp?.assignment?.categories ?? [];
  const fixedTeam = comp !== null && isFixedRosterCompetition(comp);
  const needsTeamSelection = fixedTeam && !isJoined && comp?.status !== "ended";
  const selectedVisibleChallenge = useMemo(() => {
    if (!canAccessChallenges || selectedChallenge === null) return null;
    return visibleChallenges.find((challenge) => challenge.id === selectedChallenge) ?? null;
  }, [canAccessChallenges, selectedChallenge, visibleChallenges]);
  const selectedChallengeDetail = challengeDetail?.id === selectedVisibleChallenge?.id
    ? challengeDetail
    : null;
  const selectedVisibleChallengeId = selectedVisibleChallenge?.id ?? null;
  const resolvedEnvType = selectedChallengeDetail?.env_type ?? selectedVisibleChallenge?.env_type;
  const environmentStatus = normalizeEnvironmentStatus(
    selectedChallengeDetail?.environment_status ?? selectedVisibleChallenge?.environment_status,
  );
  const environmentReady = resolvedEnvType === "none"
    || (selectedChallengeDetail?.environment_ready ?? selectedVisibleChallenge?.environment_ready) === true;
  const extensionLimitReached = extendCount === null || maxExtendCount === null || extendCount >= maxExtendCount;
  const detailAttachments = useMemo(() => {
    if (!selectedChallengeDetail) return [];
    const descriptors: ChallengeAttachmentDescriptor[] = selectedChallengeDetail.attachments ?? selectedChallengeDetail.files.map((file) => ({
      id: null, legacy_index: file.id, name: file.name, byte_size: null, sha256: null,
      state: "ready", download_path: file.download_path,
    }));
    return descriptors.map((file) => ({ ...file,
      href: file.state === "ready" ? challengeAttachmentHref(file.download_path, selectedChallengeDetail.id, compId) : null,
    }));
  }, [compId, selectedChallengeDetail]);

  const challengesByCategory = useMemo(
    () => visibleChallenges.reduce<Record<string, CompetitionChallenge[]>>(
      (acc, challenge) => {
        if (!acc[challenge.category]) acc[challenge.category] = [];
        acc[challenge.category].push(challenge);
        return acc;
      },
      {},
    ),
    [visibleChallenges],
  );

  const fetchCompetition = useCallback(async (background = false): Promise<boolean> => {
    const scope = ownerKey;
    const requestId = competitionRequestRef.current + 1;
    competitionRequestRef.current = requestId;
    competitionAbortRef.current?.abort();
    const controller = new AbortController();
    competitionAbortRef.current = controller;
    if (!background) {
      setLoading(true);
      setError(null);
    }
    try {
      const requestOwner = userId ? captureApiRequestOwner(userId) : undefined;
      const data = await apiFetch<Competition>(`/api/ctf/competitions/${compId}`, { signal: controller.signal, requestOwner });
      if (scopeRef.current !== scope || competitionRequestRef.current !== requestId || controller.signal.aborted) return false;
      setComp(data);
      setLoadedOwnerKey(scope);
      if (!background) setError(null);
      return true;
    } catch (err) {
      if (scopeRef.current === scope && competitionRequestRef.current === requestId && !controller.signal.aborted && !background) {
        setError(err instanceof ApiError ? err.message : t("loadFailed"));
      }
      return false;
    } finally {
      if (scopeRef.current === scope && competitionRequestRef.current === requestId && !controller.signal.aborted && !background) {
        setLoading(false);
      }
    }
  }, [compId, ownerKey, t, userId]);

  useEffect(() => {
    if (authLoading) return;
    void fetchCompetition();
    return () => {
      competitionRequestRef.current += 1;
      competitionAbortRef.current?.abort();
    };
  }, [authLoading, fetchCompetition]);

  useEffect(() => {
    joinRequestRef.current = false;
    categoryRequestRef.current = false;
    setJoining(false);
    setCategorySelecting(null);
    setActionError(null);
    setSelectedTeamId("");
    setSelectedChallenge(null);
  }, [ownerKey]);

  useEffect(() => {
    const controller = new AbortController();
    const scope = ownerKey;
    setTeams([]);
    setTeamsError(null);
    setSelectedTeamId("");
    if (!needsTeamSelection || !userId || sessionStatus !== "authenticated") {
      setTeamsLoading(false);
      return () => controller.abort();
    }
    setTeamsLoading(true);
    (async () => {
      try {
        const requestOwner = captureApiRequestOwner(userId);
        const data = await apiFetch<CaptainTeam[]>("/api/auth/teams", { signal: controller.signal, requestOwner });
        if (scopeRef.current !== scope || controller.signal.aborted || !isCurrentApiRequestOwner(requestOwner)) return;
        setTeams(captainTeams(data, userId));
      } catch (err) {
        if (scopeRef.current === scope && !controller.signal.aborted) setTeamsError(err instanceof ApiError ? err.message : t("teamsLoadFailed"));
      } finally {
        if (scopeRef.current === scope && !controller.signal.aborted) setTeamsLoading(false);
      }
    })();
    return () => controller.abort();
  }, [needsTeamSelection, ownerKey, sessionStatus, t, teamsVersion, userId]);

  useEffect(() => {
    if (!canAccessChallenges && selectedChallenge) {
      setSelectedChallenge(null);
    }
  }, [canAccessChallenges, selectedChallenge]);

  useEffect(() => {
    const requestId = detailRequestRef.current + 1;
    detailRequestRef.current = requestId;
    hintRequestRef.current = null;
    setChallengeDetail(null);
    setDetailError(null);
    setUnlockedHints({});
    setHintLoading(null);
    setHintError(null);

    if (selectedVisibleChallengeId === null) {
      setDetailLoading(false);
      return;
    }

    let isCurrent = true;
    setDetailLoading(true);

    (async () => {
      try {
        const detail = await apiFetch<CompetitionChallengeDetail>(
          `/api/learn/competitions/${encodeURIComponent(compId)}/challenges/${selectedVisibleChallengeId}`,
        );
        if (!isCurrent || detailRequestRef.current !== requestId) return;

        if (detail.id !== selectedVisibleChallengeId) {
          setDetailError(t("challengeDetailLoadFailed"));
          return;
        }

        setChallengeDetail(detail);
      } catch (err) {
        if (!isCurrent || detailRequestRef.current !== requestId) return;
        setDetailError(err instanceof ApiError ? err.message : t("challengeDetailLoadFailed"));
      } finally {
        if (isCurrent && detailRequestRef.current === requestId) {
          setDetailLoading(false);
        }
      }
    })();

    return () => {
      isCurrent = false;
    };
  }, [compId, detailRefreshVersion, selectedVisibleChallengeId, t]);

  // 챌린지 선택 시 기존 인스턴스 확인
  useEffect(() => {
    if (!selectedChallenge || !canAccessChallenges) {
      envChallengeRef.current = null;
      setEnvInstance(null);
      setEnvError(null);
      setExtendCount(null);
      setMaxExtendCount(null);
      setShowStopConfirm(false);
      return;
    }
    const challenge = selectedVisibleChallenge;
    if (!challenge || resolvedEnvType === "none" || !resolvedEnvType) {
      envChallengeRef.current = null;
      setEnvInstance(null);
      setEnvError(null);
      setExtendCount(null);
      setMaxExtendCount(null);
      setShowStopConfirm(false);
      if (!challenge) setSelectedChallenge(null);
      return;
    }

    const selectedChallengeChanged = envChallengeRef.current !== selectedChallenge;
    envChallengeRef.current = selectedChallenge;
    let isCurrent = true;
    if (selectedChallengeChanged) {
      setEnvInstance(null);
      setEnvError(null);
      setExtendCount(null);
      setMaxExtendCount(null);
      setShowStopConfirm(false);
    }

    (async () => {
      try {
        const list = await apiFetch<EnvInstanceSummary[]>("/api/env/instances");
        const existing = list.find((inst) => String(inst.challenge_id) === String(selectedChallenge));
        if (isCurrent && existing) {
          setEnvInstance({
            id: existing.id,
            port: Number(existing.port),
            status: existing.status,
            expires_at: existing.expires_at,
          });
          setExtendCount(parseNonNegativeCounter(existing.extend_count));
          setMaxExtendCount(parseNonNegativeCounter(existing.max_extend_count));
        }
      } catch {
        // 무시
      }
    })();

    return () => {
      isCurrent = false;
    };
  }, [canAccessChallenges, resolvedEnvType, selectedChallenge, selectedVisibleChallenge]);

  // TTL 카운트다운
  useEffect(() => {
    if (!envInstance?.expires_at) {
      if (timerRef.current) clearInterval(timerRef.current);
      return;
    }
    const updateTimer = () => {
      const raw = envInstance.expires_at;
      let expiresAt = new Date(raw).getTime();
      if (Number.isNaN(expiresAt)) expiresAt = Number(raw) * 1000;
      if (Number.isNaN(expiresAt)) { setTimeRemaining("--:--"); return; }
      const remaining = Math.max(0, Math.floor((expiresAt - Date.now()) / 1000));
      if (remaining <= 0) {
        setEnvInstance(null);
        setTimeRemaining("");
        setExtendCount(null);
        setMaxExtendCount(null);
        return;
      }
      const m = Math.floor(remaining / 60);
      const s = remaining % 60;
      setTimeRemaining(`${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`);
    };
    updateTimer();
    timerRef.current = setInterval(updateTimer, 1000);
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, [envInstance?.expires_at]);

  const startEnv = async () => {
    const challengeId = selectedChallenge;
    if (
      challengeId === null
      || !canAccessChallenges
      || !visibleChallenges.some((challenge) => challenge.id === challengeId)
      || envInstance
      || envLoading
      || envRequestRef.current
      || !environmentReady
    ) return;

    envRequestRef.current = true;
    setEnvLoading(true);
    setEnvError(null);
    try {
      const res = await apiFetch<EnvInstanceResponse>("/api/env/instances/create", {
        method: "POST",
        body: { challenge_id: challengeId, competition_id: compId },
      });
      const nextExtendCount = parseNonNegativeCounter(res.extend_count);
      const nextMaxExtendCount = parseNonNegativeCounter(res.max_extend_count);
      if (nextExtendCount === null || nextMaxExtendCount === null || !res.expires_at) {
        throw new Error("invalid_instance_response");
      }
      setEnvInstance(res);
      setExtendCount(nextExtendCount);
      setMaxExtendCount(nextMaxExtendCount);
    } catch (err) {
      setEnvError(err instanceof ApiError ? err.message : tw("envStartFailed"));
    } finally {
      setEnvLoading(false);
      envRequestRef.current = false;
    }
  };

  const stopEnv = async () => {
    if (!envInstance || envLoading || envRequestRef.current) return;

    envRequestRef.current = true;
    setEnvLoading(true);
    setEnvError(null);
    try {
      await apiFetch(`/api/env/instances/${envInstance.id}/stop`, { method: "POST" });
      setEnvInstance(null);
      setExtendCount(null);
      setMaxExtendCount(null);
    } catch (err) {
      setEnvError(err instanceof ApiError ? err.message : tw("envStopFailed"));
    } finally {
      setEnvLoading(false);
      envRequestRef.current = false;
    }
  };

  const extendEnv = async () => {
    if (!envInstance || extensionLimitReached || envLoading || envRequestRef.current) return;

    envRequestRef.current = true;
    setEnvLoading(true);
    setEnvError(null);
    try {
      const res = await apiFetch<EnvExtendResponse>(`/api/env/instances/${envInstance.id}/extend`, { method: "POST" });
      const nextExtendCount = parseNonNegativeCounter(res.extend_count);
      const nextMaxExtendCount = parseNonNegativeCounter(res.max_extend_count);
      if (!res.expires_at || nextExtendCount === null || nextMaxExtendCount === null) {
        throw new Error("invalid_extension_response");
      }
      setExtendCount(nextExtendCount);
      setMaxExtendCount(nextMaxExtendCount);
      setEnvInstance((prev) => prev ? {
        ...prev,
        expires_at: res.expires_at,
        extend_count: nextExtendCount,
        max_extend_count: nextMaxExtendCount,
      } : null);
    } catch (err) {
      setEnvError(err instanceof ApiError ? err.message : tw("extendFailed"));
    } finally {
      setEnvLoading(false);
      envRequestRef.current = false;
    }
  };

  const getAccessUrl = (instanceId: string, port: number) => {
    if (typeof window === "undefined") return "";
    if (ENV_ACCESS_MODE === "direct") {
      return `${window.location.protocol}//${window.location.hostname}:${port}`;
    }
    return `${window.location.origin}/ctf/env/proxy/${instanceId}/`;
  };

  const handleJoin = async () => {
    if (isJoined || joining || joinRequestRef.current) return;
    if (fixedTeam && (!userId || sessionStatus !== "authenticated" || teamsLoading || teamsError || !teams.some((team) => team.id === selectedTeamId))) return;

    const scope = ownerKey;
    const teamId = selectedTeamId;
    joinRequestRef.current = true;
    setJoining(true);
    setActionError(null);
    try {
      const requestOwner = userId ? captureApiRequestOwner(userId) : undefined;
      await apiFetch(`/api/ctf/competitions/${compId}/join`, { method: "POST", requestOwner,
        ...(fixedTeam ? { body: { team_id: teamId } } : {}),
      });
      if (scopeRef.current !== scope || requestOwner && !isCurrentApiRequestOwner(requestOwner)) return;
      if (!await fetchCompetition()) setActionError(t("registrationRefreshFailed"));
    } catch (err) {
      if (scopeRef.current === scope) setActionError(err instanceof ApiError ? err.message : t("joinFailed"));
    } finally {
      if (scopeRef.current === scope) {
        setJoining(false);
        joinRequestRef.current = false;
      }
    }
  };

  const handleCategorySelect = async (category: AssignmentCategory) => {
    if (
      !needsCategorySelection
      || category.assigned
      || category.available_count <= 0
      || categorySelecting !== null
      || categoryRequestRef.current
    ) return;

    categoryRequestRef.current = true;
    const scope = ownerKey;
    setCategorySelecting(category.category);
    setActionError(null);
    try {
      await apiFetch(
        `/api/ctf/competitions/${compId}/select-category?category=${encodeURIComponent(category.category)}`,
        { method: "POST" },
      );
      if (scopeRef.current === scope) await fetchCompetition();
    } catch (err) {
      if (scopeRef.current === scope) setActionError(err instanceof ApiError ? err.message : t("categorySelectionFailed"));
    } finally {
      if (scopeRef.current === scope) {
        setCategorySelecting(null);
        categoryRequestRef.current = false;
      }
    }
  };

  const handleChallengeToggle = (challengeId: number) => {
    detailRequestRef.current += 1;
    hintRequestRef.current = null;
    setSelectedChallenge((current) => current === challengeId ? null : challengeId);
  };

  const retryChallengeDetail = () => {
    if (selectedVisibleChallengeId === null || detailLoading) return;

    detailRequestRef.current += 1;
    setDetailRefreshVersion((version) => version + 1);
  };

  const unlockCompetitionHint = async (hintId: number) => {
    const detail = selectedChallengeDetail;
    const challengeId = selectedVisibleChallengeId;
    if (
      !detail
      || challengeId === null
      || detail.id !== challengeId
      || !detail.hints.some((hint) => hint.id === hintId)
      || hintLoading !== null
      || hintRequestRef.current !== null
    ) return;

    const requestId = detailRequestRef.current;
    hintRequestRef.current = hintId;
    setHintLoading(hintId);
    setHintError(null);
    try {
      const response = await apiFetch<{ content: string }>(
        `/api/learn/challenges/${challengeId}/hints/${hintId}/unlock?competition_id=${encodeURIComponent(compId)}`,
        { method: "POST" },
      );
      if (detailRequestRef.current !== requestId || hintRequestRef.current !== hintId) return;
      setUnlockedHints((previous) => ({ ...previous, [hintId]: response.content }));
    } catch (err) {
      if (detailRequestRef.current !== requestId || hintRequestRef.current !== hintId) return;
      setHintError(err instanceof ApiError ? err.message : t("hintUnlockFailed"));
    } finally {
      if (hintRequestRef.current === hintId) {
        setHintLoading(null);
        hintRequestRef.current = null;
      }
    }
  };

  const handleSubmit = async (submittedChallengeId: number, flag: string): Promise<SubmitResult> => {
    if (
      !submittedChallengeId
      || submittedChallengeId !== selectedChallenge
      || !canAccessChallenges
      || !visibleChallenges.some((challenge) => challenge.id === submittedChallengeId)
    ) {
      return { status: "error", message: t("selectChallenge") };
    }
    if (comp?.status !== "ongoing") {
      return { status: "error", message: t("competitionNotActive") || "대회 진행 중이 아닙니다." };
    }
    const res = await apiFetch<SubmitResult>(
      `/api/learn/challenges/${submittedChallengeId}/submit`,
      { method: "POST", body: { flag, competition_id: compId } },
    );
    if (res.status === "correct" || res.status === "already_solved") {
      setComp((current) => current ? {
        ...current,
        challenges: current.challenges.map((challenge) => (
          challenge.id === submittedChallengeId ? { ...challenge, solved: true } : challenge
        )),
        visible_challenges: current.visible_challenges?.map((challenge) => (
          challenge.id === submittedChallengeId ? { ...challenge, solved: true } : challenge
        )),
      } : current);
      void fetchCompetition(true);
    }
    return res;
  };

  if (loading) {
    return (
      <div className="mx-auto max-w-5xl space-y-8">
        <div className="animate-pulse space-y-4">
          <div className="h-48 rounded-xl bg-bg-surface" />
          <div className="h-64 rounded-xl bg-bg-surface" />
        </div>
      </div>
    );
  }

  if (error || !comp) {
    return (
      <div className="mx-auto max-w-5xl">
        <div className="rounded-xl border border-accent-error/30 bg-accent-error/5 p-6 text-center">
          <p className="text-sm text-accent-error">{error ?? tc("noData")}</p>
          <Button variant="secondary" className="mt-4" onClick={() => { void fetchCompetition(); }}>
            {tc("retry")}
          </Button>
        </div>
      </div>
    );
  }

  const currentChallenge = selectedVisibleChallenge;
  const displayChallengeDescription = selectedChallengeDetail
    ? selectedChallengeDetail.description
    : currentChallenge?.description;
  const hasEnv = Boolean(resolvedEnvType && resolvedEnvType !== "none");
  const environmentUnavailableDescription = environmentStatus === "legacy_unverified"
    ? t("environmentLegacyUnverifiedDesc")
    : t("environmentVerificationRequiredDesc");

  return (
    <div className="mx-auto max-w-5xl space-y-8">
      {/* 대회 헤더 */}
      <div className="overflow-hidden rounded-xl border border-border bg-bg-surface">
        {comp.banner_url && (
          <div className="relative h-48 w-full overflow-hidden bg-bg-primary sm:h-56">
            <img
              src={comp.banner_url}
              alt={comp.title}
              loading="lazy"
              className="h-full w-full object-cover"
            />
            <div className="absolute inset-0 bg-gradient-to-t from-bg-surface to-transparent" />
          </div>
        )}
        <div className="p-6">
          <div className="mb-3 flex items-center gap-2">
            <span className="rounded-full bg-accent-success/10 px-2.5 py-0.5 text-xs font-medium text-accent-success">
              {comp.status === "ongoing" ? t("ongoing") : comp.status === "upcoming" ? t("upcoming") : t("ended")}
            </span>
            <span className="text-sm text-text-muted">{t(fixedTeam ? "registeredTeamCount" : "participantCount", { count: comp.participant_count })}</span>
          </div>
          <h1 className="mb-2 text-3xl font-bold">{comp.title}</h1>
          {comp.description && <MarkdownRenderer content={comp.description} />}

          {needsTeamSelection && (
            <div className="mt-4 space-y-2">
              {!userId || sessionStatus !== "authenticated" ? (
                <Link href="/login" className="text-sm text-accent-primary underline">{t("teamLoginRequired")}</Link>
              ) : teamsError ? (
                <div role="alert" className="text-sm text-accent-error">
                  <p>{teamsError}</p>
                  <Button type="button" variant="secondary" className="mt-2" onClick={() => setTeamsVersion((version) => version + 1)}>{tc("retry")}</Button>
                </div>
              ) : (
                <div>
                  <label htmlFor="competition-team" className="mb-1 block text-sm font-medium">{t("registrationTeam")}</label>
                  <select id="competition-team" value={selectedTeamId} onChange={(event) => setSelectedTeamId(event.target.value)}
                    disabled={teamsLoading || joining || teams.length === 0} aria-busy={teamsLoading}
                    className="min-h-11 w-full min-w-0 rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm sm:max-w-md">
                    <option value="">{teamsLoading ? tc("loading") : t("selectRegistrationTeam")}</option>
                    {teams.map((team) => <option key={team.id} value={team.id}>{team.name} ({team.member_count})</option>)}
                  </select>
                  {!teamsLoading && teams.length === 0 && <p className="mt-2 text-sm text-text-muted">{t("noCaptainTeams")}</p>}
                </div>
              )}
            </div>
          )}
          {fixedTeam && comp.team_registration && (
            <section className="mt-4 border-t border-border pt-4" aria-labelledby="registered-team-title">
              <h2 id="registered-team-title" className="break-words font-semibold">{t("registeredTeam")}: {comp.team_registration.team_name}</h2>
              <details className="mt-2">
                <summary className="min-h-11 cursor-pointer py-2 text-sm text-text-secondary">{t("registeredRoster", { count: comp.team_registration.members.length })}</summary>
                <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-2 text-sm">
                  {comp.team_registration.members.map((member) => <li key={member.user_id} className="min-w-0 break-words">{member.username}</li>)}
                </ul>
              </details>
            </section>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-4">
            {!isJoined && comp.status !== "ended" ? (
              <Button onClick={handleJoin} disabled={joining || fixedTeam && (!userId || sessionStatus !== "authenticated" || teamsLoading || Boolean(teamsError) || !selectedTeamId)}>
                {joining ? tc("loading") : t("join")}
              </Button>
            ) : isJoined ? (
              <span className="rounded-lg bg-accent-success/10 px-3 py-1.5 text-sm font-medium text-accent-success">
                {t("joined")}
              </span>
            ) : null}
            <Link href={`/ctf/${compId}/scoreboard`}>
              <Button variant="secondary">{t("scoreboard")}</Button>
            </Link>
          </div>
          {actionError && (
            <p className="mt-3 text-sm text-accent-error" role="alert">
              {actionError}
            </p>
          )}
        </div>
      </div>

      {/* 대회 문제 목록 */}
      {needsCategorySelection && (
        <section className="rounded-xl border border-border bg-bg-surface p-6" aria-labelledby="category-selection-title">
          <h2 id="category-selection-title" className="text-xl font-semibold">
            {fixedTeam ? t("selectTeamCategory") : t("selectCategory")}
          </h2>
          <p className="mt-2 text-sm text-text-secondary">{fixedTeam ? t("selectTeamCategoryDescription") : t("selectCategoryDescription")}</p>

          {assignmentCategories.length > 0 ? (
            <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {assignmentCategories.map((category) => {
                const isUnavailable = category.available_count <= 0;
                const isDisabled = category.assigned || isUnavailable || categorySelecting !== null;

                return (
                  <button
                    key={category.category}
                    type="button"
                    onClick={() => handleCategorySelect(category)}
                    disabled={isDisabled}
                    className="rounded-xl border border-border bg-bg-primary p-4 text-left transition-colors hover:border-accent-primary disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <span className="font-medium text-text-primary">{category.category}</span>
                      <span className="text-xs text-text-muted">
                        {t("availableChallenges", { count: category.available_count })}
                      </span>
                    </div>
                    <span className="mt-3 block text-sm font-medium text-accent-primary">
                      {category.assigned
                        ? t("categoryAssigned")
                        : isUnavailable
                          ? t("categoryUnavailable")
                          : categorySelecting === category.category
                            ? t("categorySelecting")
                            : t("selectCategory")}
                    </span>
                  </button>
                );
              })}
            </div>
          ) : (
            <p className="mt-4 rounded-lg border border-border bg-bg-primary p-4 text-sm text-text-muted">
              {t("noCategoriesAvailable")}
            </p>
          )}
        </section>
      )}

      {canAccessChallenges && Object.keys(challengesByCategory).length > 0 && (
        <div className="space-y-6">
          {Object.entries(challengesByCategory).map(([category, challenges]) => (
            <div key={category}>
              <h2 className="mb-3 text-xl font-semibold">{category}</h2>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {challenges.map((challenge) => (
                  <button
                    key={challenge.id}
                    type="button"
                    onClick={() => handleChallengeToggle(challenge.id)}
                    disabled={envLoading}
                    className={`rounded-xl border p-4 text-left transition-all ${
                      selectedChallenge === challenge.id
                        ? "border-accent-primary bg-accent-primary/5"
                        : challenge.solved
                          ? "border-accent-success/30 bg-accent-success/5"
                          : "border-border bg-bg-surface hover:border-accent-primary"
                    } disabled:cursor-not-allowed disabled:opacity-60`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium">{challenge.name}</span>
                      {challenge.solved && (
                        <svg className="h-5 w-5 text-accent-success" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5}>
                          <polyline points="20 6 9 17 4 12" />
                        </svg>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-sm text-text-muted">{challenge.value}{tc("pointsUnit")}</span>
                      {challenge.first_blood && (
                        <span className="rounded-full bg-red-500/10 px-2 py-0.5 text-[10px] font-bold text-red-400">
                          {t("firstBlood")}: {challenge.first_blood}
                        </span>
                      )}
                    </div>
                    {fixedTeam && challenge.team_solved && <span className="mt-2 inline-flex items-center gap-1 text-xs text-accent-success"><CheckCircle2 className="h-4 w-4 shrink-0" aria-hidden="true" />{t("teamSolved")}</span>}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {canAccessChallenges && Object.keys(challengesByCategory).length === 0 && (
        <div className="rounded-xl border border-border bg-bg-surface p-6 text-sm text-text-muted">
          {t("noAssignedChallenges")}
        </div>
      )}

      {/* 선택된 문제 — 설명 + 환경 + 플래그 제출 */}
      {currentChallenge && (
        <div className="space-y-4">
          <div className="rounded-xl border border-border bg-bg-surface p-6" aria-busy={detailLoading}>
            <h3 className="mb-3 text-lg font-semibold">
              {currentChallenge.name} — {t("submitFlag")}
            </h3>
            {detailLoading && (
              <div className="mb-4 flex items-center gap-2 rounded-lg border border-border/50 bg-bg-primary p-4 text-sm text-text-secondary" role="status" aria-live="polite">
                <span className="h-4 w-4 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" aria-hidden="true" />
                {t("challengeDetailLoading")}
              </div>
            )}
            {detailError && (
              <div className="mb-4 rounded-lg border border-accent-error/30 bg-accent-error/10 p-4" role="alert">
                <p className="text-sm text-accent-error">{detailError}</p>
                <Button type="button" variant="secondary" size="sm" className="mt-3" onClick={retryChallengeDetail} disabled={detailLoading}>
                  {t("retryChallengeDetail")}
                </Button>
              </div>
            )}
            {displayChallengeDescription && (
              <div className="mb-4 rounded-lg border border-border/50 bg-bg-primary p-4 text-sm leading-relaxed text-text-secondary">
                <MarkdownRenderer content={displayChallengeDescription} />
              </div>
            )}
            <FlagSubmitForm
              challengeId={currentChallenge.id}
              solved={currentChallenge.solved}
              onSubmit={handleSubmit}
            />
          </div>

          {detailAttachments.length > 0 && (
            <section className="rounded-xl border border-border bg-bg-surface p-6" aria-labelledby="challenge-attachments-title">
              <h4 id="challenge-attachments-title" className="mb-3 text-lg font-semibold">{t("attachments")}</h4>
              <ul className="space-y-2">
                {detailAttachments.map((file, index) => (
                  <li key={file.id ?? `legacy-${file.legacy_index ?? index}`}>
                    {file.href ? <a
                      href={file.href}
                      download
                      className="flex items-center justify-between gap-3 rounded-lg border border-border bg-bg-primary px-4 py-3 text-sm transition-colors hover:border-accent-primary hover:bg-bg-primary/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
                      aria-label={t("downloadAttachment", { name: file.name })}
                    >
                      <span className="min-w-0 break-words font-medium text-text-primary">{file.name}{file.byte_size !== null && <span className="ml-2 text-xs text-text-muted">{attachmentByteLabel(file.byte_size)}</span>}</span>
                      <Download className="h-4 w-4 shrink-0 text-text-muted" aria-hidden="true" />
                    </a> : <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border px-4 py-3 text-sm">
                      <span className="min-w-0 break-words font-medium">{file.name}</span><span className="text-text-muted">{t("attachmentUnavailable")}</span>
                    </div>}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {selectedChallengeDetail && selectedChallengeDetail.hints.length > 0 && (
            <section className="rounded-xl border border-border bg-bg-surface p-6" aria-labelledby="competition-hints-title">
              <h4 id="competition-hints-title" className="text-lg font-semibold">{t("hints")}</h4>
              <p id="competition-hints-free" className="mt-2 text-sm text-text-secondary">{t("competitionHintsFree")}</p>
              {hintError && <p className="mt-3 text-sm text-accent-error" role="alert">{hintError}</p>}
              <ul className="mt-4 space-y-3">
                {selectedChallengeDetail.hints.map((hint, index) => {
                  const isUnlocked = Object.prototype.hasOwnProperty.call(unlockedHints, hint.id);
                  return (
                    <li key={hint.id} className="rounded-lg border border-border/50 bg-bg-primary p-4">
                      {isUnlocked ? (
                        <div className="text-sm leading-relaxed text-text-secondary" aria-live="polite">
                          <MarkdownRenderer content={unlockedHints[hint.id]} />
                        </div>
                      ) : (
                        <Button
                          type="button"
                          variant="secondary"
                          size="sm"
                          onClick={() => unlockCompetitionHint(hint.id)}
                          disabled={hintLoading !== null}
                          aria-describedby="competition-hints-free"
                          aria-label={t("openHintNumber", { number: index + 1 })}
                        >
                          {hintLoading === hint.id ? t("hintOpening") : t("openHint")}
                        </Button>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
          )}

          {/* 컨테이너 환경 */}
          {hasEnv && (
            <EnvInstanceSection
              envInstance={envInstance}
              envLoading={envLoading}
              envError={envError}
              timeRemaining={timeRemaining}
              extendCount={extendCount}
              maxExtendCount={maxExtendCount}
              extensionDisabled={extensionLimitReached || envLoading}
              environmentReady={environmentReady}
              environmentUnavailableTitle={t("environmentVerificationRequired")}
              environmentUnavailableDescription={environmentUnavailableDescription}
              onStart={startEnv}
              onStop={stopEnv}
              onExtend={extendEnv}
              showStopConfirm={showStopConfirm}
              onStopConfirmToggle={setShowStopConfirm}
              getAccessUrl={getAccessUrl}
            />
          )}
        </div>
      )}
    </div>
  );
}
