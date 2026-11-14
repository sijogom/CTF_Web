"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useRouter } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { WizardStepper } from "../WizardStepper";
import { StepBasicInfo } from "./StepBasicInfo";
import { StepFlag } from "./StepFlag";
import { StepFiles } from "./StepFiles";
import { StepEnvironment } from "./StepEnvironment";
import { StepHints } from "./StepHints";
import { StepPreview } from "./StepPreview";
import { Button } from "@/components/ui/Button";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { apiFetch, ApiError, captureApiRequestOwner, isCurrentApiRequestOwner } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import { isRichContentEmpty } from "@/lib/rich-content.mjs";
import { useKnowledgeTagMappingDraft } from "@/lib/use-knowledge-tag-mapping-draft";
import { useUnsavedChangesGuard } from "@/lib/use-unsaved-changes-guard";
import { useImageEditOperation } from "@/lib/use-image-edit-operation";
import type { ImageEditLease } from "@/lib/image-edit-operation.mjs";
import { useAuth } from "@/providers/AuthProvider";
import { useChallengeAttachments } from "@/lib/use-challenge-attachments";
import type { AttachmentSaveResponse } from "@/lib/challenge-attachments.mjs";
import type { ChallengeAttachmentDescriptor } from "@/types/challenge-attachment";

const STEP_KEYS = [
  { i18nKey: "stepBasicInfo", icon: "📝" },
  { i18nKey: "stepFlag",      icon: "🏴" },
  { i18nKey: "stepFiles",     icon: "📎" },
  { i18nKey: "stepEnv",       icon: "🐳" },
  { i18nKey: "stepHints",     icon: "💡" },
  { i18nKey: "stepPreview",   icon: "👁️" },
];

export interface FlagEntry {
  type: "static" | "regex";
  value: string;
  caseSensitive: boolean;
}

export type ChallengeFlagType = "static" | "regex" | "dynamic_instance";

/**
 * `dynamic_instance`는 raw flag가 아니라 인스턴스마다 플랫폼이 주입하는
 * 비밀 없는 전달 정책만 가진다. HMAC 키 재료는 브라우저에 전달하지 않는다.
 */
export interface DynamicFlagPolicyForm {
  enabled: boolean;
  delivery: "file";
  runtimePath: "/flag.txt";
  ttlSeconds: number;
  hmacKeyVersion: string;
}

export function createDefaultDynamicFlagPolicy(): DynamicFlagPolicyForm {
  return {
    enabled: true,
    delivery: "file",
    runtimePath: "/flag.txt",
    ttlSeconds: 3600,
    hmacKeyVersion: "v1",
  };
}

export interface ChallengeFormData {
  title: string;
  category: string;
  difficulty: string;
  description: string;
  tags: string[];
  points: number;
  xp: number;
  flagType: ChallengeFlagType;
  flagValue: string;
  flags: FlagEntry[];
  dynamicFlag: DynamicFlagPolicyForm | null;
  files: string[];
  attachments: ChallengeAttachmentDescriptor[];
  attachmentRevision: number;
  envType: "none" | "dockerfile" | "image";
  dockerfile: string;
  imageName: string;
  containerPort: number;
  hints: { content: string; cost: number }[];
  prerequisiteIds: number[];
  knowledgeTagIds: number[];
  learningPrerequisites: { type: string; target_id: string }[];
  recommendedChallenges: number[];
  recommendedLearning: { type: string; target_id: string }[];
  challengeType: "wargame" | "competition";
  isPublished: boolean;
  thumbnailUrl: string | null;
  environmentRevision: number;
  environmentVerifiedRevision: number | null;
  environmentReady: boolean;
  environmentVerifiedAt: string | null;
  environmentLastCheckedAt: string | null;
  environmentLastFailureCode: string | null;
}

const INITIAL_DATA: ChallengeFormData = {
  title: "",
  category: "web",
  difficulty: "beginner",
  description: "",
  tags: [],
  points: 100,
  xp: 50,
  flagType: "static",
  flagValue: "",
  flags: [],
  dynamicFlag: null,
  files: [],
  attachments: [],
  attachmentRevision: 0,
  envType: "none",
  dockerfile: "",
  imageName: "",
  containerPort: 8080,
  hints: [],
  prerequisiteIds: [],
  knowledgeTagIds: [],
  learningPrerequisites: [],
  recommendedChallenges: [],
  recommendedLearning: [],
  challengeType: "wargame",
  isPublished: false,
  thumbnailUrl: null,
  environmentRevision: 0,
  environmentVerifiedRevision: null,
  environmentReady: true,
  environmentVerifiedAt: null,
  environmentLastCheckedAt: null,
  environmentLastFailureCode: null,
};

interface ChallengeWizardProps {
  mode?: "create" | "edit";
  initialData?: ChallengeFormData;
  challengeId?: number;
}

// 환경 스텝 인덱스 (파일 업로드가 필요한 스텝)
const ENV_STEP_INDEX = 3;

export function ChallengeWizard({
  mode: initialMode = "create",
  initialData,
  challengeId: initialChallengeId,
}: ChallengeWizardProps) {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const tchall = useTranslations("challenge");
  const tm = useTranslations("knowledgeMapping");
  const router = useRouter();
  const { toast } = useToast();
  const { user } = useAuth();
  const [step, setStep] = useState(0);
  const stepRef = useRef(step);
  const [data, setData] = useState<ChallengeFormData>(initialData ?? INITIAL_DATA);
  const dataRef = useRef(data);
  const [currentMode, setCurrentMode] = useState(initialMode);
  const [currentChallengeId, setCurrentChallengeId] = useState(initialChallengeId);
  const challengeIdRef = useRef(initialChallengeId);
  const attachmentFiles = useRef(new Map<string, File>());
  const savedFilesRef = useRef(initialData?.files ?? INITIAL_DATA.files);
  const { operation, busy, uploading: thumbnailUploading, kind } = useImageEditOperation(`challenge:${initialChallengeId ?? "new"}`);
  const autoSaving = kind === "autosave";
  const isSubmitting = kind === "save";
  const mappingController = useKnowledgeTagMappingDraft("challenge", currentChallengeId ? String(currentChallengeId) : null);
  const attachments = useChallengeAttachments({ operation, files: attachmentFiles,
    initial: { challengeId: initialChallengeId, attachments: initialData?.attachments ?? [], revision: initialData?.attachmentRevision ?? 0 },
    onChange(snapshot) {
      const next = { ...dataRef.current, attachments: [...dataRef.current.attachments.filter(item => item.id === null), ...snapshot.attachments] };
      dataRef.current = next; setData(next);
    },
    onCanonical(value) {
      const next = { ...dataRef.current, attachments: value.attachments, attachmentRevision: value.attachment_revision, ...(value.files ? { files: value.files } : {}) };
      dataRef.current = next; setData(next); savedFilesRef.current = [...next.files];
    },
  });
  const steps = STEP_KEYS.map((s) => ({ label: t(s.i18nKey), icon: s.icon }));

  // 폼 수정 여부 판별
  const isDirty = useCallback(() => {
    const init = initialData ?? INITIAL_DATA;
    return (
      mappingController.dirty || attachments.dirty || JSON.stringify(data.files) !== JSON.stringify(init.files) || data.title !== init.title ||
      data.description !== init.description ||
      data.flagValue !== init.flagValue ||
      data.flagType !== init.flagType ||
      data.category !== init.category ||
      data.difficulty !== init.difficulty ||
      data.points !== init.points ||
      data.xp !== init.xp ||
      data.envType !== init.envType ||
      data.dockerfile !== init.dockerfile ||
      data.imageName !== init.imageName ||
      data.containerPort !== init.containerPort ||
      data.challengeType !== init.challengeType ||
      data.isPublished !== init.isPublished ||
      data.thumbnailUrl !== init.thumbnailUrl ||
      data.tags.length !== init.tags.length ||
      data.hints.length !== init.hints.length ||
      data.flags.length !== init.flags.length ||
      JSON.stringify(data.tags) !== JSON.stringify(init.tags) ||
      JSON.stringify(data.hints) !== JSON.stringify(init.hints) ||
      JSON.stringify(data.flags) !== JSON.stringify(init.flags)
      || JSON.stringify(data.dynamicFlag) !== JSON.stringify(init.dynamicFlag)
      || JSON.stringify(data.knowledgeTagIds) !== JSON.stringify(init.knowledgeTagIds)
    );
  }, [data, initialData, mappingController.dirty, attachments.dirty]);

  const leaveGuard = useUnsavedChangesGuard(isDirty(), { busy: busy || mappingController.busy });

  const updateData = (partial: Partial<ChallengeFormData>) => {
    const nextData = (() => {
      const prev = dataRef.current;
      const next = { ...prev, ...partial };
      const environmentInputsChanged = (
        prev.envType !== next.envType
        || prev.dockerfile !== next.dockerfile
        || prev.imageName !== next.imageName
        || prev.containerPort !== next.containerPort
        || prev.flagType !== next.flagType
        || prev.flagValue !== next.flagValue
        || JSON.stringify(prev.flags) !== JSON.stringify(next.flags)
        || JSON.stringify(prev.dynamicFlag) !== JSON.stringify(next.dynamicFlag)
      );
      const includesCanonicalVerification = (
        Object.prototype.hasOwnProperty.call(partial, "environmentRevision")
        || Object.prototype.hasOwnProperty.call(partial, "environmentVerifiedRevision")
        || Object.prototype.hasOwnProperty.call(partial, "environmentReady")
      );

      if (!environmentInputsChanged || includesCanonicalVerification) return next;
      return {
        ...next,
        environmentReady: next.envType === "none",
        environmentVerifiedRevision: null,
        environmentVerifiedAt: null,
        environmentLastFailureCode: next.envType === "none" ? null : "configuration_changed",
      };
    })();
    dataRef.current = nextData;
    setData(nextData);
  };

  const saveDraft = () => {
    let lease;
    try {
      lease = operation.claim("draft");
      if (!lease) return;
      if (!user?.id) return;
      localStorage.setItem("challenge-draft", JSON.stringify({ ownerId: user.id, data: dataRef.current, attachmentDraft: attachments.controller.metadata() }));
      toast("success", tchall("draftSaved"));
    } catch {
      toast("error", tc("errorOccurred"));
    } finally {
      if (lease) operation.release(lease);
    }
  };

  // 마운트 시 저장된 드래프트 복원
  useEffect(() => {
    if (initialMode !== "create" || initialData || initialChallengeId || !user?.id) return;
    const saved = localStorage.getItem("challenge-draft");
    if (!saved) return;
    let cancelled = false;
    try {
      const restoreOwner = captureApiRequestOwner(user.id);
      const stored = JSON.parse(saved);
      if (stored.ownerId !== user.id || !stored.data || typeof stored.data !== "object") return;
      const parsed = stored.data as Partial<ChallengeFormData>;
      if (parsed.title) {
        const apply = () => {
          if (cancelled || !isCurrentApiRequestOwner(restoreOwner)) return;
          const restored = { ...dataRef.current, ...parsed, attachments: dataRef.current.attachments, attachmentRevision: dataRef.current.attachmentRevision };
          dataRef.current = restored; setData(restored); toast("info", tchall("draftRestored"));
          if (localStorage.getItem("challenge-draft") === saved) localStorage.removeItem("challenge-draft");
        };
        if (stored.attachmentDraft) void attachments.controller.restore(stored.attachmentDraft).then(verified => { if (verified) apply(); });
        else apply();
      }
    } catch { /* 파싱 실패 시 무시 */ }
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id]);

  /** 프론트엔드 폼 → 백엔드 스키마 변환 */
  const toBackendPayload = () => {
    const data = dataRef.current;
    const payload = {
      name: data.title,
      category: data.category,
      difficulty: data.difficulty,
      description: data.description,
      value: data.points,
      xp: data.xp,
      flag_type: data.flagType,
      flag_value: data.flagType === "dynamic_instance" ? "" : data.flagValue,
      flags: data.flagType === "dynamic_instance"
        ? []
        : data.flags.map((f) => ({
          type: f.type,
          value: f.value,
          case_sensitive: f.caseSensitive,
        })),
      tags: data.tags,
      env_type: data.envType,
      dockerfile_content: data.dockerfile,
      docker_image: data.imageName,
      container_port: data.containerPort,
      hints: data.hints,
      prerequisite_ids: data.prerequisiteIds,
      learning_prerequisites: data.learningPrerequisites,
      recommended_challenges: data.recommendedChallenges,
      recommended_learning: data.recommendedLearning,
      challenge_type: data.challengeType,
      is_published: data.isPublished,
      thumbnail_url: data.thumbnailUrl,
    };
    if (!challengeIdRef.current || JSON.stringify(data.files) !== JSON.stringify(savedFilesRef.current)) {
      Object.assign(payload, { files: data.files });
    }

    // 신규 문제의 초기 태그만 challenge 생성과 함께 저장한다. 기존 문제의
    // 매핑 원장은 품질 메타데이터와 감사 기록을 함께 다루는 전용 편집기가
    // 소유하므로 일반 PATCH에 오래된 ID 집합을 다시 보내지 않는다.
    if (currentMode === "create" && !challengeIdRef.current) {
      Object.assign(payload, { knowledge_tag_ids: data.knowledgeTagIds });
    } else if (mappingController.dirty && mappingController.payload) {
      Object.assign(payload, { knowledge_tag_mapping: mappingController.payload });
    }

    if (data.flagType !== "dynamic_instance") return payload;

    return {
      ...payload,
      dynamic_flag: data.dynamicFlag
        ? {
          enabled: data.dynamicFlag.enabled,
          delivery: data.dynamicFlag.delivery,
          runtime_path: data.dynamicFlag.runtimePath,
          ttl_seconds: data.dynamicFlag.ttlSeconds,
          hmac_key_version: data.dynamicFlag.hmacKeyVersion,
        }
        : undefined,
    };
  };

  /** 생성 모드에서 환경 스텝 진입 전 자동 저장 (challenge ID 확보) */
  const autoSaveForEnvStep = async (lease: ImageEditLease): Promise<boolean> => {
    if (challengeIdRef.current) return true;
    try {
      const attachmentPayload = await attachments.controller.prepareSave(lease, JSON.stringify(dataRef.current.files) !== JSON.stringify(savedFilesRef.current));
      if (!operation.isCurrent(lease)) return false;
      const res = await apiFetch<AttachmentSaveResponse>("/api/learn/challenges", {
        method: "POST",
        requestOwner: lease.owner,
        body: { ...toBackendPayload(), ...attachmentPayload },
      });
      if (!operation.isCurrent(lease)) return false;
      if (!Number.isInteger(res.id) || res.id < 1) throw new Error("Invalid challenge response");
      attachments.controller.acceptSaved(res, lease);
      updateData({ attachments: res.attachments, attachmentRevision: res.attachment_revision });
      savedFilesRef.current = [...dataRef.current.files];
      challengeIdRef.current = res.id;
      setCurrentChallengeId(res.id);
      setCurrentMode("edit");
      toast("success", tchall("autoSaveSuccess"));
      return true;
    } catch (err) {
      if (operation.isCurrent(lease)) { attachments.controller.saveFailed(err); toast("error", err instanceof ApiError ? err.message : tc("errorOccurred")); }
      return false;
    }
  };

  /** 필수 입력값 검증 */
  const validateBasicInfo = (): boolean => {
    const data = dataRef.current;
    if (!data.title.trim()) {
      toast("error", tchall("validationTitle"));
      return false;
    }
    if (isRichContentEmpty(data.description)) {
      toast("error", tchall("validationDesc"));
      return false;
    }
    return true;
  };

  const validateFlag = (): boolean => {
    const data = dataRef.current;
    if (data.flagType === "dynamic_instance") {
      const policy = data.dynamicFlag;
      if (data.envType === "none") {
        toast("error", tchall("validationDynamicEnvironment"));
        return false;
      }
      if (
        !policy
        || !policy.enabled
        || policy.delivery !== "file"
        || policy.runtimePath !== "/flag.txt"
        || !Number.isInteger(policy.ttlSeconds)
        || policy.ttlSeconds < 60
        || policy.ttlSeconds > 86400
      ) {
        toast("error", tchall("validationDynamicPolicy"));
        return false;
      }
      if (data.flagValue.trim() || data.flags.length > 0) {
        toast("error", tchall("validationDynamicNoStatic"));
        return false;
      }
      return true;
    }

    const hasFlags = data.flags.length > 0 && data.flags.some((f) => f.value.trim());
    const hasSingleFlag = data.flagValue.trim();
    if (!hasFlags && !hasSingleFlag) {
      toast("error", tchall("validationFlag"));
      return false;
    }
    return true;
  };

  /** 스텝 이동 — 환경 스텝 진입 시 자동 저장 */
  const goToStep = async (targetStep: number) => {
    if (mappingController.busy || operation.busy) return;
    let lease;
    try {
      const needsAutoSave = targetStep >= ENV_STEP_INDEX && currentMode === "create" && !challengeIdRef.current;
      lease = operation.claim(needsAutoSave ? "autosave" : "navigate");
      if (!lease) return;
      if (stepRef.current === 0 && targetStep > 0 && !validateBasicInfo()) return;
      if (stepRef.current === 1 && targetStep > 1 && !validateFlag()) return;
      if (needsAutoSave && attachments.blocked) { toast("error", tchall("attachmentPendingSave")); return; }
      if (needsAutoSave && !await autoSaveForEnvStep(lease)) return;
      if (!operation.isCurrent(lease)) return;
      stepRef.current = targetStep;
      setStep(targetStep);
    } catch (err) {
      if (!lease || operation.isCurrent(lease)) toast("error", err instanceof ApiError ? err.message : tc("errorOccurred"));
    } finally {
      if (lease) operation.release(lease);
    }
  };

  const handleSubmit = async () => {
    if (mappingController.busy || operation.busy || !validateBasicInfo() || !validateFlag()) return;
    if (challengeIdRef.current && !mappingController.payload) {
      toast("error", tm("loadRequired"));
      stepRef.current = 0;
      setStep(0);
      return;
    }
    let lease;
    try {
      lease = operation.claim("save");
      if (!lease) return;
      if (attachments.blocked) { toast("error", tchall("attachmentPendingSave")); return; }
      const attachmentPayload = await attachments.controller.prepareSave(lease, JSON.stringify(dataRef.current.files) !== JSON.stringify(savedFilesRef.current));
      if (!operation.isCurrent(lease)) return;
      let saved: AttachmentSaveResponse;
      if (challengeIdRef.current) {
        saved = await apiFetch<AttachmentSaveResponse>(`/api/learn/challenges/${challengeIdRef.current}`, {
          method: "PATCH",
          requestOwner: lease.owner,
          body: { ...toBackendPayload(), ...attachmentPayload },
        });
      } else {
        saved = await apiFetch<AttachmentSaveResponse>("/api/learn/challenges", {
          method: "POST",
          requestOwner: lease.owner,
          body: { ...toBackendPayload(), ...attachmentPayload },
        });
      }
      if (!operation.isCurrent(lease)) return;
      attachments.controller.acceptSaved(saved, lease);
      updateData({ attachments: saved.attachments, attachmentRevision: saved.attachment_revision });
      router.push("/admin/challenges");
    } catch (err) {
      if (lease && operation.isCurrent(lease)) attachments.controller.saveFailed(err);
      if (!lease || operation.isCurrent(lease)) toast("error", err instanceof ApiError ? err.message : tc("errorOccurred"));
    } finally {
      if (lease) operation.release(lease);
    }
  };

  const pageTitle = currentMode === "edit" ? t("chalEditTitle") : t("newChallengeTitle");
  const submitLabel = currentMode === "edit" ? t("chalSaveChanges") : tchall("submitChallenge");
  const legacyAttachments: ChallengeAttachmentDescriptor[] = data.files.map((file, index) =>
    data.attachments.find(item => item.id === null && item.legacy_index === index) ?? {
      id: null, legacy_index: index, name: file.split(/[/\\]/).pop() || file,
      byte_size: null, sha256: null, state: "unavailable", download_path: null,
    });

  const stepComponents = [
    <StepBasicInfo
      key={0}
      data={data}
      onChange={updateData}
      challengeId={currentChallengeId}
      mode={currentMode}
      mappingController={mappingController}
      operation={operation}
      thumbnailUploading={thumbnailUploading}
      disabled={busy || mappingController.busy}
    />,
    <StepFlag key={1} data={data} onChange={updateData} />,
    <StepFiles key={2} attachments={attachments} legacy={legacyAttachments} disabled={busy || mappingController.busy}
      onRemoveLegacy={(index) => {
        const lease = operation.claim("attachment-remove"); if (!lease) return;
        try { updateData({ files: dataRef.current.files.filter((_, i) => i !== index), attachments: dataRef.current.attachments.filter(item => item.legacy_index !== index).map(item => item.legacy_index !== null && item.legacy_index > index ? { ...item, legacy_index: item.legacy_index - 1 } : item) }); }
        finally { operation.release(lease); }
      }} onReload={async () => { await attachments.controller.reload(); }} />,
    <StepEnvironment key={3} data={data} onChange={updateData} challengeId={currentChallengeId} mode={currentMode} />,
    <StepHints key={4} data={data} onChange={updateData} />,
    <StepPreview key={5} data={data} onChange={updateData} onSubmit={handleSubmit} submitLabel={submitLabel} />,
  ];

  return (
    <div className="mx-auto max-w-4xl">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-bold">{pageTitle}</h1>
        <div className="flex gap-2">
          <Button variant="ghost" disabled={busy || mappingController.busy} onClick={() => { if (!operation.busy && !mappingController.busy) leaveGuard.requestDiscard(() => router.back()); }}>
            {tc("cancel")}
          </Button>
          {currentMode === "create" && !currentChallengeId && (
            <Button variant="secondary" disabled={busy} onClick={saveDraft}>
              {t("saveDraft")}
            </Button>
          )}
        </div>
      </div>

      <WizardStepper steps={steps} currentStep={step} onStepClick={goToStep} />

      <div inert={isSubmitting || autoSaving ? true : undefined}>{stepComponents[step]}</div>

      <div className="mt-6 flex justify-between">
        <Button
          variant="secondary"
          onClick={() => goToStep(stepRef.current - 1)}
          disabled={step === 0 || busy || mappingController.busy}
        >
          {t("prev")}
        </Button>
        {step < steps.length - 1 && (
          <Button onClick={() => goToStep(stepRef.current + 1)} disabled={busy || mappingController.busy}>
            {autoSaving ? tchall("autoSaving") : t("nextStep", { step: steps[step + 1].label })}
          </Button>
        )}
      </div>

      <ConfirmModal
        open={leaveGuard.confirmDiscardOpen}
        title={tchall("unsavedChangesTitle")}
        description={tchall("unsavedChangesDesc")}
        confirmLabel={tchall("leaveAnyway")}
        variant="danger"
        onConfirm={() => { if (!operation.busy && !mappingController.busy) leaveGuard.confirmDiscard(); }}
        onCancel={leaveGuard.cancelDiscard}
      />
    </div>
  );
}
