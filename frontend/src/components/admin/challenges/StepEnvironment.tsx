"use client";

import { useState, useRef, useCallback, useEffect } from "react";
import { useTranslations } from "next-intl";
import { apiFetch, apiFormFetch, ApiError } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import type { ChallengeFormData } from "./ChallengeWizard";

interface Props {
  data: ChallengeFormData;
  onChange: (partial: Partial<ChallengeFormData>) => void;
  challengeId?: number;
  mode?: "create" | "edit";
}

interface BuildFileInfo {
  name: string;
  size: number;
}

interface AdminEnvironmentDetails {
  env_type?: ChallengeFormData["envType"];
  dockerfile_content?: string;
  docker_image?: string;
  container_port?: number;
  environment_revision?: number;
  environment_verified_revision?: number | null;
  environment_ready?: boolean;
  environment_verified_at?: string | null;
  environment_last_checked_at?: string | null;
  environment_last_failure_code?: string | null;
}

interface EnvironmentVerificationSummary {
  revision: number;
  verifiedRevision: number | null;
  ready: boolean;
}

function environmentConfigSignature(data: Pick<
  ChallengeFormData,
  "envType" | "dockerfile" | "imageName" | "containerPort" | "flagType" | "flagValue" | "flags" | "dynamicFlag"
>): string {
  return JSON.stringify([
    data.envType,
    data.dockerfile,
    data.imageName,
    data.containerPort,
    data.flagType,
    data.flagValue,
    data.flags,
    data.dynamicFlag,
  ]);
}

const ENV_TYPE_KEYS = [
  { value: "none" as const, labelKey: "envNone", descKey: "envNoneDesc" },
  { value: "dockerfile" as const, labelKey: "envDockerfile", descKey: "envDockerfileDesc" },
  { value: "image" as const, labelKey: "envImageLabel", descKey: "envImageDesc" },
];

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function parseExposePort(dockerfileContent: string): number | null {
  for (const line of dockerfileContent.split("\n")) {
    const match = line.trim().match(/^EXPOSE\s+(\d+)/i);
    if (!match) continue;
    const port = Number(match[1]);
    if (port >= 1 && port <= 65535) return port;
  }
  return null;
}

export function StepEnvironment({ data, onChange, challengeId, mode }: Props) {
  const t = useTranslations("challenge");
  const tc = useTranslations("common");
  const { toast } = useToast();
  const [building, setBuilding] = useState(false);
  const [buildElapsed, setBuildElapsed] = useState(0);
  const [buildLog, setBuildLog] = useState<string | null>(null);
  const [buildFiles, setBuildFiles] = useState<BuildFileInfo[]>([]);
  const [buildFilesError, setBuildFilesError] = useState<string | null>(null);
  const [buildFilesLoading, setBuildFilesLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [deletingFile, setDeletingFile] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [filesLoaded, setFilesLoaded] = useState(false);
  const [verifyingEnvironment, setVerifyingEnvironment] = useState(false);
  const [environmentVerificationState, setEnvironmentVerificationState] = useState<"idle" | "ready" | "failed">(() => {
    if (
      data.envType === "none"
      || (
        data.environmentReady
        && data.environmentVerifiedRevision !== null
        && data.environmentVerifiedRevision === data.environmentRevision
      )
    ) return "ready";
    if (data.environmentLastFailureCode && data.environmentLastFailureCode !== "configuration_changed") {
      return "failed";
    }
    return "idle";
  });
  const [dynamicFlagRequired, setDynamicFlagRequired] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const buildTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const hasAutoBuildAttemptedRef = useRef(false);
  const environmentRequestGenerationRef = useRef(0);
  const environmentConfigSignatureRef = useRef(environmentConfigSignature(data));
  const environmentOperationSequenceRef = useRef(0);
  const activeEnvironmentOperationRef = useRef<{ id: number; challengeId: number } | null>(null);
  const buildFilesRequestRef = useRef(0);
  const mountedRef = useRef(true);
  const challengeIdRef = useRef(challengeId);
  const previousChallengeIdRef = useRef(challengeId);
  challengeIdRef.current = challengeId;

  const isEditWithId = mode === "edit" && !!challengeId;
  const dynamicFlagRequiresEnvironment = data.flagType === "dynamic_instance";

  const beginEnvironmentOperation = useCallback((targetChallengeId: number) => {
    if (activeEnvironmentOperationRef.current !== null) return null;
    const operation = {
      id: environmentOperationSequenceRef.current + 1,
      challengeId: targetChallengeId,
    };
    environmentOperationSequenceRef.current = operation.id;
    activeEnvironmentOperationRef.current = operation;
    return operation;
  }, []);

  const isCurrentTarget = useCallback((targetChallengeId: number) => (
    mountedRef.current && challengeIdRef.current === targetChallengeId
  ), []);

  const isCurrentEnvironmentResult = useCallback((generation: number, targetChallengeId: number) => (
    isCurrentTarget(targetChallengeId)
    && environmentRequestGenerationRef.current === generation
  ), [isCurrentTarget]);

  const endEnvironmentOperation = useCallback((operation: { id: number; challengeId: number }) => {
    const active = activeEnvironmentOperationRef.current;
    if (active?.id !== operation.id || active.challengeId !== operation.challengeId) return false;
    activeEnvironmentOperationRef.current = null;
    return mountedRef.current;
  }, []);

  // 빌드 타이머 정리
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (buildTimerRef.current) clearInterval(buildTimerRef.current);
    };
  }, []);

  // Reusing this step for another challenge must not carry the previous
  // challenge's files, pending indicators, or request ownership into the new
  // target. In-flight results are invalidated and will fail their target guard.
  useEffect(() => {
    if (previousChallengeIdRef.current === challengeId) return;
    previousChallengeIdRef.current = challengeId;
    environmentRequestGenerationRef.current += 1;
    buildFilesRequestRef.current += 1;
    activeEnvironmentOperationRef.current = null;
    environmentConfigSignatureRef.current = environmentConfigSignature(data);
    hasAutoBuildAttemptedRef.current = false;
    if (buildTimerRef.current) {
      clearInterval(buildTimerRef.current);
      buildTimerRef.current = null;
    }
    setBuilding(false);
    setBuildElapsed(0);
    setBuildLog(null);
    setBuildFiles([]);
    setBuildFilesError(null);
    setBuildFilesLoading(false);
    setUploading(false);
    setDeletingFile(null);
    setDragging(false);
    setFilesLoaded(false);
    setVerifyingEnvironment(false);
    setDynamicFlagRequired(false);
    setEnvironmentVerificationState(
      data.envType === "none"
      || (
        data.environmentReady
        && data.environmentVerifiedRevision !== null
        && data.environmentVerifiedRevision === data.environmentRevision
      )
        ? "ready"
        : data.environmentLastFailureCode
            && data.environmentLastFailureCode !== "configuration_changed"
          ? "failed"
          : "idle",
    );
  }, [
    challengeId,
    data,
  ]);

  // 저장 전 로컬 설정이 달라진 순간에도 이전 서버 검증을 현재 설정의 근거로 표시하지 않는다.
  useEffect(() => {
    const signature = environmentConfigSignature(data);
    if (environmentConfigSignatureRef.current === signature) return;

    environmentConfigSignatureRef.current = signature;
    environmentRequestGenerationRef.current += 1;
    setEnvironmentVerificationState(data.envType === "none" ? "ready" : "idle");
    onChange({
      environmentReady: data.envType === "none",
      environmentVerifiedRevision: null,
      environmentVerifiedAt: null,
      environmentLastFailureCode: data.envType === "none" ? null : "configuration_changed",
    });
    // onChange는 부모 렌더마다 새 함수이므로 실제 설정 필드만 이 효과의 기준으로 둔다.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    challengeId,
    data.envType,
    data.imageName,
    data.containerPort,
    data.dockerfile,
    data.flagType,
    data.flagValue,
    data.flags,
    data.dynamicFlag,
  ]);

  const applyEnvironmentDetails = useCallback((raw: AdminEnvironmentDetails): EnvironmentVerificationSummary => {
    const revision = Number(raw.environment_revision ?? 0);
    const verifiedRevision = raw.environment_verified_revision == null
      ? null
      : Number(raw.environment_verified_revision);
    const ready = raw.environment_ready === true
      && verifiedRevision !== null
      && verifiedRevision === revision;
    const canonicalConfig = {
      envType: raw.env_type ?? data.envType,
      dockerfile: raw.dockerfile_content ?? data.dockerfile,
      imageName: raw.docker_image ?? data.imageName,
      containerPort: Number(raw.container_port ?? data.containerPort),
      flagType: data.flagType,
      flagValue: data.flagValue,
      flags: data.flags,
      dynamicFlag: data.dynamicFlag,
    };

    environmentConfigSignatureRef.current = environmentConfigSignature(canonicalConfig);
    onChange({
      envType: canonicalConfig.envType,
      dockerfile: canonicalConfig.dockerfile,
      imageName: canonicalConfig.imageName,
      containerPort: canonicalConfig.containerPort,
      environmentRevision: revision,
      environmentVerifiedRevision: verifiedRevision,
      environmentReady: raw.env_type === "none" || ready,
      environmentVerifiedAt: raw.environment_verified_at ?? null,
      environmentLastCheckedAt: raw.environment_last_checked_at ?? null,
      environmentLastFailureCode: raw.environment_last_failure_code ?? null,
    });
    setEnvironmentVerificationState(
      raw.env_type === "none" || ready
        ? "ready"
        : raw.environment_last_failure_code && raw.environment_last_failure_code !== "configuration_changed"
          ? "failed"
          : "idle",
    );
    return { revision, verifiedRevision, ready: raw.env_type === "none" || ready };
  }, [
    data.containerPort,
    data.dockerfile,
    data.dynamicFlag,
    data.envType,
    data.flagType,
    data.flagValue,
    data.flags,
    data.imageName,
    onChange,
  ]);

  const refreshEnvironmentDetails = useCallback(async (
    generation: number,
    targetChallengeId: number,
  ): Promise<EnvironmentVerificationSummary | null> => {
    const raw = await apiFetch<AdminEnvironmentDetails>(`/api/learn/challenges/${targetChallengeId}`);
    if (!isCurrentEnvironmentResult(generation, targetChallengeId)) return null;
    return applyEnvironmentDetails(raw);
  }, [applyEnvironmentDetails, isCurrentEnvironmentResult]);

  // 편집 모드에서 기존 빌드 파일 목록 로드
  const loadBuildFiles = useCallback(async (targetChallengeId: number) => {
    const requestId = buildFilesRequestRef.current + 1;
    buildFilesRequestRef.current = requestId;
    setBuildFilesLoading(true);
    setBuildFilesError(null);
    try {
      const res = await apiFetch<{ files: BuildFileInfo[] }>(
        `/api/learn/challenges/${targetChallengeId}/build-files`,
      );
      if (
        !isCurrentTarget(targetChallengeId)
        || buildFilesRequestRef.current !== requestId
      ) return false;
      setBuildFiles(res.files);
      return true;
    } catch {
      if (
        isCurrentTarget(targetChallengeId)
        && buildFilesRequestRef.current === requestId
      ) {
        setBuildFilesError(t("buildFilesLoadFailed"));
      }
      return false;
    } finally {
      if (
        isCurrentTarget(targetChallengeId)
        && buildFilesRequestRef.current === requestId
      ) {
        setFilesLoaded(true);
        setBuildFilesLoading(false);
      }
    }
  }, [isCurrentTarget, t]);

  useEffect(() => {
    if (isEditWithId && challengeId && data.envType === "dockerfile" && !filesLoaded) {
      void loadBuildFiles(challengeId);
    }
  }, [challengeId, isEditWithId, data.envType, filesLoaded, loadBuildFiles]);

  // 빌드 전 env 설정을 백엔드에 동기화 (env_type, container_port 등)
  const syncEnvSettings = useCallback(async (
    generation: number,
    targetChallengeId: number,
  ): Promise<EnvironmentVerificationSummary | null> => {
    try {
      await apiFetch(`/api/learn/challenges/${targetChallengeId}`, {
        method: "PATCH",
        body: {
          env_type: data.envType,
          container_port: data.containerPort,
          dockerfile_content: data.dockerfile,
          docker_image: data.imageName,
          // build/verify가 화면과 같은 플래그 모드를 사용하도록 세 모드를 모두
          // 동기화한다. dynamic_instance에서 나갈 때도 새 정적 검증 재료를 보낸다.
          ...(data.flagType === "dynamic_instance" && data.dynamicFlag
            ? {
              flag_type: "dynamic_instance",
              flag_value: "",
              flags: [],
              dynamic_flag: {
                enabled: data.dynamicFlag.enabled,
                delivery: data.dynamicFlag.delivery,
                runtime_path: data.dynamicFlag.runtimePath,
                ttl_seconds: data.dynamicFlag.ttlSeconds,
                hmac_key_version: data.dynamicFlag.hmacKeyVersion,
              },
            }
            : {
              flag_type: data.flagType,
              flag_value: data.flagValue,
              flags: data.flags.map((flag) => ({
                type: flag.type,
                value: flag.value,
                case_sensitive: flag.caseSensitive,
              })),
            }),
        },
      });
      if (!isCurrentEnvironmentResult(generation, targetChallengeId)) return null;
      return await refreshEnvironmentDetails(generation, targetChallengeId);
    } catch {
      if (isCurrentEnvironmentResult(generation, targetChallengeId)) {
        toast("error", t("syncEnvFailed"));
      }
      return null;
    }
  }, [
    data.envType,
    data.containerPort,
    data.dockerfile,
    data.imageName,
    data.flagType,
    data.dynamicFlag,
    data.flagValue,
    data.flags,
    isCurrentEnvironmentResult,
    refreshEnvironmentDetails,
    toast,
    t,
  ]);

  // 공개 또는 대회 배정 전에 실제 컨테이너가 전용 네트워크에서 준비되는지 확인한다.
  // 이미지·포트 변경사항을 먼저 저장해 화면의 임시 값과 다른 환경을 검사하지 않는다.
  const handleVerifyEnvironment = useCallback(async () => {
    if (!challengeId || data.envType === "none") return;
    const targetChallengeId = challengeId;
    const operation = beginEnvironmentOperation(targetChallengeId);
    if (!operation) return;

    const generation = environmentRequestGenerationRef.current + 1;
    environmentRequestGenerationRef.current = generation;
    setVerifyingEnvironment(true);
    setEnvironmentVerificationState("idle");

    try {
      const synced = await syncEnvSettings(generation, targetChallengeId);
      if (!synced) {
        if (isCurrentEnvironmentResult(generation, targetChallengeId)) {
          setEnvironmentVerificationState("failed");
        }
        return;
      }
      const result = await apiFetch<{ status: "ready"; checked_at: string; revision: number }>(
        `/api/env/admin/challenges/${targetChallengeId}/verify-environment`,
        { method: "POST" },
      );
      if (!isCurrentEnvironmentResult(generation, targetChallengeId)) return;
      const canonical = await refreshEnvironmentDetails(generation, targetChallengeId);
      if (!canonical || !isCurrentEnvironmentResult(generation, targetChallengeId)) return;
      if (
        canonical.ready
        && canonical.revision === result.revision
        && canonical.verifiedRevision === result.revision
      ) {
        setEnvironmentVerificationState("ready");
        toast("success", t("verifyEnvironmentSuccess"));
      } else {
        setEnvironmentVerificationState("failed");
        toast("error", t("verifyEnvironmentFailed"));
      }
    } catch {
      if (isCurrentEnvironmentResult(generation, targetChallengeId)) {
        setEnvironmentVerificationState("failed");
        toast("error", t("verifyEnvironmentFailed"));
        try {
          await refreshEnvironmentDetails(generation, targetChallengeId);
        } catch {
          // 검증 실패 원장 조회도 실패하면 현재 실패 상태를 유지한다.
        }
      }
    } finally {
      if (endEnvironmentOperation(operation)) {
        setVerifyingEnvironment(false);
      }
    }
  }, [
    beginEnvironmentOperation,
    challengeId,
    data.envType,
    endEnvironmentOperation,
    isCurrentEnvironmentResult,
    refreshEnvironmentDetails,
    syncEnvSettings,
    t,
    toast,
  ]);

  // 이미지 빌드
  const handleBuild = useCallback(async (): Promise<boolean> => {
    if (!challengeId) return false;
    const targetChallengeId = challengeId;
    const operation = beginEnvironmentOperation(targetChallengeId);
    if (!operation) return false;
    const generation = environmentRequestGenerationRef.current + 1;
    environmentRequestGenerationRef.current = generation;
    setBuilding(true);
    setBuildLog(null);
    setDynamicFlagRequired(false);
    setBuildElapsed(0);
    let operationTimer: ReturnType<typeof setInterval> | null = null;

    try {
      // 빌드 전 env 설정 동기화 — 실패 시 빌드 중단
      const synced = await syncEnvSettings(generation, targetChallengeId);
      if (!synced) {
        if (isCurrentEnvironmentResult(generation, targetChallengeId)) {
          setEnvironmentVerificationState("failed");
        }
        return false;
      }

      // 경과 시간 타이머
      const start = Date.now();
      operationTimer = setInterval(() => {
        if (isCurrentEnvironmentResult(generation, targetChallengeId)) {
          setBuildElapsed(Math.floor((Date.now() - start) / 1000));
        }
      }, 1000);
      buildTimerRef.current = operationTimer;

      const res = await apiFetch<{
        message: string;
        image_name: string;
        log: string;
        verification?: { status: "ready"; checked_at: string; revision: number };
      }>(
        `/api/env/admin/challenges/${targetChallengeId}/build`,
        { method: "POST" },
      );
      if (!isCurrentEnvironmentResult(generation, targetChallengeId)) return false;
      setBuildLog(res.log);
      const canonical = await refreshEnvironmentDetails(generation, targetChallengeId);
      if (!canonical || !isCurrentEnvironmentResult(generation, targetChallengeId)) return false;
      if (
        res.verification?.status === "ready"
        && canonical.ready
        && canonical.revision === res.verification.revision
        && canonical.verifiedRevision === res.verification.revision
      ) {
        setEnvironmentVerificationState("ready");
      } else {
        setEnvironmentVerificationState("failed");
        toast("error", t("verifyEnvironmentFailed"));
        return false;
      }
      toast("success", t("buildSuccess"));
      return true;
    } catch (err) {
      if (!isCurrentEnvironmentResult(generation, targetChallengeId)) return false;
      setEnvironmentVerificationState("failed");
      if (err instanceof ApiError) {
        if (err.log) setBuildLog(err.log);
        setDynamicFlagRequired(err.code === "dynamic_flag_required");
        toast(
          "error",
          err.code === "dynamic_flag_required"
            ? t("dynamicFlagRequired")
            : t("buildFailed"),
        );
      } else {
        toast("error", t("buildFailed"));
      }
      try {
        await refreshEnvironmentDetails(generation, targetChallengeId);
      } catch {
        // 빌드 실패 원장 조회도 실패하면 현재 실패 상태를 유지한다.
      }
      return false;
    } finally {
      if (endEnvironmentOperation(operation)) {
        setBuilding(false);
      }
      if (operationTimer) {
        clearInterval(operationTimer);
      }
      if (buildTimerRef.current === operationTimer) {
        buildTimerRef.current = null;
      }
    }
  }, [
    beginEnvironmentOperation,
    challengeId,
    endEnvironmentOperation,
    isCurrentEnvironmentResult,
    refreshEnvironmentDetails,
    syncEnvSettings,
    t,
    toast,
  ]);

  // 빌드에 불필요한 디렉토리/파일 패턴 — 폴더 업로드 시 자동 제외
  const IGNORED_DIR_NAMES = new Set([".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", ".DS_Store"]);
  const MAX_UPLOAD_FILES = 200;

  /** 폴더 업로드 시 불필요한 경로를 필터링 */
  const shouldIgnoreFile = (path: string): boolean => {
    const segments = path.split("/");
    return segments.some((seg) => IGNORED_DIR_NAMES.has(seg));
  };

  // 파일 업로드 처리 — 폴더 구조 경로를 보존하여 업로드
  const handleUploadFiles = async (fileList: FileList | File[]) => {
    if (!challengeId) return;
    const targetChallengeId = challengeId;
    const rawFiles = Array.from(fileList);
    if (rawFiles.length === 0) return;

    // 불필요한 디렉토리 파일 필터링
    const files = rawFiles.filter((file) => {
      const rp = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
      if (rp && shouldIgnoreFile(rp)) return false;
      if (shouldIgnoreFile(file.name)) return false;
      return true;
    });

    if (files.length === 0) {
      toast("error", t("noValidFiles"));
      return;
    }
    if (files.length > MAX_UPLOAD_FILES) {
      toast("error", t("tooManyFiles", { max: MAX_UPLOAD_FILES }));
      return;
    }

    const operation = beginEnvironmentOperation(targetChallengeId);
    if (!operation) return;
    const generation = environmentRequestGenerationRef.current + 1;
    environmentRequestGenerationRef.current = generation;
    buildFilesRequestRef.current += 1;
    setUploading(true);
    try {
      const formData = new FormData();
      for (const file of files) {
        // webkitRelativePath가 있으면 폴더 구조 경로 사용, 없으면 파일명만
        const relativePath = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
        if (relativePath) {
          // webkitRelativePath는 "폴더명/하위/파일.py" 형태 — 최상위 폴더명 제거
          const parts = relativePath.split("/");
          const pathWithoutRoot = parts.length > 1 ? parts.slice(1).join("/") : relativePath;
          // 경로를 별도 필드로 전송
          formData.append("files", file, pathWithoutRoot);
        } else {
          formData.append("files", file);
        }
      }
      const uploadResult = await apiFormFetch<{
        files: BuildFileInfo[];
        container_port?: number;
        message: string;
      }>(
        `/api/learn/challenges/${targetChallengeId}/build-files`,
        formData,
      );
      if (!isCurrentEnvironmentResult(generation, targetChallengeId)) return;
      const returnedPort = Number(uploadResult.container_port);
      const canonicalPort = Number.isInteger(returnedPort)
        && returnedPort >= 1
        && returnedPort <= 65535
        ? returnedPort
        : data.containerPort;
      // The server owns EXPOSE fallback policy. Apply the returned canonical
      // port only after the upload commits, then pre-align the signature so the
      // corresponding parent render cannot invalidate this request generation.
      environmentConfigSignatureRef.current = environmentConfigSignature({
        ...data,
        containerPort: canonicalPort,
      });
      onChange({
        containerPort: canonicalPort,
        environmentReady: false,
        environmentVerifiedRevision: null,
        environmentVerifiedAt: null,
        environmentLastFailureCode: "configuration_changed",
      });
      setEnvironmentVerificationState("idle");
      hasAutoBuildAttemptedRef.current = false;
      toast("success", t("fileUploadSuccess"));
      await loadBuildFiles(targetChallengeId);
      if (!isCurrentEnvironmentResult(generation, targetChallengeId)) return;
      try {
        await refreshEnvironmentDetails(generation, targetChallengeId);
      } catch {
        if (isCurrentEnvironmentResult(generation, targetChallengeId)) {
          toast("error", t("loadFailed"));
        }
      }
    } catch (err) {
      if (isCurrentEnvironmentResult(generation, targetChallengeId)) {
        const msg = err instanceof ApiError ? err.message : t("buildFailed");
        toast("error", msg);
      }
    } finally {
      if (endEnvironmentOperation(operation)) setUploading(false);
    }
  };

  // 파일 업로드 완료 후 Dockerfile 포함 시 자동 빌드 (1회만)
  useEffect(() => {
    if (hasAutoBuildAttemptedRef.current) return;
    if (
      !uploading
      && !deletingFile
      && !buildFilesLoading
      && !buildFilesError
      && filesLoaded
      && buildFiles.length > 0
      && !building
      && !verifyingEnvironment
      && !data.imageName
    ) {
      const hasDF = buildFiles.some((f) => f.name === "Dockerfile");
      if (hasDF) {
        // 같은 파일 집합의 자동 빌드는 한 번만 시도한다. 실패 뒤에는 버튼으로
        // 명시적으로 재시도할 수 있고, 새 업로드가 성공하면 이 표식을 초기화한다.
        hasAutoBuildAttemptedRef.current = true;
        void handleBuild();
      }
    }
  }, [buildFiles, buildFilesError, buildFilesLoading, building, data.imageName, deletingFile, handleBuild, filesLoaded, uploading, verifyingEnvironment]);

  // 파일 삭제
  const handleDeleteFile = async (filename: string) => {
    if (!challengeId) return;
    const targetChallengeId = challengeId;
    const operation = beginEnvironmentOperation(targetChallengeId);
    if (!operation) return;
    const generation = environmentRequestGenerationRef.current + 1;
    environmentRequestGenerationRef.current = generation;
    buildFilesRequestRef.current += 1;
    setDeletingFile(filename);
    try {
      await apiFetch(`/api/learn/challenges/${targetChallengeId}/build-files/${encodeURIComponent(filename)}`, {
        method: "DELETE",
      });
      if (!isCurrentEnvironmentResult(generation, targetChallengeId)) return;
      setEnvironmentVerificationState("idle");
      onChange({
        environmentReady: false,
        environmentVerifiedRevision: null,
        environmentVerifiedAt: null,
        environmentLastFailureCode: "configuration_changed",
      });
      toast("success", t("fileDeleteSuccess"));
      setBuildFiles((prev) => prev.filter((f) => f.name !== filename));
      try {
        await refreshEnvironmentDetails(generation, targetChallengeId);
      } catch {
        if (isCurrentEnvironmentResult(generation, targetChallengeId)) {
          toast("error", t("loadFailed"));
        }
      }
    } catch (err) {
      if (isCurrentEnvironmentResult(generation, targetChallengeId)) {
        const msg = err instanceof ApiError ? err.message : t("buildFailed");
        toast("error", msg);
      }
    } finally {
      if (endEnvironmentOperation(operation)) setDeletingFile(null);
    }
  };

  // 드래그 앤 드롭
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(true);
  };
  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
  };
  // 재귀적으로 디렉토리 엔트리에서 파일을 수집
  const readEntryRecursive = (entry: FileSystemEntry, path: string): Promise<File[]> => {
    return new Promise((resolve) => {
      // 불필요한 디렉토리 건너뛰기
      if (entry.isDirectory && IGNORED_DIR_NAMES.has(entry.name)) {
        resolve([]);
        return;
      }
      if (entry.isFile) {
        (entry as FileSystemFileEntry).file((file) => {
          // 경로가 있으면 File 객체에 상대 경로를 부여하기 위해 새 File 생성
          const namedFile = path
            ? new File([file], path, { type: file.type, lastModified: file.lastModified })
            : file;
          resolve([namedFile]);
        }, () => resolve([]));
      } else if (entry.isDirectory) {
        const dirReader = (entry as FileSystemDirectoryEntry).createReader();
        // readEntries()는 부분 결과를 반환할 수 있으므로 빈 배열이 올 때까지 반복
        const readAllEntries = (): Promise<FileSystemEntry[]> => {
          return new Promise((res) => {
            const results: FileSystemEntry[] = [];
            const readBatch = () => {
              dirReader.readEntries((entries) => {
                if (entries.length === 0) { res(results); }
                else { results.push(...entries); readBatch(); }
              }, () => res(results));
            };
            readBatch();
          });
        };
        readAllEntries().then(async (entries) => {
          const allFiles: File[] = [];
          for (const child of entries) {
            const childPath = path ? `${path}/${child.name}` : child.name;
            const childFiles = await readEntryRecursive(child, childPath);
            allFiles.push(...childFiles);
          }
          resolve(allFiles);
        });
      } else {
        resolve([]);
      }
    });
  };

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);

    // DataTransferItemList에서 FileSystemEntry를 얻어 디렉토리 탐색
    const items = e.dataTransfer.items;
    if (items && items.length > 0) {
      const allFiles: File[] = [];
      for (let i = 0; i < items.length; i++) {
        const entry = items[i].webkitGetAsEntry?.();
        if (entry) {
          const files = await readEntryRecursive(entry, entry.isDirectory ? "" : "");
          allFiles.push(...files);
        }
      }
      if (allFiles.length > 0) {
        handleUploadFiles(allFiles);
        return;
      }
    }

    // 폴백: 일반 파일 드래그
    if (e.dataTransfer.files.length > 0) {
      handleUploadFiles(e.dataTransfer.files);
    }
  };

  const hasDockerfile = buildFiles.some((f) => f.name === "Dockerfile");
  const hasFlagFile = buildFiles.some((f) => f.name.split("/").at(-1) === "flag.txt");
  const canBuild = isEditWithId && data.envType === "dockerfile" && (hasDockerfile || data.dockerfile);
  const canVerifyEnvironment = isEditWithId && data.envType !== "none" && Boolean(data.imageName);

  return (
    <div className="space-y-5">
      <h2 className="text-lg font-semibold">{t("practiceEnvTitle")}</h2>
      <p className="text-sm text-text-secondary">
        {t("envDockerDesc")}
      </p>

      {/* 환경 타입 선택 */}
      <div className="grid grid-cols-3 gap-3">
        {ENV_TYPE_KEYS.map((item) => (
          <button
            key={item.value}
            type="button"
            disabled={
              uploading
              || deletingFile !== null
              || building
              || verifyingEnvironment
              || (dynamicFlagRequiresEnvironment && item.value === "none")
            }
            onClick={() => onChange({ envType: item.value })}
            className={`rounded-lg border p-4 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
              data.envType === item.value
                ? "border-accent-primary bg-accent-primary/10"
                : "border-border hover:border-text-muted"
            }`}
          >
            <p className="text-sm font-medium">{t(item.labelKey)}</p>
            <p className="mt-1 text-xs text-text-muted">{t(item.descKey)}</p>
          </button>
        ))}
      </div>

      {dynamicFlagRequiresEnvironment && (
        <p className="rounded-md border border-accent-primary/30 bg-accent-primary/5 px-3 py-2 text-xs leading-5 text-text-secondary">
          {t("dynamicFlagEnvironmentRequired")}
        </p>
      )}

      {/* Dockerfile 모드 */}
      {data.envType === "dockerfile" && (
        <div className="space-y-4">
          {/* 멀티파일 업로드 영역 — 편집 모드에서만 */}
          {isEditWithId ? (
            <div className="space-y-3">
              <h3 className="text-sm font-semibold">{t("buildContextFiles")}</h3>

              {/* 드래그 앤 드롭 영역 */}
              <div
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                className={`rounded-lg border-2 border-dashed p-6 text-center transition-colors ${
                  dragging
                    ? "border-accent-primary bg-accent-primary/5"
                    : "border-border hover:border-text-muted"
                }`}
              >
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  disabled={uploading || deletingFile !== null || building || verifyingEnvironment}
                  className="hidden"
                  onChange={(e) => {
                    if (e.target.files) handleUploadFiles(e.target.files);
                    e.target.value = "";
                  }}
                />
                {/* eslint-disable-next-line @typescript-eslint/ban-ts-comment */}
                {/* @ts-ignore — webkitdirectory is a non-standard attribute */}
                <input
                  ref={folderInputRef}
                  type="file"
                  // @ts-expect-error webkitdirectory is non-standard but widely supported
                  webkitdirectory=""
                  directory=""
                  multiple
                  disabled={uploading || deletingFile !== null || building || verifyingEnvironment}
                  className="hidden"
                  onChange={(e) => {
                    if (e.target.files) handleUploadFiles(e.target.files);
                    e.target.value = "";
                  }}
                />
                <svg className="mx-auto mb-2 h-8 w-8 text-text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 16.5V9.75m0 0l3 3m-3-3l-3 3M6.75 19.5a4.5 4.5 0 01-1.41-8.775 5.25 5.25 0 0110.233-2.33 3 3 0 013.758 3.848A3.752 3.752 0 0118 19.5H6.75z" />
                </svg>
                <p className="text-sm text-text-muted">
                  {t("buildFileUploadHint")}
                </p>
                <div className="mt-3 flex items-center justify-center gap-3">
                  <button
                    type="button"
                    onClick={() => fileInputRef.current?.click()}
                    disabled={uploading || deletingFile !== null || building || verifyingEnvironment}
                    className="rounded-lg border border-border bg-bg-surface px-3 py-1.5 text-xs font-medium text-text-secondary transition-colors hover:bg-bg-elevated hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    📄 {t("selectFiles")}
                  </button>
                  <button
                    type="button"
                    onClick={() => folderInputRef.current?.click()}
                    disabled={uploading || deletingFile !== null || building || verifyingEnvironment}
                    className="rounded-lg border border-border bg-bg-surface px-3 py-1.5 text-xs font-medium text-text-secondary transition-colors hover:bg-bg-elevated hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    📁 {t("selectFolder")}
                  </button>
                </div>
                {uploading && (
                  <div className="mt-2 flex items-center justify-center gap-2 text-xs text-accent-primary">
                    <svg className="h-4 w-4 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                    {t("uploading")}
                  </div>
                )}
              </div>

              {/* 업로드된 파일 목록 */}
              {buildFilesLoading ? (
                <p className="text-sm text-text-muted" role="status">{tc("loading")}</p>
              ) : buildFilesError ? (
                <div className="rounded-lg border border-accent-error/30 bg-accent-error/10 p-3" role="alert">
                  <p className="text-sm text-accent-error">{buildFilesError}</p>
                  <button
                    type="button"
                    onClick={() => { if (challengeId) void loadBuildFiles(challengeId); }}
                    disabled={buildFilesLoading}
                    className="mt-2 text-xs font-medium text-accent-primary underline disabled:opacity-50"
                  >
                    {tc("retry")}
                  </button>
                </div>
              ) : buildFiles.length > 0 ? (
                <div className="rounded-lg border border-border">
                  <div className="border-b border-border px-3 py-2">
                    <p className="text-xs font-medium text-text-muted">{t("uploadedFiles")} ({buildFiles.length})</p>
                  </div>
                  <ul className="divide-y divide-border">
                    {buildFiles.map((file) => (
                      <li key={file.name} className="flex items-center justify-between px-3 py-2">
                        <div className="flex items-center gap-2 overflow-hidden">
                          <span className="flex-shrink-0 text-base">
                            {file.name === "Dockerfile" || file.name.startsWith("Dockerfile.") ? "\uD83D\uDC33" : "\uD83D\uDCC4"}
                          </span>
                          <span className="truncate font-mono text-sm">{file.name}</span>
                          <span className="flex-shrink-0 text-xs text-text-muted">
                            ({formatFileSize(file.size)})
                          </span>
                        </div>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDeleteFile(file.name);
                          }}
                          disabled={uploading || deletingFile !== null || building || verifyingEnvironment}
                          className="ml-2 flex-shrink-0 rounded p-1 text-text-muted transition-colors hover:bg-red-500/10 hover:text-red-500 disabled:cursor-not-allowed disabled:opacity-50"
                          aria-label={t("deleteFile", { name: file.name })}
                        >
                          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                          </svg>
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : filesLoaded ? (
                <p className="text-sm text-text-muted">{t("noFilesUploaded")}</p>
              ) : null}

              {/* Dockerfile 포함 여부 경고 */}
              {filesLoaded && buildFiles.length > 0 && !hasDockerfile && (
                <p className="text-xs text-yellow-500">{t("dockerfileRequired")}</p>
              )}
              {filesLoaded && hasFlagFile && data.flagType !== "dynamic_instance" && (
                <p className="rounded-md border border-yellow-500/40 bg-yellow-500/5 px-3 py-2 text-xs leading-5 text-yellow-700 dark:text-yellow-300" role="status">
                  {t("flagFilePlaceholderWarning")}
                </p>
              )}
            </div>
          ) : (
            /* 생성 모드: 먼저 저장 안내 */
            <div className="rounded-lg border border-border bg-bg-secondary/50 p-4">
              <p className="text-sm text-text-muted">{t("saveFirstToUpload")}</p>
            </div>
          )}

          {/* 레거시: Dockerfile 텍스트 입력 (빌드 파일이 없을 때 폴백) */}
          {(isEditWithId && buildFiles.length === 0 && !building) || !isEditWithId ? (
            <div>
              <label htmlFor="dockerfile" className="mb-1.5 block text-sm font-medium">
                Dockerfile ({t("envDockerfileDesc")})
              </label>
              <textarea
                id="dockerfile"
                value={data.dockerfile}
                disabled={uploading || deletingFile !== null || building || verifyingEnvironment}
                onChange={(e) => {
                  const val = e.target.value;
                  const updates: Partial<ChallengeFormData> = { dockerfile: val };
                  const port = parseExposePort(val);
                  if (port) updates.containerPort = port;
                  onChange(updates);
                }}
                placeholder={`FROM ubuntu:22.04\nRUN apt-get update && apt-get install -y ...\nCOPY ./challenge /app\nEXPOSE 8080\nCMD ["/app/start.sh"]`}
                className="min-h-[200px] w-full resize-y rounded-lg border border-border bg-bg-primary p-4 font-mono text-sm focus:border-accent-primary focus:outline-none"
              />
            </div>
          ) : null}

          {/* 현재 이미지 상태 */}
          {isEditWithId && (
            <div className="rounded-lg border border-border bg-bg-primary p-3">
              <p className="text-xs text-text-muted">
                {t("currentImage")}:{" "}
                <span className="font-mono font-medium text-text-primary">
                  {data.imageName || t("noImageBuilt")}
                </span>
              </p>
            </div>
          )}

          {/* 빌드 상태 + 버튼 */}
          {canBuild && (
            <div className="space-y-2">
              {!building && (
                <button
                  type="button"
                  onClick={handleBuild}
                  disabled={building || verifyingEnvironment || uploading || deletingFile !== null}
                  className="flex items-center gap-2 rounded-lg bg-accent-primary px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-accent-primary/90 disabled:opacity-50"
                >
                  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
                  </svg>
                  {data.imageName ? t("rebuildImage") : t("buildImage")}
                </button>
              )}

              {building && (
                <div className="flex items-center gap-3 rounded-lg border border-accent-primary/30 bg-accent-primary/5 px-4 py-3">
                  <svg className="h-5 w-5 animate-spin text-accent-primary" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  <div>
                    <p className="text-sm font-medium text-accent-primary">{t("buildingImage")}</p>
                    <p className="text-xs text-text-muted">
                      {t("buildElapsed", { seconds: buildElapsed })}
                    </p>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* 빌드 로그 */}
          {buildLog && (
            <div className="rounded-lg border border-border bg-gray-900 p-4">
              <p className="mb-2 text-xs font-medium text-gray-400">{t("buildLog")}</p>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap font-mono text-xs leading-relaxed text-gray-200">
                {buildLog}
              </pre>
            </div>
          )}
          {dynamicFlagRequired && (
            <div className="rounded-lg border border-yellow-500/40 bg-yellow-500/5 p-4" role="alert">
              <p className="text-sm font-medium text-yellow-700 dark:text-yellow-300">
                {t("dynamicFlagRequired")}
              </p>
              <p className="mt-1 text-xs leading-5 text-text-secondary">
                {t("dynamicFlagSwitchHint")}
              </p>
              <button
                type="button"
                onClick={() => {
                  onChange({
                    flagType: "dynamic_instance",
                    flagValue: "",
                    flags: [],
                    dynamicFlag: {
                      enabled: true,
                      delivery: "file",
                      runtimePath: "/flag.txt",
                      ttlSeconds: 3600,
                      hmacKeyVersion: "v1",
                    },
                  });
                  setDynamicFlagRequired(false);
                }}
                className="mt-3 rounded-lg border border-yellow-600 px-3 py-2 text-sm font-medium text-yellow-700 transition-colors hover:bg-yellow-500/10 dark:border-yellow-300 dark:text-yellow-300"
              >
                {t("switchToDynamicFlag")}
              </button>
            </div>
          )}
        </div>
      )}

      {/* 컨테이너 포트 입력 (dockerfile/image 모드) */}
      {data.envType !== "none" && (
        <div>
          <label htmlFor="container-port" className="mb-1.5 block text-sm font-medium">
            {t("containerPortLabel")}
          </label>
          <input
            id="container-port"
            type="number"
            min={1}
            max={65535}
            value={data.containerPort}
            disabled={uploading || deletingFile !== null || building || verifyingEnvironment}
            onChange={(e) => {
              const val = Number(e.target.value);
              onChange({ containerPort: (val >= 1 && val <= 65535) ? val : 8080 });
            }}
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 font-mono text-sm focus:border-accent-primary focus:outline-none"
          />
          <p className="mt-1 text-xs text-text-muted">{t("containerPortHint")}</p>
          <p className="mt-2 rounded-md border border-blue-500/30 bg-blue-500/5 px-3 py-2 text-xs leading-5 text-text-secondary">
            {t("containerRuntimeContract")}
          </p>
        </div>
      )}

      {/* 이미지 이름 입력 */}
      {data.envType === "image" && (
        <div>
          <label htmlFor="image-name" className="mb-1.5 block text-sm font-medium">
            {t("dockerImageLabel")}
          </label>
          <input
            id="image-name"
            value={data.imageName}
            disabled={uploading || deletingFile !== null || building || verifyingEnvironment}
            onChange={(e) => onChange({ imageName: e.target.value })}
            placeholder="registry.example.com/challenge:latest"
            className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 font-mono text-sm focus:border-accent-primary focus:outline-none"
          />
        </div>
      )}

      {/* 사전 환경 검증 — 실제 사용자 인스턴스와 동일한 전용 네트워크 계약으로 점검 */}
      {isEditWithId && data.envType !== "none" && (
        <div className="rounded-lg border border-border bg-bg-secondary/40 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-sm font-semibold">{t("verifyEnvironment")}</p>
              <p className="mt-1 text-xs text-text-muted">{t("verifyEnvironmentHint")}</p>
            </div>
            <button
              type="button"
              onClick={handleVerifyEnvironment}
              disabled={!canVerifyEnvironment || verifyingEnvironment || building || uploading || deletingFile !== null}
              className="flex items-center gap-2 rounded-lg border border-accent-primary px-3 py-2 text-sm font-medium text-accent-primary transition-colors hover:bg-accent-primary/10 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {verifyingEnvironment ? (
                <svg className="h-4 w-4 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" aria-hidden="true">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
              ) : (
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m5-2a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              )}
              {verifyingEnvironment ? t("verifyingEnvironment") : t("verifyEnvironment")}
            </button>
          </div>
          {!canVerifyEnvironment && (
            <p className="mt-3 text-xs text-yellow-600 dark:text-yellow-400">{t("verifyEnvironmentImageRequired")}</p>
          )}
          <p className="mt-3 text-xs text-text-muted">
            {t("environmentRevisionStatus", {
              current: data.environmentRevision,
              verified: data.environmentVerifiedRevision ?? t("notSet"),
            })}
          </p>
          {environmentVerificationState === "ready" && (
            <p className="mt-3 text-xs text-green-600 dark:text-green-400">{t("verifyEnvironmentSuccess")}</p>
          )}
          {environmentVerificationState === "failed" && (
            <p className="mt-3 text-xs text-red-600 dark:text-red-400">{t("verifyEnvironmentFailed")}</p>
          )}
        </div>
      )}
    </div>
  );
}
