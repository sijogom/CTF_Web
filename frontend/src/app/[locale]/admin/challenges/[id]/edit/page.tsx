"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  ChallengeWizard,
  createDefaultDynamicFlagPolicy,
} from "@/components/admin/challenges/ChallengeWizard";
import type { ChallengeFormData } from "@/components/admin/challenges/ChallengeWizard";
import { apiFetch, captureApiRequestOwner, isCurrentApiRequestOwner } from "@/lib/api-client";
import { useAuth } from "@/providers/AuthProvider";
import { runGuardedNavigation } from "@/lib/use-unsaved-changes-guard";
import type { ChallengeAttachmentDescriptor } from "@/types/challenge-attachment";

interface LoadScope {
  key: string;
  mounted: boolean;
  generation: number;
  controller: AbortController | null;
}
type LoadState = {
  scope: LoadScope;
  generation: number;
} & ({ status: "loading" | "error" } | { status: "ready"; data: ChallengeFormData });

/** 백엔드 응답 → ChallengeFormData 변환 */
function toFormData(raw: Record<string, unknown>): ChallengeFormData {
  const flagType = (raw.flag_type as ChallengeFormData["flagType"]) ?? "static";
  const dynamicFlag = raw.dynamic_flag as Record<string, unknown> | null | undefined;
  return {
    title: (raw.name as string) ?? "",
    category: (raw.category as string) ?? "web",
    difficulty: (raw.difficulty as string) ?? "beginner",
    description: (raw.description as string) ?? "",
    tags: (raw.tags as string[]) ?? [],
    points: (raw.value as number) ?? 100,
    xp: (raw.xp as number) ?? 50,
    flagType,
    flagValue: (raw.flag_value as string) ?? "",
    flags: ((raw.flags as { type: "static" | "regex"; value: string; case_sensitive: boolean }[]) ?? []).map((f) => ({
      type: f.type,
      value: f.value,
      caseSensitive: f.case_sensitive ?? true,
    })),
    dynamicFlag: flagType === "dynamic_instance"
      ? {
        ...createDefaultDynamicFlagPolicy(),
        enabled: (dynamicFlag?.enabled as boolean) ?? true,
        delivery: "file",
        runtimePath: "/flag.txt",
        ttlSeconds: (dynamicFlag?.ttl_seconds as number) ?? 3600,
        hmacKeyVersion: (dynamicFlag?.hmac_key_version as string) ?? "v1",
      }
      : null,
    files: (raw.files as string[]) ?? [],
    attachments: (raw.attachments as ChallengeAttachmentDescriptor[]) ?? [],
    attachmentRevision: Number(raw.attachment_revision ?? 0),
    envType: (raw.env_type as "none" | "dockerfile" | "image") ?? "none",
    dockerfile: (raw.dockerfile_content as string) ?? "",
    imageName: (raw.docker_image as string) ?? "",
    containerPort: (raw.container_port as number) ?? 8080,
    hints: (raw.hints as { content: string; cost: number }[]) ?? [],
    prerequisiteIds: (raw.prerequisite_ids as number[]) ?? [],
    knowledgeTagIds: (raw.knowledge_tag_ids as number[]) ?? [],
    learningPrerequisites: (raw.learning_prerequisites_raw as { type: string; target_id: string }[]) ?? [],
    recommendedChallenges: (raw.recommended_challenges as number[]) ?? [],
    recommendedLearning: (raw.recommended_learning_raw as { type: string; target_id: string }[]) ?? [],
    challengeType: (raw.challenge_type as "wargame" | "competition") ?? "wargame",
    isPublished: (raw.is_published as boolean) ?? false,
    thumbnailUrl: (raw.thumbnail_url as string) ?? null,
    environmentRevision: Number(raw.environment_revision ?? 0),
    environmentVerifiedRevision: raw.environment_verified_revision == null
      ? null
      : Number(raw.environment_verified_revision),
    environmentReady: raw.environment_ready === true,
    environmentVerifiedAt: (raw.environment_verified_at as string | null) ?? null,
    environmentLastCheckedAt: (raw.environment_last_checked_at as string | null) ?? null,
    environmentLastFailureCode: (raw.environment_last_failure_code as string | null) ?? null,
  };
}

export default function EditChallengePage() {
  const params = useParams();
  const challengeId = Number(params.id);
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const { user } = useAuth();
  const userId = user?.id ?? "";
  const validId = Number.isInteger(challengeId) && challengeId > 0;
  const scope = useMemo<LoadScope>(() => ({
    key: JSON.stringify([challengeId, userId]), mounted: false, generation: 0, controller: null,
  }), [challengeId, userId]);
  const [loadState, setLoadState] = useState<LoadState | null>(null);
  const currentLoad = loadState?.scope === scope ? loadState : null;

  useLayoutEffect(() => {
    scope.mounted = true;
    return () => {
      scope.mounted = false;
      scope.generation += 1;
      scope.controller?.abort();
    };
  }, [scope]);

  const loadChallenge = useCallback(async () => {
    if (!scope.mounted || !Number.isInteger(challengeId) || challengeId < 1) return;
    scope.controller?.abort();
    const controller = new AbortController();
    scope.controller = controller;
    const generation = ++scope.generation;
    const isCurrent = () => scope.mounted && scope.generation === generation
      && scope.controller === controller && !controller.signal.aborted;
    setLoadState({ scope, generation, status: "loading" });
    try {
      const requestOwner = captureApiRequestOwner(userId);
      const raw = await apiFetch<Record<string, unknown>>(`/api/learn/challenges/${challengeId}`, {
        signal: controller.signal, requestOwner,
      });
      if (!isCurrent()) return;
      if (!isCurrentApiRequestOwner(requestOwner)) {
        setLoadState({ scope, generation, status: "error" });
        return;
      }
      if (raw.id !== challengeId) throw new Error("Challenge response identity mismatch");
      setLoadState({ scope, generation, status: "ready", data: toFormData(raw) });
    } catch {
      if (isCurrent()) setLoadState({ scope, generation, status: "error" });
    }
  }, [challengeId, userId, scope]);

  const requestLoad = useCallback(() => {
    runGuardedNavigation(() => { void loadChallenge(); });
  }, [loadChallenge]);

  useEffect(() => { requestLoad(); }, [requestLoad]);

  if (!validId || currentLoad?.status === "error") {
    return (
      <div className="py-20 text-center">
        <p role="alert" className="text-status-error">{t("loadStatsFailed")}</p>
        {validId && <button type="button" onClick={requestLoad} className="mt-2 text-accent-primary hover:underline">{tc("retry")}</button>}
      </div>
    );
  }

  if (currentLoad?.status !== "ready") {
    return (
      <div className="flex justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }

  return (
    <ChallengeWizard
      key={`${scope.key}:${currentLoad.generation}`}
      mode="edit"
      initialData={currentLoad.data}
      challengeId={challengeId}
    />
  );
}
