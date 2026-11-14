"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { ArrowRight } from "lucide-react";
import { Link } from "@/i18n/navigation";
import { ApiError, apiFetch } from "@/lib/api-client";
import { useAuth } from "@/providers/AuthProvider";
import { hasLearningPathContent, learningPathAction } from "@/lib/learning-path-presentation.mjs";
import type {
  LearningPathAccessState,
  LearningPathContentType,
  LearningPathHelpfulContent,
  LearningPathProjection,
  LearningPathStep,
  LearningPathStepStatus,
} from "@/types/knowledge";

type LearningPathPanelProps = {
  /** 공개 워게임 상세를 성공적으로 읽은 뒤에만 이 값을 전달한다. */
  challengeId: number;
  refreshKey?: string | number;
};

type PanelState =
  | { kind: "loading" }
  | { kind: "ready"; projection: LearningPathProjection }
  | { kind: "unauthenticated" }
  | { kind: "unavailable" };

type LearningPathDisplayItem = LearningPathStep | LearningPathHelpfulContent;

const CONTENT_TYPES: readonly LearningPathContentType[] = [
  "challenge",
  "course",
  "lesson",
  "knowledge_tag",
  "hint",
];

// The map is a public learning-content canvas. Hints are shown in the
// learning path, but operational/private surfaces must never become map
// centers through a generated link.
const MAP_CONTENT_TYPES: readonly LearningPathContentType[] = [
  "challenge",
  "course",
  "lesson",
  "knowledge_tag",
];

const STEP_STATUSES: readonly LearningPathStepStatus[] = [
  "completed",
  "in_progress",
  "not_started",
  "unavailable",
  "unknown",
];

const ACCESS_STATES: readonly LearningPathAccessState[] = [
  "available",
  "locked",
  "sign_in_required",
];

const STATUS_MARK: Record<LearningPathStepStatus, string> = {
  completed: "✓",
  in_progress: "◐",
  not_started: "○",
  unavailable: "−",
  unknown: "?",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isInternalHref(value: unknown): value is string {
  return typeof value === "string"
    && value.startsWith("/")
    && !value.startsWith("//")
    && !value.includes("\\");
}

function isContentType(value: unknown): value is LearningPathContentType {
  return typeof value === "string" && CONTENT_TYPES.includes(value as LearningPathContentType);
}

function isMapContentType(value: unknown): value is LearningPathContentType {
  return typeof value === "string" && MAP_CONTENT_TYPES.includes(value as LearningPathContentType);
}

function isStepStatus(value: unknown): value is LearningPathStepStatus {
  return typeof value === "string" && STEP_STATUSES.includes(value as LearningPathStepStatus);
}

function isAccessState(value: unknown): value is LearningPathAccessState {
  return typeof value === "string" && ACCESS_STATES.includes(value as LearningPathAccessState);
}

function readOptionalNonNegativeInteger(value: unknown): number | null | undefined {
  if (value === null || value === undefined) return value;
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : undefined;
}

function readPathStep(value: unknown): LearningPathStep | null {
  if (!isRecord(value)
    || (value.type !== "challenge" && value.type !== "course" && value.type !== "lesson")
    || typeof value.id !== "string"
    || typeof value.name !== "string"
    || !isStepStatus(value.status)
    || !isInternalHref(value.href)
    || !isRecord(value.graph_focus)
    || !isContentType(value.graph_focus.focus_type)
    || typeof value.graph_focus.focus_id !== "string") {
    return null;
  }

  const completedUnits = readOptionalNonNegativeInteger(value.completed_units);
  const totalUnits = readOptionalNonNegativeInteger(value.total_units);

  return {
    type: value.type,
    id: value.id,
    name: value.name,
    status: value.status,
    href: value.href,
    graph_focus: {
      focus_type: value.graph_focus.focus_type,
      focus_id: value.graph_focus.focus_id,
    },
    completed_units: completedUnits ?? null,
    total_units: totalUnits ?? null,
    course_id: typeof value.course_id === "string" ? value.course_id : null,
  };
}

function readHelpfulContent(value: unknown): LearningPathHelpfulContent | null {
  if (!isRecord(value)
    || !isContentType(value.type)
    || typeof value.id !== "string"
    || typeof value.name !== "string"
    || !isStepStatus(value.status)
    || !isInternalHref(value.href)
    || !isRecord(value.graph_focus)
    || !isContentType(value.graph_focus.focus_type)
    || typeof value.graph_focus.focus_id !== "string"
    || (value.relation_type !== "prepares_for" && value.relation_type !== "recommends" && value.relation_type !== "helps")
    || typeof value.strength !== "number"
    || !Number.isFinite(value.strength)
    || typeof value.confidence !== "number"
    || !Number.isFinite(value.confidence)
    || typeof value.origin !== "string") {
    return null;
  }

  return {
    type: value.type,
    id: value.id,
    name: value.name,
    status: value.status,
    href: value.href,
    graph_focus: {
      focus_type: value.graph_focus.focus_type,
      focus_id: value.graph_focus.focus_id,
    },
    course_id: typeof value.course_id === "string" ? value.course_id : null,
    relation_type: value.relation_type,
    strength: value.strength,
    confidence: value.confidence,
    evidence_summary: typeof value.evidence_summary === "string" ? value.evidence_summary : null,
    origin: value.origin,
  };
}

function readPathSteps(value: unknown): LearningPathStep[] {
  return Array.isArray(value)
    ? value.map(readPathStep).filter((item): item is LearningPathStep => item !== null)
    : [];
}

function readHelpfulItems(value: unknown): LearningPathHelpfulContent[] {
  return Array.isArray(value)
    ? value.map(readHelpfulContent).filter((item): item is LearningPathHelpfulContent => item !== null)
    : [];
}

function readProjection(value: unknown): LearningPathProjection | null {
  if (!isRecord(value)
    || !isRecord(value.target)
    || value.target.type !== "challenge"
    || typeof value.target.id !== "string"
    || typeof value.target.name !== "string"
    || !isInternalHref(value.target.href)
    || !isAccessState(value.access_state)) {
    return null;
  }

  const nextAction = isRecord(value.next_action)
    && typeof value.next_action.kind === "string"
    && isInternalHref(value.next_action.href)
    ? { kind: value.next_action.kind, href: value.next_action.href }
    : null;

  return {
    target: {
      type: "challenge",
      id: value.target.id,
      name: value.target.name,
      href: value.target.href,
    },
    access_state: value.access_state,
    reason_code: typeof value.reason_code === "string" ? value.reason_code : null,
    required_steps: readPathSteps(value.required_steps),
    recommended_steps: readPathSteps(value.recommended_steps),
    helpful_content: readHelpfulItems(value.helpful_content),
    next_action: nextAction,
    map_focus_href: isInternalHref(value.map_focus_href) ? value.map_focus_href : null,
  };
}

function hasProgress(step: LearningPathDisplayItem): step is LearningPathDisplayItem & {
  completed_units: number;
  total_units: number;
} {
  return "completed_units" in step
    && "total_units" in step
    && typeof step.completed_units === "number"
    && typeof step.total_units === "number"
    && step.total_units > 0;
}

function contentTypeKey(type: LearningPathContentType): "learningPathTypeChallenge" | "learningPathTypeCourse" | "learningPathTypeLesson" | "learningPathTypeKnowledgeTag" | "learningPathTypeHint" {
  switch (type) {
    case "challenge":
      return "learningPathTypeChallenge";
    case "course":
      return "learningPathTypeCourse";
    case "lesson":
      return "learningPathTypeLesson";
    case "knowledge_tag":
      return "learningPathTypeKnowledgeTag";
    case "hint":
      return "learningPathTypeHint";
  }
}

function statusKey(status: LearningPathStepStatus): "learningPathStatusCompleted" | "learningPathStatusInProgress" | "learningPathStatusNotStarted" | "learningPathStatusUnavailable" | "learningPathStatusUnknown" {
  switch (status) {
    case "completed":
      return "learningPathStatusCompleted";
    case "in_progress":
      return "learningPathStatusInProgress";
    case "not_started":
      return "learningPathStatusNotStarted";
    case "unavailable":
      return "learningPathStatusUnavailable";
    case "unknown":
      return "learningPathStatusUnknown";
  }
}

function accessStateMessageKey(accessState: LearningPathAccessState): "learningPathLocked" | "learningPathAvailable" | "learningPathSignInRequired" {
  switch (accessState) {
    case "locked":
      return "learningPathLocked";
    case "available":
      return "learningPathAvailable";
    case "sign_in_required":
      return "learningPathSignInRequired";
  }
}

function makeMapFocusHref(focusType: LearningPathContentType, focusId: string): string | null {
  if (!isMapContentType(focusType)) return null;
  const params = new URLSearchParams({
    view: "map",
    focus_type: focusType,
    focus_id: focusId,
  });
  return `/learn?${params.toString()}`;
}

function normalizeMapHref(href: string | null | undefined): string | null {
  if (!href || !isInternalHref(href)) return null;

  const parsed = new URL(href, "https://ctf-web.local");
  if (!isMapContentType(parsed.searchParams.get("focus_type"))) return null;
  if (parsed.pathname === "/learn/knowledge-map") {
    parsed.pathname = "/learn";
    parsed.searchParams.set("view", "map");
  }
  return `${parsed.pathname}${parsed.search}`;
}

type StepListProps = {
  heading: string;
  items: LearningPathDisplayItem[];
};

function StepList({ heading, items }: StepListProps) {
  const t = useTranslations("wargame");
  const tm = useTranslations("knowledgeMap");

  if (items.length === 0) return null;

  return (
    <section aria-label={heading}>
      <h3 className="text-sm font-semibold text-text-primary">{heading}</h3>
      <ul className="mt-2 space-y-2">
        {items.map((item) => {
          const href = isInternalHref(item.href) ? item.href : null;
          const mapHref = makeMapFocusHref(item.graph_focus.focus_type, item.graph_focus.focus_id);

          return (
            <li
              key={`${item.type}:${item.id}`}
              className="flex flex-col gap-3 rounded-lg border border-border bg-bg-primary p-3 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="flex min-w-0 items-start gap-3">
                <span
                  className="mt-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded border border-border bg-bg-surface px-1 text-xs font-bold text-text-primary"
                  aria-label={t(statusKey(item.status))}
                  title={t(statusKey(item.status))}
                >
                  <span aria-hidden="true">{STATUS_MARK[item.status]}</span>
                </span>
                <div className="min-w-0">
                  <p className="text-xs font-medium text-text-muted">{t(contentTypeKey(item.type))}</p>
                  <p className="break-words text-sm font-medium text-text-primary">{item.name}</p>
                  <p className="mt-0.5 text-xs text-text-secondary">{t(statusKey(item.status))}</p>
                  {hasProgress(item) && (
                    <p className="mt-0.5 text-xs text-text-secondary">
                      {t("learningPathProgress", { completed: item.completed_units, total: item.total_units })}
                    </p>
                  )}
                  {"relation_type" in item && <div className="mt-2 space-y-1 text-xs text-text-secondary"><p>{tm(`relationTypes.${item.relation_type}`)}</p>{item.evidence_summary && <p className="break-words leading-5">{item.evidence_summary}</p>}<p>{tm("relationStrength")} {Math.round(item.strength * 100)}% · {tm("evidenceConfidence")} {Math.round(item.confidence * 100)}%</p><p>{tm("optionalRelation")}</p></div>}
                </div>
              </div>
              {(href || mapHref) && (
                <div className="flex shrink-0 flex-wrap gap-x-3 gap-y-1 text-sm font-medium">
                  {href && (
                    <Link
                      href={href}
                      className="text-accent-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
                    >
                      {t("learningPathOpenContent")}
                    </Link>
                  )}
                  {mapHref && (
                    <Link
                      href={mapHref}
                      className="text-accent-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
                    >
                      {t("learningPathOpenMap")}
                    </Link>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * 기존 선행조건 접근 판단을 대체하지 않고, 공개 문제 상세 이후에만
 * 읽기 전용 학습 경로 투영을 조회해 설명을 보강한다.
 */
export function LearningPathPanel({ challengeId, refreshKey = "" }: LearningPathPanelProps) {
  const t = useTranslations("wargame");
  const tn = useTranslations("nav");
  const tm = useTranslations("knowledgeMap");
  const { user } = useAuth();
  const [retry, setRetry] = useState(0);
  const requestKey = `${challengeId}:${refreshKey}:${user?.id ?? "anonymous"}:${retry}`;
  const [response, setResponse] = useState<{ key: string; state: PanelState }>({ key: requestKey, state: { kind: "loading" } });
  const state: PanelState = response.key === requestKey ? response.state : { kind: "loading" };

  useEffect(() => {
    const controller = new AbortController();
    const commit = (next: PanelState) => setResponse({ key: requestKey, state: next });

    void apiFetch<unknown>(`/api/learn/learning-path/challenges/${challengeId}`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (controller.signal.aborted) return;
        const projection = readProjection(value);
        commit(projection ? { kind: "ready", projection } : { kind: "unavailable" });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
          commit({ kind: "unauthenticated" });
          return;
        }
        commit({ kind: "unavailable" });
      });

    return () => controller.abort();
  }, [challengeId, requestKey]);

  if (state.kind === "loading") {
    return (
      <section className="rounded-xl border border-border bg-bg-surface p-5" aria-busy="true" aria-live="polite">
        <h2 className="text-lg font-semibold text-text-primary">{t("learningPathTitle")}</h2>
        <p className="mt-2 text-sm text-text-muted">{t("learningPathLoading")}</p>
      </section>
    );
  }

  if (state.kind === "unauthenticated") {
    return (
      <section className="rounded-xl border border-border bg-bg-surface p-5" aria-live="polite">
        <h2 className="text-lg font-semibold text-text-primary">{t("learningPathTitle")}</h2>
        <p className="mt-2 text-sm text-text-secondary">{t("learningPathSignInRequired")}</p>
      </section>
    );
  }

  if (state.kind === "unavailable") {
    return (
      <section className="rounded-xl border border-border bg-bg-surface p-5" aria-live="polite">
        <h2 className="text-lg font-semibold text-text-primary">{t("learningPathTitle")}</h2>
        <p className="mt-2 text-sm text-text-secondary">{t("learningPathUnavailable")}</p>
        <button type="button" onClick={() => setRetry((value) => value + 1)} className="mt-3 text-sm font-medium text-accent-primary">{tm("retry")}</button>
      </section>
    );
  }

  const { projection } = state;
  if (!hasLearningPathContent(projection)) return null;
  const mapFocusHref = normalizeMapHref(projection.map_focus_href);
  const action = learningPathAction(projection);

  return (
    <section className="rounded-xl border border-border bg-bg-surface p-5" aria-labelledby="learning-path-title">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 id="learning-path-title" className="text-lg font-semibold text-text-primary">{t("learningPathTitle")}</h2>
          <p className="mt-1 text-sm text-text-secondary">{t(accessStateMessageKey(projection.access_state))}</p>
        </div>
        {mapFocusHref && (
          <Link
            href={mapFocusHref}
            className="shrink-0 text-sm font-medium text-accent-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
          >
            {t("learningPathOpenMap")}
          </Link>
        )}
      </div>

      <div className="mt-5 space-y-5">
        <StepList heading={t("learningPathRequired")} items={projection.required_steps} />
        <StepList heading={t("learningPathRecommended")} items={projection.recommended_steps} />
        <StepList heading={t("learningPathHelpful")} items={projection.helpful_content} />
      </div>

      {action && (
        <div className="mt-4">
          <Link
            href={action.href}
            className="inline-flex max-w-full items-center gap-2 text-sm font-medium text-accent-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
          >
            <span className="min-w-0 break-words">{action.kind === "sign_in" ? tn("login")
              : action.kind === "contact_admin" ? t("learningPathContactAdmin")
                : t("learningPathContinue", { name: action.name })}</span>
            <ArrowRight className="h-4 w-4 shrink-0" aria-hidden="true" />
          </Link>
        </div>
      )}
    </section>
  );
}
