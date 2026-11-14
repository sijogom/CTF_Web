"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useParams } from "next/navigation";
import { useTranslations, useLocale } from "next-intl";
import { CategoryBadge } from "@/components/ui/Badge";
import { FlagSubmitForm } from "@/components/wargame/FlagSubmitForm";
import { Button } from "@/components/ui/Button";
import { EnvInstanceSection } from "@/components/wargame/EnvInstanceSection";
import { HintSection } from "@/components/wargame/HintSection";
import { AttachmentSection } from "@/components/wargame/AttachmentSection";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { LearningPathPanel } from "@/components/wargame/LearningPathPanel";
import { knowledgeExtensionsEnabled } from "@/lib/knowledge-release-stage.mjs";
import { apiFetch, ApiError, captureApiRequestOwner, isCurrentApiRequestOwner } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import { useAuth } from "@/providers/AuthProvider";
import { Link } from "@/i18n/navigation";

import type { ChallengeDetail, EnvInstance, SubmitResult } from "@/types/challenge";

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

function parseNonNegativeCounter(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
}

export default function ChallengeDetailPage() {
  const params = useParams();
  const t = useTranslations("wargame");
  const tc = useTranslations("common");
  const locale = useLocale();
  const { toast } = useToast();
  const { user, refresh, sessionStatus } = useAuth();
  const [unlockedHints, setUnlockedHints] = useState<Record<number, string>>({});
  const [loadedChallenge, setChallenge] = useState<ChallengeDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hintLoading, setHintLoading] = useState<number | null>(null);

  // 환경 관련 상태
  const [envInstance, setEnvInstance] = useState<EnvInstance | null>(null);
  const [envLoading, setEnvLoading] = useState(false);
  const [envError, setEnvError] = useState<string | null>(null);
  const [timeRemaining, setTimeRemaining] = useState<string>("");
  const [extendCount, setExtendCount] = useState<number | null>(null);
  const [maxExtendCount, setMaxExtendCount] = useState<number | null>(null);
  const [hintConfirmId, setHintConfirmId] = useState<number | null>(null);
  const [showStopConfirm, setShowStopConfirm] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const envRequestRef = useRef(false);
  const envResponseRef = useRef(0);
  const challengeRequestRef = useRef(0);
  const challengeViewRef = useRef({ generation: 0, requestId: 0 });
  const detailViewRef = useRef(0);
  const hintRequestRef = useRef(0);
  const mountedRef = useRef(true);
  const hintViewRef = useRef({ key: "", generation: 0 });
  const unlockedHintViewRef = useRef(0);

  const challengeId = Number(params.id);
  const isInvalidId = Number.isNaN(challengeId) || challengeId <= 0;
  const ownerId = user?.id ?? "";
  const hintViewKey = `${challengeId}:${locale}:${ownerId || "anonymous"}:${sessionStatus}`;
  if (hintViewRef.current.key !== hintViewKey) {
    hintViewRef.current = { key: hintViewKey, generation: hintViewRef.current.generation + 1 };
  }
  const viewGeneration = hintViewRef.current.generation;
  const loadedChallengeRequestId = challengeViewRef.current.requestId;
  const challenge = challengeViewRef.current.generation === viewGeneration
    && loadedChallengeRequestId === challengeRequestRef.current ? loadedChallenge : null;
  const isCurrentView = useCallback((generation: number) => mountedRef.current
    && hintViewRef.current.generation === generation, []);
  const hasCurrentEnvironment = challenge?.id === challengeId
    && (challenge.env_type === "dockerfile" || challenge.env_type === "image")
    && challenge.environment_ready !== false && challenge.locked !== true;
  const visibleUnlockedHints = unlockedHintViewRef.current === hintViewRef.current.generation ? unlockedHints : {};
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      challengeRequestRef.current += 1;
      envResponseRef.current += 1;
    };
  }, []);
  useEffect(() => {
    hintRequestRef.current += 1;
    setUnlockedHints({});
    setHintLoading(null);
    setHintConfirmId(null);
    envResponseRef.current += 1;
    envRequestRef.current = false;
    setEnvInstance(null);
    setEnvLoading(false);
    setEnvError(null);
    setTimeRemaining("");
    setExtendCount(null);
    setMaxExtendCount(null);
    setShowStopConfirm(false);
  }, [hintViewKey]);

  // 컨테이너 접속 URL 구성
  // - direct 모드 (프로덕션 Linux): host:port 직접 접속 (CTF 업계 표준)
  // - proxy 모드 (개발 WSL2): Caddy 경유 프록시 (WSL2는 동적 포트 외부 노출 불가)
  const envAccessMode = process.env.NEXT_PUBLIC_ENV_ACCESS_MODE || "proxy";

  const getAccessUrl = useCallback((instanceId: string, port: number) => {
    if (typeof window === "undefined") return "";
    if (envAccessMode === "direct") {
      const host = window.location.hostname;
      return `http://${host}:${port}`;
    }
    // proxy 모드: Caddy → env-orchestrator → 컨테이너
    return `${window.location.origin}/ctf/env/proxy/${instanceId}/`;
  }, [envAccessMode]);

  const fetchChallenge = useCallback(async () => {
    const generation = viewGeneration;
    if (!isCurrentView(generation)) return;
    const requestId = ++challengeRequestRef.current;
    let owner: ReturnType<typeof captureApiRequestOwner> | null = null;
    const currentRequest = () => isCurrentView(generation) && challengeRequestRef.current === requestId
      && (owner === null || isCurrentApiRequestOwner(owner));
    detailViewRef.current = generation;
    if (isInvalidId) {
      setLoading(false);
      return;
    }
    try {
      setLoading(true);
      setError(null);
      if (ownerId) owner = captureApiRequestOwner(ownerId);
      const data = await apiFetch<ChallengeDetail>(`/api/learn/challenges/${challengeId}?lang=${locale}`,
        { requestOwner: owner ?? undefined });
      if (!currentRequest()) return;
      if (data.id !== challengeId) throw new Error("invalid_challenge_response");
      challengeViewRef.current = { generation, requestId };
      setChallenge(data);
    } catch (err) {
      if (!currentRequest()) return;
      setError(err instanceof ApiError ? err.message : t("loadFailed"));
    } finally {
      if (currentRequest()) setLoading(false);
    }
  }, [challengeId, isInvalidId, locale, t, ownerId, viewGeneration, isCurrentView]);

  // 기존 실행 중 인스턴스 복원
  const fetchExistingInstance = useCallback(async () => {
    const generation = viewGeneration;
    if (!hasCurrentEnvironment || !ownerId || sessionStatus !== "authenticated" || loadedChallenge?.id !== challengeId
      || loadedChallengeRequestId !== challengeRequestRef.current
      || envRequestRef.current || !isCurrentView(generation)) return;
    const detailRequestId = loadedChallengeRequestId;
    const requestId = ++envResponseRef.current;
    let owner: ReturnType<typeof captureApiRequestOwner> | null = null;
    const currentRequest = () => isCurrentView(generation) && challengeRequestRef.current === detailRequestId
      && envResponseRef.current === requestId && (owner === null || isCurrentApiRequestOwner(owner));
    try {
      owner = captureApiRequestOwner(ownerId);
      const instances = await apiFetch<EnvInstanceSummary[]>("/api/env/instances", { requestOwner: owner });
      if (!currentRequest()) return;
      const existing = instances.find(
        (inst) => String(inst.challenge_id) === String(challengeId) && inst.status === "running",
      );
      if (existing) {
        setEnvInstance({
          id: existing.id,
          port: Number(existing.port),
          status: existing.status,
          expires_at: existing.expires_at,
          extend_count: parseNonNegativeCounter(existing.extend_count) ?? undefined,
          max_extend_count: parseNonNegativeCounter(existing.max_extend_count) ?? undefined,
        });
        setExtendCount(parseNonNegativeCounter(existing.extend_count));
        setMaxExtendCount(parseNonNegativeCounter(existing.max_extend_count));
      } else {
        setEnvInstance(null);
        setTimeRemaining("");
        setExtendCount(null);
        setMaxExtendCount(null);
      }
    } catch {
      // 비로그인 또는 네트워크 오류 — 무시 (환경 시작 버튼은 정상 표시)
    }
  }, [challengeId, hasCurrentEnvironment, ownerId, sessionStatus, viewGeneration, isCurrentView, loadedChallenge, loadedChallengeRequestId]);

  useEffect(() => {
    fetchChallenge();
  }, [fetchChallenge]);

  useEffect(() => {
    fetchExistingInstance();
  }, [fetchExistingInstance]);

  // TTL 카운트다운 타이머
  useEffect(() => {
    if (!envInstance?.expires_at) {
      if (timerRef.current) clearInterval(timerRef.current);
      return;
    }

    const updateTimer = () => {
      if (!isCurrentView(viewGeneration)) return;
      // expires_at 형식: ISO 문자열("2026-...") 또는 Unix timestamp 문자열("1740549600")
      const raw = envInstance.expires_at;
      let expiresAt = new Date(raw).getTime();
      if (Number.isNaN(expiresAt)) {
        expiresAt = Number(raw) * 1000;
      }
      if (Number.isNaN(expiresAt)) {
        setTimeRemaining("--:--");
        if (timerRef.current) clearInterval(timerRef.current);
        return;
      }
      const diff = expiresAt - Date.now();
      if (diff <= 0) {
        setTimeRemaining("00:00");
        setEnvInstance(null);
        setExtendCount(null);
        setMaxExtendCount(null);
        if (timerRef.current) clearInterval(timerRef.current);
        return;
      }
      const minutes = Math.floor(diff / 60000);
      const seconds = Math.floor((diff % 60000) / 1000);
      setTimeRemaining(`${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`);
    };

    updateTimer();
    timerRef.current = setInterval(updateTimer, 1000);
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, [envInstance, viewGeneration, isCurrentView]);

  const handleSubmit = async (submittedChallengeId: number, flag: string): Promise<SubmitResult> => {
    const generation = viewGeneration;
    const detailRequestId = challengeRequestRef.current;
    let owner: ReturnType<typeof captureApiRequestOwner> | null = null;
    const currentRequest = () => isCurrentView(generation) && challengeRequestRef.current === detailRequestId
      && (owner === null || isCurrentApiRequestOwner(owner));
    try {
      if (user) owner = captureApiRequestOwner(user.id);
      const res = await apiFetch<SubmitResult>(
        `/api/learn/challenges/${submittedChallengeId}/submit`,
        { method: "POST", body: { flag }, requestOwner: owner ?? undefined },
      );
      if (currentRequest() && (res.status === "correct" || res.status === "already_solved")) {
        setChallenge((current) => current?.id === submittedChallengeId
          ? { ...current, solved: true }
          : current);
        try {
          const refreshed = await apiFetch<ChallengeDetail>(
            `/api/learn/challenges/${submittedChallengeId}?lang=${locale}`,
            { requestOwner: owner ?? undefined },
          );
          if (!currentRequest()) return res;
          if (refreshed.id !== submittedChallengeId) throw new Error("invalid_challenge_response");
          setChallenge((current) => current?.id === submittedChallengeId
            ? { ...refreshed, solved: refreshed.solved ?? true }
            : current);
        } catch {
          // 제출 성공은 이미 확정되었으므로 재조회 실패 시 로컬 완료 상태를 유지한다.
        }
      }
      return res;
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : t("loadFailed");
      if (currentRequest()) toast("error", msg);
      return { status: "error", message: msg };
    }
  };

  const unlockHint = async (hintId: number) => {
    if (hintLoading !== null) return;
    const generation = hintViewRef.current.generation;
    const requestId = ++hintRequestRef.current;
    let owner: ReturnType<typeof captureApiRequestOwner> | null = null;
    const currentView = () => mountedRef.current && hintViewRef.current.generation === generation
      && hintRequestRef.current === requestId;
    setHintLoading(hintId);
    try {
      owner = captureApiRequestOwner(user?.id ?? "");
      const res = await apiFetch<{ content: string }>(
        `/api/learn/challenges/${challengeId}/hints/${hintId}/unlock`,
        { method: "POST", requestOwner: owner },
      );
      if (!currentView() || !isCurrentApiRequestOwner(owner)) return;
      unlockedHintViewRef.current = generation;
      setUnlockedHints((prev) => ({ ...prev, [hintId]: res.content }));
      // The charge is confirmed; a failed stats refresh must not undo the hint.
      await refresh().catch(() => undefined);
    } catch (err) {
      if (!currentView() || (owner !== null && !isCurrentApiRequestOwner(owner))) return;
      const message = err instanceof ApiError ? err.message : t("loadFailed");
      toast("error", message);
    } finally {
      if (currentView()) setHintLoading(null);
    }
  };

  // 환경 시작
  const startEnv = async () => {
    const generation = viewGeneration;
    if (!hasCurrentEnvironment || loadedChallengeRequestId !== challengeRequestRef.current
      || !isCurrentView(generation) || envLoading || envRequestRef.current) return;
    const requestId = ++envResponseRef.current;
    let owner: ReturnType<typeof captureApiRequestOwner> | null = null;
    const currentRequest = () => isCurrentView(generation) && envResponseRef.current === requestId
      && (owner === null || isCurrentApiRequestOwner(owner));
    envRequestRef.current = true;
    setEnvLoading(true);
    setEnvError(null);
    try {
      owner = captureApiRequestOwner(user?.id ?? "");
      const res = await apiFetch<EnvInstance>("/api/env/instances/create", {
        method: "POST",
        body: { challenge_id: challengeId },
        requestOwner: owner,
      });
      if (!currentRequest()) return;
      const nextExtendCount = parseNonNegativeCounter(res.extend_count);
      const nextMaxExtendCount = parseNonNegativeCounter(res.max_extend_count);
      if (!res.expires_at || nextExtendCount === null || nextMaxExtendCount === null) {
        throw new Error("invalid_instance_response");
      }
      setEnvInstance(res);
      setExtendCount(nextExtendCount);
      setMaxExtendCount(nextMaxExtendCount);
    } catch (err) {
      if (!currentRequest()) return;
      const msg = err instanceof ApiError ? err.message : t("envStartFailed");
      setEnvError(msg);
      toast("error", msg);
    } finally {
      if (currentRequest()) {
        setEnvLoading(false);
        envRequestRef.current = false;
      }
    }
  };

  // 환경 정지
  const stopEnv = async () => {
    const generation = viewGeneration;
    if (!hasCurrentEnvironment || loadedChallengeRequestId !== challengeRequestRef.current
      || !isCurrentView(generation) || !envInstance || envLoading || envRequestRef.current) return;
    const requestId = ++envResponseRef.current;
    let owner: ReturnType<typeof captureApiRequestOwner> | null = null;
    const currentRequest = () => isCurrentView(generation) && envResponseRef.current === requestId
      && (owner === null || isCurrentApiRequestOwner(owner));
    envRequestRef.current = true;
    setEnvLoading(true);
    try {
      owner = captureApiRequestOwner(user?.id ?? "");
      await apiFetch(`/api/env/instances/${envInstance.id}/stop`, { method: "POST", requestOwner: owner });
      if (!currentRequest()) return;
      setEnvInstance(null);
      setTimeRemaining("");
      setExtendCount(null);
      setMaxExtendCount(null);
    } catch (err) {
      if (!currentRequest()) return;
      toast("error", err instanceof ApiError ? err.message : t("loadFailed"));
    } finally {
      if (currentRequest()) {
        setEnvLoading(false);
        envRequestRef.current = false;
      }
    }
  };

  // 시간 연장
  const extendEnv = async () => {
    const generation = viewGeneration;
    if (
      !hasCurrentEnvironment || loadedChallengeRequestId !== challengeRequestRef.current
      || !isCurrentView(generation) || !envInstance
      || envLoading
      || envRequestRef.current
      || extendCount === null
      || maxExtendCount === null
      || extendCount >= maxExtendCount
    ) return;
    const requestId = ++envResponseRef.current;
    let owner: ReturnType<typeof captureApiRequestOwner> | null = null;
    const currentRequest = () => isCurrentView(generation) && envResponseRef.current === requestId
      && (owner === null || isCurrentApiRequestOwner(owner));
    envRequestRef.current = true;
    setEnvLoading(true);
    setEnvError(null);
    try {
      owner = captureApiRequestOwner(user?.id ?? "");
      const res = await apiFetch<EnvExtendResponse>(`/api/env/instances/${envInstance.id}/extend`, { method: "POST", requestOwner: owner });
      if (!currentRequest()) return;
      const nextExtendCount = parseNonNegativeCounter(res.extend_count);
      const nextMaxExtendCount = parseNonNegativeCounter(res.max_extend_count);
      if (!res.expires_at || nextExtendCount === null || nextMaxExtendCount === null) {
        throw new Error("invalid_extension_response");
      }
      setExtendCount(nextExtendCount);
      setMaxExtendCount(nextMaxExtendCount);
      setEnvInstance((prev) =>
        prev ? {
          ...prev,
          expires_at: res.expires_at,
          extend_count: nextExtendCount,
          max_extend_count: nextMaxExtendCount,
        } : null,
      );
      toast("success", t("extendSuccess"));
    } catch (err) {
      if (!currentRequest()) return;
      const message = err instanceof ApiError ? err.message : t("extendFailed");
      setEnvError(message);
      toast("error", message);
    } finally {
      if (currentRequest()) {
        setEnvLoading(false);
        envRequestRef.current = false;
      }
    }
  };

  // 유효하지 않은 ID
  if (isInvalidId) {
    return (
      <div className="mx-auto max-w-4xl">
        <div className="rounded-xl border border-accent-error/30 bg-accent-error/5 p-6 text-center">
          <p className="text-sm text-accent-error">{tc("noData")}</p>
        </div>
      </div>
    );
  }

  if (loading || detailViewRef.current !== viewGeneration) {
    return (
      <div className="mx-auto max-w-4xl space-y-8">
        <div className="animate-pulse space-y-4">
          <div className="h-8 w-48 rounded bg-bg-surface" />
          <div className="h-64 rounded-xl bg-bg-surface" />
        </div>
      </div>
    );
  }

  if (error || !challenge) {
    return (
      <div className="mx-auto max-w-4xl">
        <div className="rounded-xl border border-accent-error/30 bg-accent-error/5 p-6 text-center">
          <p className="text-sm text-accent-error">{error ?? tc("noData")}</p>
          <Button variant="secondary" className="mt-4" onClick={fetchChallenge}>
            {tc("retry")}
          </Button>
        </div>
      </div>
    );
  }

  const hasEnv = challenge.env_type !== "none";
  const environmentReady = challenge.environment_ready !== false;
  const isLocked = challenge.locked === true;

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      {/* 대표 이미지 배너 */}
      {challenge.thumbnail_url && (
        <div className="overflow-hidden rounded-xl">
          <img
            src={challenge.thumbnail_url}
            alt={challenge.name}
            loading="lazy"
            className="h-48 w-full object-cover"
          />
        </div>
      )}

      {/* 헤더 */}
      <div>
        <div className="mb-3 flex items-center gap-3">
          <CategoryBadge category={challenge.category} />
          <span className="text-sm text-text-muted">{challenge.value}{tc("pointsUnit")}</span>
          <span className="text-sm text-text-muted">{t("solveCount", { count: challenge.solves })}</span>
          {isLocked && (
            <span className="rounded-full bg-text-muted/10 px-2.5 py-0.5 text-xs font-semibold text-text-muted">
              {t("locked")}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-3xl font-bold">{challenge.name}</h1>
          <Link
            href={`/learn?view=map&focus_type=challenge&focus_id=${challenge.id}`}
            className="inline-flex items-center gap-2 rounded-lg border border-accent-primary/40 px-3 py-2 text-sm font-medium text-accent-primary transition-colors hover:border-accent-primary hover:bg-accent-primary/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
          >
            <span aria-hidden="true">◎</span>
            {t("viewMap")}
          </Link>
        </div>
      </div>

      {/* 잠금 상태 -- 선행조건 미충족 */}
      {knowledgeExtensionsEnabled() && <LearningPathPanel challengeId={challengeId} refreshKey={challenge.solved ? "solved" : "unsolved"} />}

      {/* 설명 */}
      {!isLocked && (
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <MarkdownRenderer content={challenge.description} />
      </div>
      )}

      {/* 실습 환경 */}
      {!isLocked && hasEnv && !environmentReady && (
        <div className="rounded-xl border border-yellow-500/30 bg-yellow-500/5 p-5" role="status">
          <p className="text-sm font-semibold text-yellow-700 dark:text-yellow-300">
            {t("envVerificationRequired")}
          </p>
          <p className="mt-1 text-xs text-text-muted">{t("envVerificationRequiredDesc")}</p>
        </div>
      )}

      {!isLocked && hasEnv && environmentReady && (
        <EnvInstanceSection
          envInstance={envInstance}
          envLoading={envLoading}
          envError={envError}
          timeRemaining={timeRemaining}
          extendCount={extendCount}
          maxExtendCount={maxExtendCount}
          extensionDisabled={envLoading || extendCount === null || maxExtendCount === null || extendCount >= maxExtendCount}
          environmentReady={environmentReady}
          environmentUnavailableTitle={t("envVerificationRequired")}
          environmentUnavailableDescription={t("envVerificationRequiredDesc")}
          onStart={startEnv}
          onStop={stopEnv}
          onExtend={extendEnv}
          showStopConfirm={showStopConfirm}
          onStopConfirmToggle={setShowStopConfirm}
          getAccessUrl={getAccessUrl}
        />
      )}

      {/* 플래그 제출 */}
      {!isLocked && (
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <h2 className="mb-3 text-lg font-semibold">{t("submitFlag")}</h2>
        <FlagSubmitForm challengeId={challengeId} solved={challenge.solved} onSubmit={handleSubmit} />
      </div>
      )}

      {/* 힌트 */}
      {!isLocked && (
        <HintSection
          hints={challenge.hints}
          unlockedHints={visibleUnlockedHints}
          hintLoading={hintLoading}
          hintConfirmId={hintConfirmId}
          onUnlock={unlockHint}
          onConfirmToggle={setHintConfirmId}
        />
      )}

      {/* 첨부 파일 */}
      {!isLocked && (
        <AttachmentSection files={challenge.files} attachments={challenge.attachments} challengeId={Number(params.id)} />
      )}
    </div>
  );
}
