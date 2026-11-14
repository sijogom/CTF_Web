"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { KnowledgeProposalBulkReview } from "@/components/admin/knowledge/KnowledgeProposalBulkReview";
import { KnowledgeContentPicker } from "@/components/admin/knowledge/KnowledgeContentPicker";
import { ListPagination } from "@/components/ui/ListPagination";
import { apiFetch } from "@/lib/api-client";
import { readEnum, readPositiveInt, writeUrlQuery } from "@/lib/url-query";
import { parseOffsetListPage } from "@/lib/list-response.mjs";
import { readBulkReviewResults } from "@/lib/knowledge-review-state.mjs";
import { knowledgeExtensionsEnabled } from "@/lib/knowledge-release-stage.mjs";
import { useAuth } from "@/providers/AuthProvider";
import { useToast } from "@/providers/ToastProvider";
import type {
  KnowledgeCandidateRun,
  KnowledgeCandidateGenerationResult,
  KnowledgeCandidatePreview,
  KnowledgeCandidateRunList,
  KnowledgeContentType,
  KnowledgeContentSummary,
  KnowledgeProposalList,
  KnowledgeProposalOrigin,
  KnowledgeProposalState,
  KnowledgeRelation,
  KnowledgeRelationProposal,
  KnowledgeRelationList,
  KnowledgeRelationStatus,
  KnowledgeRelationType,
} from "@/types/knowledge";

const RELATION_TYPES: Array<{ value: KnowledgeRelationType; label: string }> = [
  { value: "requires", label: "필수 선행" },
  { value: "prepares_for", label: "선행 학습 도움" },
  { value: "recommends", label: "추천 학습" },
  { value: "helps", label: "도움 콘텐츠" },
  { value: "related", label: "관련 콘텐츠" },
  { value: "similar", label: "유사 콘텐츠" },
  { value: "tag_affinity", label: "태그 연관" },
];

const PROPOSAL_STATE_OPTIONS: Array<{ value: KnowledgeProposalState; label: string }> = [
  { value: "draft", label: "초안" },
  { value: "pending_review", label: "검토 대기" },
  { value: "approved", label: "승인됨" },
  { value: "rejected", label: "반려됨" },
  { value: "retired", label: "비활성화됨" },
  { value: "superseded", label: "대체됨" },
];

const PROPOSAL_ORIGIN_OPTIONS: Array<{ value: KnowledgeProposalOrigin; label: string }> = [
  { value: "manual", label: "수동 등록" },
  { value: "rule", label: "규칙 후보" },
  { value: "model_candidate", label: "모델 후보" },
  { value: "import", label: "가져오기" },
  { value: "observation", label: "관찰 기록" },
];

const CANDIDATE_RUN_STATE_OPTIONS = [
  { value: "succeeded", label: "성공" },
  { value: "failed", label: "실패" },
  { value: "running", label: "실행 중" },
  { value: "queued", label: "대기" },
] as const;

const LIST_PAGE_SIZE = 20;
const MANAGEMENT_TABS = ["relations", "proposals", "automation"] as const;
type ManagementTab = (typeof MANAGEMENT_TABS)[number];
const RELATION_STATUS_FILTERS = ["all", "draft", "pending_review", "approved", "rejected", "retired"] as const;
const PROPOSAL_STATE_FILTERS = ["all", ...PROPOSAL_STATE_OPTIONS.map((item) => item.value)] as const;
const PROPOSAL_ORIGIN_FILTERS = ["all", ...PROPOSAL_ORIGIN_OPTIONS.map((item) => item.value)] as const;
const CANDIDATE_RUN_STATE_FILTERS = ["all", ...CANDIDATE_RUN_STATE_OPTIONS.map((item) => item.value)] as const;

type RelationStatusFilter = (typeof RELATION_STATUS_FILTERS)[number];
type ProposalStateFilter = (typeof PROPOSAL_STATE_FILTERS)[number];
type ProposalOriginFilter = (typeof PROPOSAL_ORIGIN_FILTERS)[number];
type CandidateRunStateFilter = (typeof CANDIDATE_RUN_STATE_FILTERS)[number];

function proposalStateClass(state: string): string {
  switch (state) {
    case "approved":
      return "bg-accent-success/15 text-accent-success";
    case "pending_review":
      return "bg-accent-warning/15 text-accent-warning";
    case "rejected":
      return "bg-accent-error/15 text-accent-error";
    case "retired":
    case "superseded":
      return "bg-bg-elevated text-text-muted";
    default:
      return "bg-bg-elevated text-text-secondary";
  }
}

interface RelationForm {
  sourceType: KnowledgeContentType;
  sourceId: string;
  targetType: KnowledgeContentType;
  targetId: string;
  relationType: KnowledgeRelationType;
  isDirected: boolean;
  strength: number;
  confidence: number;
  evidenceSummary: string;
}

const INITIAL_FORM: RelationForm = {
  sourceType: "course",
  sourceId: "",
  targetType: "challenge",
  targetId: "",
  relationType: "helps",
  isDirected: true,
  strength: 0.7,
  confidence: 0.8,
  evidenceSummary: "",
};

function statusClass(status: KnowledgeRelationStatus): string {
  switch (status) {
    case "approved":
      return "bg-accent-success/15 text-accent-success";
    case "pending_review":
      return "bg-accent-warning/15 text-accent-warning";
    case "rejected":
      return "bg-accent-error/15 text-accent-error";
    case "retired":
      return "bg-bg-elevated text-text-muted";
    default:
      return "bg-bg-elevated text-text-secondary";
  }
}

export default function KnowledgeRelationManagementPage() {
  const t = useTranslations("admin");
  const tk = useTranslations("knowledgeAdmin");
  const m = useTranslations("knowledgeAdmin.management");
  const locale = useLocale();
  const formatDateTime = (value: string | null | undefined): string => {
    if (!value) return m("noRecord");
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? m("noRecord") : date.toLocaleString(locale);
  };
  const searchParams = useSearchParams();
  const { toast } = useToast();
  const { isSuperAdmin, user } = useAuth();
  const [activeTab, setActiveTab] = useState<ManagementTab>(() => readEnum(searchParams.get("knowledge_view"), MANAGEMENT_TABS, "relations"));
  const [relations, setRelations] = useState<KnowledgeRelation[]>([]);
  const [relationTotal, setRelationTotal] = useState(0);
  const [relationPage, setRelationPage] = useState(() => readPositiveInt(searchParams.get("relation_page")));
  const [loading, setLoading] = useState(true);
  const [relationError, setRelationError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [filter, setFilter] = useState<RelationStatusFilter>(() => (
    readEnum(searchParams.get("relation_status"), RELATION_STATUS_FILTERS, "all")
  ));
  const [form, setForm] = useState<RelationForm>(INITIAL_FORM);
  const [sourceContent, setSourceContent] = useState<KnowledgeContentSummary | null>(null);
  const [targetContent, setTargetContent] = useState<KnowledgeContentSummary | null>(null);
  const [editingRelation, setEditingRelation] = useState<KnowledgeRelation | null>(null);
  const [editReason, setEditReason] = useState("");
  const [reviewReasons, setReviewReasons] = useState<Record<string, string>>({});
  const [reconcileMessage, setReconcileMessage] = useState<string | null>(null);
  const [proposals, setProposals] = useState<KnowledgeRelationProposal[]>([]);
  const [proposalTotal, setProposalTotal] = useState(0);
  const [proposalPage, setProposalPage] = useState(() => readPositiveInt(searchParams.get("proposal_page")));
  const [proposalLoading, setProposalLoading] = useState(true);
  const [proposalStateFilter, setProposalStateFilter] = useState<ProposalStateFilter>(() => (
    readEnum(searchParams.get("proposal_state"), PROPOSAL_STATE_FILTERS, "all")
  ));
  const [proposalOriginFilter, setProposalOriginFilter] = useState<ProposalOriginFilter>(() => (
    readEnum(searchParams.get("proposal_origin"), PROPOSAL_ORIGIN_FILTERS, "all")
  ));
  const [proposalReviewReasons, setProposalReviewReasons] = useState<Record<string, string>>({});
  const [reviewingProposalId, setReviewingProposalId] = useState<string | null>(null);
  const [selectedProposalIds, setSelectedProposalIds] = useState<string[]>([]);
  const [bulkReason, setBulkReason] = useState("");
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkResults, setBulkResults] = useState<Array<{ proposal_id: string; status: string; error?: string; name: string }>>([]);
  const [proposalError, setProposalError] = useState(false);
  const [pendingProposalId, setPendingProposalId] = useState<string | null>(null);
  const reviewBusyRef = useRef(false);
  const [candidateRuns, setCandidateRuns] = useState<KnowledgeCandidateRun[]>([]);
  const [candidateRunTotal, setCandidateRunTotal] = useState(0);
  const [candidateRunPage, setCandidateRunPage] = useState(() => readPositiveInt(searchParams.get("run_page")));
  const [candidateRunLoading, setCandidateRunLoading] = useState(true);
  const [candidateRunError, setCandidateRunError] = useState(false);
  const [candidateRunStateFilter, setCandidateRunStateFilter] = useState<CandidateRunStateFilter>(() => (
    readEnum(searchParams.get("run_state"), CANDIDATE_RUN_STATE_FILTERS, "all")
  ));
  const [candidateGenerationBusy, setCandidateGenerationBusy] = useState(false);
  const [candidatePreview, setCandidatePreview] = useState<KnowledgeCandidatePreview[] | null>(null);
  const [candidatePreviewTruncated, setCandidatePreviewTruncated] = useState(false);
  const relationRequestRef = useRef(0);
  const proposalRequestRef = useRef(0);
  const candidateRunRequestRef = useRef(0);
  const relationAbortRef = useRef<AbortController | null>(null);
  const proposalAbortRef = useRef<AbortController | null>(null);
  const candidateRunAbortRef = useRef<AbortController | null>(null);
  const savingRef = useRef(false);
  const generationBusyRef = useRef(false);
  const reconcileBusyRef = useRef(false);
  const [reconcileBusy, setReconcileBusy] = useState(false);

  useEffect(() => {
    const syncFromUrl = () => {
      const params = new URL(window.location.href).searchParams;
      setActiveTab(readEnum(params.get("knowledge_view"), MANAGEMENT_TABS, "relations"));
      setSelectedProposalIds([]);
      setBulkResults([]);
      setFilter(readEnum(params.get("relation_status"), RELATION_STATUS_FILTERS, "all"));
      setRelationPage(readPositiveInt(params.get("relation_page")));
      setProposalStateFilter(readEnum(params.get("proposal_state"), PROPOSAL_STATE_FILTERS, "all"));
      setProposalOriginFilter(readEnum(params.get("proposal_origin"), PROPOSAL_ORIGIN_FILTERS, "all"));
      setProposalPage(readPositiveInt(params.get("proposal_page")));
      setCandidateRunStateFilter(readEnum(params.get("run_state"), CANDIDATE_RUN_STATE_FILTERS, "all"));
      setCandidateRunPage(readPositiveInt(params.get("run_page")));
    };
    window.addEventListener("popstate", syncFromUrl);
    return () => {
      window.removeEventListener("popstate", syncFromUrl);
      relationAbortRef.current?.abort();
      proposalAbortRef.current?.abort();
      candidateRunAbortRef.current?.abort();
    };
  }, []);

  const loadRelations = useCallback(async (
    nextFilter: RelationStatusFilter = filter,
    nextPage: number = relationPage,
  ) => {
    const requestId = ++relationRequestRef.current;
    relationAbortRef.current?.abort();
    const controller = new AbortController();
    relationAbortRef.current = controller;
    setLoading(true);
    setRelationError(false);
    try {
      const params = new URLSearchParams({
        offset: String((nextPage - 1) * LIST_PAGE_SIZE),
        limit: String(LIST_PAGE_SIZE),
      });
      if (nextFilter !== "all") params.set("status", nextFilter);
      const expectedOffset = (nextPage - 1) * LIST_PAGE_SIZE;
      const rawResponse = await apiFetch<unknown>(
        `/api/learn/admin/knowledge-relations?${params.toString()}`,
        { signal: controller.signal },
      );
      if (requestId !== relationRequestRef.current) return;
      const response = parseOffsetListPage<KnowledgeRelation>(
        rawResponse,
        expectedOffset,
        LIST_PAGE_SIZE,
      ) as KnowledgeRelationList;
      setRelations(response.items);
      setRelationTotal(response.total);
      const lastPage = Math.max(1, Math.ceil(response.total / LIST_PAGE_SIZE));
      if (nextPage > lastPage) {
        setRelationPage(lastPage);
        writeUrlQuery({ relation_page: lastPage === 1 ? null : lastPage }, "replace");
      }
    } catch (error) {
      if (requestId !== relationRequestRef.current || (error instanceof DOMException && error.name === "AbortError")) return;
      setRelationError(true);
      toast("error", tk("loadFailed"));
    } finally {
      if (requestId === relationRequestRef.current) setLoading(false);
    }
  }, [filter, relationPage, toast, tk]);

  useEffect(() => {
    void loadRelations();
  }, [loadRelations]);

  const loadProposals = useCallback(async (
    nextState: ProposalStateFilter = proposalStateFilter,
    nextOrigin: ProposalOriginFilter = proposalOriginFilter,
    nextPage: number = proposalPage,
  ) => {
    const requestId = ++proposalRequestRef.current;
    proposalAbortRef.current?.abort();
    const controller = new AbortController();
    proposalAbortRef.current = controller;
    setProposalLoading(true);
    setProposalError(false);
    try {
      const params = new URLSearchParams({
        offset: String((nextPage - 1) * LIST_PAGE_SIZE),
        limit: String(LIST_PAGE_SIZE),
      });
      if (nextState !== "all") params.set("state", nextState);
      if (nextOrigin !== "all") params.set("origin", nextOrigin);
      const expectedOffset = (nextPage - 1) * LIST_PAGE_SIZE;
      const rawResponse = await apiFetch<unknown>(
        `/api/learn/admin/knowledge-proposals?${params.toString()}`,
        { signal: controller.signal },
      );
      if (requestId !== proposalRequestRef.current) return;
      const response = parseOffsetListPage<KnowledgeRelationProposal>(
        rawResponse,
        expectedOffset,
        LIST_PAGE_SIZE,
      ) as KnowledgeProposalList;
      setProposals(response.items);
      setSelectedProposalIds((current) => current.filter((id) => response.items.some((item) => item.id === id)));
      setProposalTotal(response.total);
      const lastPage = Math.max(1, Math.ceil(response.total / LIST_PAGE_SIZE));
      if (nextPage > lastPage) {
        setProposalPage(lastPage);
        writeUrlQuery({ proposal_page: lastPage === 1 ? null : lastPage }, "replace");
      }
    } catch (error) {
      if (requestId !== proposalRequestRef.current || (error instanceof DOMException && error.name === "AbortError")) return;
      setProposalError(true);
      toast("error", error instanceof Error ? error.message : tk("loadFailed"));
    } finally {
      if (requestId === proposalRequestRef.current) setProposalLoading(false);
    }
  }, [proposalOriginFilter, proposalPage, proposalStateFilter, toast, tk]);

  useEffect(() => {
    void loadProposals();
  }, [loadProposals]);

  const loadCandidateRuns = useCallback(async (
    nextState: CandidateRunStateFilter = candidateRunStateFilter,
    nextPage: number = candidateRunPage,
  ) => {
    const requestId = ++candidateRunRequestRef.current;
    candidateRunAbortRef.current?.abort();
    const controller = new AbortController();
    candidateRunAbortRef.current = controller;
    setCandidateRunLoading(true);
    setCandidateRunError(false);
    try {
      const params = new URLSearchParams({
        offset: String((nextPage - 1) * LIST_PAGE_SIZE),
        limit: String(LIST_PAGE_SIZE),
      });
      if (nextState !== "all") params.set("state", nextState);
      const expectedOffset = (nextPage - 1) * LIST_PAGE_SIZE;
      const rawResponse = await apiFetch<unknown>(
        `/api/learn/admin/knowledge-candidate-runs?${params.toString()}`,
        { signal: controller.signal },
      );
      if (requestId !== candidateRunRequestRef.current) return;
      const response = parseOffsetListPage<KnowledgeCandidateRun>(
        rawResponse,
        expectedOffset,
        LIST_PAGE_SIZE,
      ) as KnowledgeCandidateRunList;
      setCandidateRuns(response.items);
      setCandidateRunTotal(response.total);
      const lastPage = Math.max(1, Math.ceil(response.total / LIST_PAGE_SIZE));
      if (nextPage > lastPage) {
        setCandidateRunPage(lastPage);
        writeUrlQuery({ run_page: lastPage === 1 ? null : lastPage }, "replace");
      }
    } catch (error) {
      if (requestId !== candidateRunRequestRef.current || (error instanceof DOMException && error.name === "AbortError")) return;
      setCandidateRunError(true);
      toast("error", error instanceof Error ? error.message : tk("loadFailed"));
    } finally {
      if (requestId === candidateRunRequestRef.current) setCandidateRunLoading(false);
    }
  }, [candidateRunPage, candidateRunStateFilter, toast, tk]);

  useEffect(() => {
    void loadCandidateRuns();
  }, [loadCandidateRuns]);

  const reloadCurrentLists = async () => {
    const params = new URL(window.location.href).searchParams;
    await Promise.all([
      loadRelations(readEnum(params.get("relation_status"), RELATION_STATUS_FILTERS, "all"), readPositiveInt(params.get("relation_page"))),
      loadProposals(readEnum(params.get("proposal_state"), PROPOSAL_STATE_FILTERS, "all"), readEnum(params.get("proposal_origin"), PROPOSAL_ORIGIN_FILTERS, "all"), readPositiveInt(params.get("proposal_page"))),
    ]);
  };

  const changeRelationFilter = (nextFilter: RelationStatusFilter) => {
    setLoading(true);
    setFilter(nextFilter);
    setRelationPage(1);
    writeUrlQuery({
      relation_status: nextFilter === "all" ? null : nextFilter,
      relation_page: null,
    }, "push");
  };

  const changeRelationPage = (nextPage: number) => {
    setLoading(true);
    setRelationPage(nextPage);
    writeUrlQuery({ relation_page: nextPage === 1 ? null : nextPage }, "push");
  };

  const changeProposalStateFilter = (nextState: ProposalStateFilter) => {
    setProposalLoading(true);
    setSelectedProposalIds([]);
    setBulkResults([]);
    setProposalStateFilter(nextState);
    setProposalPage(1);
    writeUrlQuery({
      proposal_state: nextState === "all" ? null : nextState,
      proposal_page: null,
    }, "push");
  };

  const changeProposalOriginFilter = (nextOrigin: ProposalOriginFilter) => {
    setProposalLoading(true);
    setSelectedProposalIds([]);
    setBulkResults([]);
    setProposalOriginFilter(nextOrigin);
    setProposalPage(1);
    writeUrlQuery({
      proposal_origin: nextOrigin === "all" ? null : nextOrigin,
      proposal_page: null,
    }, "push");
  };

  const changeProposalPage = (nextPage: number) => {
    setProposalLoading(true);
    setSelectedProposalIds([]);
    setBulkResults([]);
    setProposalPage(nextPage);
    writeUrlQuery({ proposal_page: nextPage === 1 ? null : nextPage }, "push");
  };

  const changeCandidateRunFilter = (nextState: CandidateRunStateFilter) => {
    setCandidateRunStateFilter(nextState);
    setCandidateRunPage(1);
    writeUrlQuery({
      run_state: nextState === "all" ? null : nextState,
      run_page: null,
    }, "push");
  };

  const changeCandidateRunPage = (nextPage: number) => {
    setCandidateRunPage(nextPage);
    writeUrlQuery({ run_page: nextPage === 1 ? null : nextPage }, "push");
  };

  const generateCandidates = async (dryRun: boolean) => {
    if (!isSuperAdmin || generationBusyRef.current) return;
    if (!dryRun) {
      const accepted = window.confirm(
        m("generateConfirm"),
      );
      if (!accepted) return;
    }
    generationBusyRef.current = true;
    setCandidateGenerationBusy(true);
    try {
      const result = await apiFetch<KnowledgeCandidateGenerationResult>(
        "/api/learn/admin/knowledge-candidate-runs/generate",
        {
          method: "POST",
          body: { dry_run: dryRun, max_candidates: 100 },
        },
      );
      setCandidatePreview(result.preview);
      setCandidatePreviewTruncated(result.preview_truncated);
      toast(
        "success",
        dryRun
          ? m("previewCreated", { count: result.run.output_count, duplicates: result.run.duplicate_count })
          : m("candidatesCreated", { count: result.run.output_count }),
      );
      if (!dryRun) {
        setProposalStateFilter("pending_review");
        setProposalOriginFilter("rule");
        setProposalPage(1);
        writeUrlQuery({
          proposal_state: "pending_review",
          proposal_origin: "rule",
          proposal_page: null,
        }, "replace");
        await Promise.all([
          loadProposals("pending_review", "rule", 1),
          loadCandidateRuns(candidateRunStateFilter, candidateRunPage),
        ]);
      } else {
        await Promise.all([
          loadProposals(proposalStateFilter, proposalOriginFilter, proposalPage),
          loadCandidateRuns(candidateRunStateFilter, candidateRunPage),
        ]);
      }
    } catch (error) {
      toast("error", error instanceof Error ? error.message : m("generationFailed"));
    } finally {
      generationBusyRef.current = false;
      setCandidateGenerationBusy(false);
    }
  };

  const reviewProposal = async (
    proposal: KnowledgeRelationProposal,
    action: "approve" | "reject" | "retire",
  ) => {
    if (!isSuperAdmin || reviewBusyRef.current || savingRef.current || proposalLoading || proposalError) return;
    if (action === "approve" && user?.id && proposal.created_by === user.id) {
      toast("error", tk("ownApproval"));
      return;
    }
    const reason = proposalReviewReasons[proposal.id]?.trim() ?? "";
    if (reason.length < 3) {
      toast("error", tk("reasonRequired"));
      return;
    }

    reviewBusyRef.current = true;
    setReviewingProposalId(proposal.id);
    try {
      await apiFetch(`/api/learn/admin/knowledge-proposals/${proposal.id}/review`, {
        method: "POST",
        body: { action, reason },
      });
      setProposalReviewReasons((current) => ({ ...current, [proposal.id]: "" }));
      toast("success", m(`reviewSuccess.${action}`));
      await reloadCurrentLists();
    } catch (error) {
      toast("error", error instanceof Error ? error.message : m("reviewFailed"));
    } finally {
      setReviewingProposalId(null);
      reviewBusyRef.current = false;
    }
  };

  const reviewBulk = async (action: "approve" | "reject" | "retire") => {
    if (!knowledgeExtensionsEnabled() || !isSuperAdmin || reviewBusyRef.current || savingRef.current || proposalLoading || proposalError || selectedProposalIds.length === 0) return;
    if (bulkReason.trim().length < 3) { toast("error", tk("reasonRequired")); return; }
    if (action === "approve" && proposals.some((item) => selectedProposalIds.includes(item.id) && item.created_by === user?.id)) { toast("error", tk("ownApproval")); return; }
    const ids = [...selectedProposalIds];
    const names = new Map(proposals.map((item) => [item.id, `${item.source.name} → ${item.target.name}`]));
    reviewBusyRef.current = true;
    setBulkBusy(true);
    setBulkResults([]);
    try {
      const result = await apiFetch<{ items: Array<{ proposal_id: string; status: "succeeded" | "failed"; error?: string }> }>("/api/learn/admin/knowledge-proposals/bulk-review", { method: "POST", body: { proposal_ids: ids, action, reason: bulkReason.trim() } });
      const rows = readBulkReviewResults(ids, result).map((item) => ({ ...item, name: names.get(item.proposal_id) ?? item.proposal_id }));
      setBulkResults(rows);
      setSelectedProposalIds(rows.filter((item) => item.status !== "succeeded").map((item) => item.proposal_id));
      await reloadCurrentLists();
    } catch (error) {
      setBulkResults(ids.map((id) => ({ proposal_id: id, status: "unknown", error: error instanceof Error ? error.message : tk("missingResult"), name: names.get(id) ?? id })));
    } finally { reviewBusyRef.current = false; setBulkBusy(false); }
  };

  const updateForm = <Key extends keyof RelationForm>(key: Key, value: RelationForm[Key]) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const startEdit = (relation: KnowledgeRelation) => {
    if (savingRef.current || reviewBusyRef.current) return;
    setActiveTab("relations");
    writeUrlQuery({ knowledge_view: null }, "replace");
    setEditingRelation(relation);
    setSourceContent(relation.source);
    setTargetContent(relation.target);
    setEditReason("");
    setForm({
      sourceType: relation.source.type,
      sourceId: relation.source.id,
      targetType: relation.target.type,
      targetId: relation.target.id,
      relationType: relation.relation_type,
      isDirected: relation.is_directed,
      strength: relation.strength,
      confidence: relation.confidence,
      evidenceSummary: relation.evidence_summary ?? "",
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const cancelEdit = () => {
    setEditingRelation(null);
    setEditReason("");
    setForm(INITIAL_FORM);
    setSourceContent(null);
    setTargetContent(null);
  };

  const saveRelation = async () => {
    if (savingRef.current || reviewBusyRef.current) return;
    if (!sourceContent || !targetContent || sourceContent.type !== form.sourceType || targetContent.type !== form.targetType || sourceContent.id !== form.sourceId || targetContent.id !== form.targetId) {
      toast("error", m("selectBoth"));
      return;
    }
    if (!form.evidenceSummary.trim()) {
      toast("error", m("evidenceRequired"));
      return;
    }
    if (editingRelation && editReason.trim().length < 3) {
      toast("error", m("editReasonRequired"));
      return;
    }
    savingRef.current = true;
    setSaving(true);
    try {
      const relationPayload = {
        source: { type: form.sourceType, id: form.sourceId.trim() },
        target: { type: form.targetType, id: form.targetId.trim() },
        relation_type: form.relationType,
        is_directed: form.isDirected,
        strength: form.strength,
        confidence: form.confidence,
        origin: "manual",
        evidence_summary: form.evidenceSummary.trim(),
      };
      let nextProposal: string | null = null;
      if (editingRelation) {
        const result = await apiFetch<KnowledgeRelation & { pending_proposal_id?: string | null }>(`/api/learn/admin/knowledge-relations/${editingRelation.id}`, {
          method: "PUT",
          body: { ...relationPayload, reason: editReason.trim() },
        });
        nextProposal = result.pending_proposal_id ?? null;
        setPendingProposalId(nextProposal);
        toast("success", nextProposal ? tk("revisionPending") : m("relationSaved"));
      } else {
        await apiFetch("/api/learn/admin/knowledge-relations", {
          method: "POST",
          body: relationPayload,
        });
        toast("success", m("relationSaved"));
      }
      cancelEdit();
      if (nextProposal) {
        setActiveTab("proposals");
        setSelectedProposalIds([]);
        setProposalStateFilter("pending_review"); setProposalOriginFilter("all"); setProposalPage(1);
        writeUrlQuery({ knowledge_view: "proposals", proposal_state: "pending_review", proposal_origin: null, proposal_page: null }, "replace");
        await Promise.all([loadRelations(), loadProposals("pending_review", "all", 1)]);
        return;
      }
      setFilter("pending_review");
      setRelationPage(1);
      writeUrlQuery({ relation_status: "pending_review", relation_page: null }, "replace");
      await loadRelations("pending_review", 1);
    } catch (error) {
      toast("error", error instanceof Error ? error.message : m("saveFailed"));
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  const reviewRelation = async (relation: KnowledgeRelation, action: "approve" | "reject" | "retire") => {
    if (reviewBusyRef.current || savingRef.current || relationError || loading) return;
    if (action === "approve" && relation.created_by === user?.id) { toast("error", tk("ownApproval")); return; }
    const reason = reviewReasons[relation.id]?.trim() ?? "";
    if (reason.length < 3) {
      toast("error", tk("reasonRequired"));
      return;
    }
    reviewBusyRef.current = true;
    setReviewingProposalId(relation.id);
    try {
      await apiFetch(`/api/learn/admin/knowledge-relations/${relation.id}/review`, {
        method: "POST",
        body: { action, reason },
      });
      toast("success", m(`reviewSuccess.${action}`));
      setReviewReasons((current) => ({ ...current, [relation.id]: "" }));
      await reloadCurrentLists();
    } catch (error) {
      toast("error", error instanceof Error ? error.message : m("reviewFailed"));
    } finally {
      reviewBusyRef.current = false;
      setReviewingProposalId(null);
    }
  };

  const dryRunReconcile = async () => {
    if (!isSuperAdmin || reconcileBusyRef.current) return;
    reconcileBusyRef.current = true;
    setReconcileBusy(true);
    try {
      const result = await apiFetch<{
        scanned: number;
        updated: number;
        deactivated: number;
        missing_source: number;
      }>("/api/learn/admin/knowledge-nodes/reconcile", {
        method: "POST",
        body: { dry_run: true },
      });
      setReconcileMessage(
        m("reconcileResult", { scanned: result.scanned, updated: result.updated, deactivated: result.deactivated, missing: result.missing_source }),
      );
    } catch (error) {
      toast("error", error instanceof Error ? error.message : m("reconcileFailed"));
    } finally {
      reconcileBusyRef.current = false;
      setReconcileBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t("knowledgeRelationManagement")}</h1>
      </div>
      <div role="tablist" aria-label={m("tabsLabel")} className="flex flex-wrap gap-1 border-b border-border">
        {MANAGEMENT_TABS.map((tab) => <button key={tab} id={`knowledge-tab-${tab}`} type="button" role="tab" aria-selected={activeTab === tab} aria-controls={`knowledge-panel-${tab}`} tabIndex={activeTab === tab ? 0 : -1}
          onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const index = MANAGEMENT_TABS.indexOf(tab);
            const next = MANAGEMENT_TABS[event.key === "Home" ? 0 : event.key === "End" ? MANAGEMENT_TABS.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + MANAGEMENT_TABS.length) % MANAGEMENT_TABS.length];
            setActiveTab(next); setSelectedProposalIds([]); setBulkResults([]);
            writeUrlQuery({ knowledge_view: next === "relations" ? null : next }, "push");
            document.getElementById(`knowledge-tab-${next}`)?.focus();
          }}
          onClick={() => { setActiveTab(tab); setSelectedProposalIds([]); setBulkResults([]); writeUrlQuery({ knowledge_view: tab === "relations" ? null : tab }, "push"); }}
          className={`border-b-2 px-4 py-3 text-sm font-medium ${activeTab === tab ? "border-accent-primary text-accent-primary" : "border-transparent text-text-secondary hover:text-text-primary"}`}>
          {m(`tabs.${tab}`)}
        </button>)}
      </div>

      <div id="knowledge-panel-relations" role="tabpanel" aria-labelledby="knowledge-tab-relations" hidden={activeTab !== "relations"} className="space-y-6">
      <section className="rounded-lg border border-border bg-bg-surface p-4 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">{m(editingRelation ? "editRelation" : "newRelation")}</h2>
            {editingRelation?.status === "approved" && <p className="mt-1 max-w-2xl text-sm text-text-secondary">{m("approvedEdit")}</p>}
          </div>
          {isSuperAdmin && (
            <Button type="button" variant="secondary" size="sm" disabled={reconcileBusy} onClick={() => void dryRunReconcile()}>
              {m(reconcileBusy ? "checking" : "reconcile")}
            </Button>
          )}
        </div>
        {reconcileMessage && <p className="mt-3 rounded-lg bg-bg-elevated px-3 py-2 text-sm text-text-secondary">{reconcileMessage}</p>}

        <div className="mt-5 grid gap-4 lg:grid-cols-2">
          <KnowledgeContentPicker key={`source-${user?.id ?? "anonymous"}`} label={m("source")} type={form.sourceType} value={sourceContent} disabled={saving || editingRelation?.status === "approved"}
            onTypeChange={(type) => setForm((current) => ({ ...current, sourceType: type, sourceId: "" }))}
            onChange={(content) => { setSourceContent(content); updateForm("sourceId", content?.id ?? ""); }} />
          <KnowledgeContentPicker key={`target-${user?.id ?? "anonymous"}`} label={m("target")} type={form.targetType} value={targetContent} disabled={saving || editingRelation?.status === "approved"}
            onTypeChange={(type) => setForm((current) => ({ ...current, targetType: type, targetId: "" }))}
            onChange={(content) => { setTargetContent(content); updateForm("targetId", content?.id ?? ""); }} />

          <label className="block">
            <span className="mb-1 block text-sm font-medium">{m("relationType")}</span>
            <select
              value={form.relationType}
              disabled={saving || editingRelation?.status === "approved"}
              onChange={(event) => updateForm("relationType", event.target.value as KnowledgeRelationType)}
              className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm"
            >
              {RELATION_TYPES.map((item) => <option key={item.value} value={item.value}>{m(`relationTypes.${item.value}`)}</option>)}
            </select>
          </label>

          {editingRelation && (
            <label className="block lg:col-span-2">
              <span className="mb-1 block text-sm font-medium">{m("editReason")}</span>
              <input
                value={editReason}
                disabled={saving}
                onChange={(event) => setEditReason(event.target.value)}
                maxLength={1000}
                placeholder={m("editReasonPlaceholder")}
                className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm"
              />
            </label>
          )}

          <label className="block">
            <span className="mb-1 block text-sm font-medium">{m("evidence")}</span>
            <input
              value={form.evidenceSummary}
              disabled={saving}
              onChange={(event) => updateForm("evidenceSummary", event.target.value)}
              maxLength={1000}
              placeholder={m("evidencePlaceholder")}
              className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm"
            />
          </label>

          <label className="block">
            <span className="mb-1 flex justify-between text-sm font-medium"><span>{m("strength")}</span><span>{Math.round(form.strength * 100)}%</span></span>
            <input
              type="range"
              min="0"
              max="1"
              step="0.05"
              value={form.strength}
              disabled={saving}
              onChange={(event) => updateForm("strength", Number(event.target.value))}
              className="w-full accent-accent-primary"
            />
          </label>

          <label className="block">
            <span className="mb-1 flex justify-between text-sm font-medium"><span>{m("confidence")}</span><span>{Math.round(form.confidence * 100)}%</span></span>
            <input
              type="range"
              min="0"
              max="1"
              step="0.05"
              value={form.confidence}
              disabled={saving}
              onChange={(event) => updateForm("confidence", Number(event.target.value))}
              className="w-full accent-accent-primary"
            />
          </label>
        </div>

        <label className="mt-4 flex items-center gap-2 text-sm text-text-secondary">
          <input
            type="checkbox"
            checked={form.isDirected}
            disabled={saving || editingRelation?.status === "approved"}
            onChange={(event) => updateForm("isDirected", event.target.checked)}
          />
          {m("directed")}
        </label>
        <div className="mt-5">
          <div className="flex gap-2">
            <Button type="button" onClick={() => void saveRelation()} disabled={saving || !sourceContent || !targetContent}>
              {m(saving ? "saving" : editingRelation ? "saveRevision" : "create")}
            </Button>
            {editingRelation && (
              <Button type="button" variant="secondary" onClick={cancelEdit} disabled={saving}>
                {m("cancelEdit")}
              </Button>
            )}
          </div>
        </div>
      </section>

      <section className="rounded-lg border border-border bg-bg-surface">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border p-4">
          <div>
            <h2 className="font-semibold">{m("tabs.relations")}</h2>
          </div>
          <select
            aria-label={m("statusFilter")}
            value={filter}
            onChange={(event) => changeRelationFilter(event.target.value as RelationStatusFilter)}
            className="rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm"
          >
            <option value="all">{m("allStates")}</option>
            {RELATION_STATUS_FILTERS.filter((value) => value !== "all").map((value) => <option key={value} value={value}>{m(`states.${value}`)}</option>)}
          </select>
        </div>

        {loading ? (
          <p role="status" className="p-8 text-center text-text-muted">{m("loading")}</p>
        ) : relationError ? <div role="alert" className="flex flex-wrap items-center gap-3 p-4 text-sm text-accent-error">{m("relationsFailed")}<button type="button" onClick={() => void loadRelations()} className="text-accent-primary">{m("retry")}</button></div> : relations.length === 0 ? (
          <p className="p-8 text-center text-text-muted">{m("relationsEmpty")}</p>
        ) : (
          <div className="divide-y divide-border">
            {relations.map((relation) => (
              <article key={relation.id} className="space-y-3 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex flex-wrap items-center gap-2 text-sm">
                      <span className="break-words font-semibold">{relation.source.name}</span>
                      <span className="text-text-muted">({m(`contentTypes.${relation.source.type}`)})</span>
                      <span aria-label={m(relation.is_directed ? "directedLabel" : "bidirectionalLabel")} className="text-accent-primary">
                        {relation.is_directed ? "→" : "↔"}
                      </span>
                      <span className="break-words font-semibold">{relation.target.name}</span>
                      <span className="text-text-muted">({m(`contentTypes.${relation.target.type}`)})</span>
                    </div>
                    <p className="mt-1 text-xs text-text-muted">
                      {m(`relationTypes.${relation.relation_type}`)} · {m("quality", { strength: Math.round(relation.strength * 100), confidence: Math.round(relation.confidence * 100) })} · {m(`origins.${relation.origin}`)}
                    </p>
                  </div>
                  <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${statusClass(relation.status)}`}>
                    {m(`states.${relation.status}`)}
                  </span>
                </div>

                <p className="rounded-lg bg-bg-elevated px-3 py-2 text-sm text-text-secondary">
                  <strong className="font-medium text-text-primary">{m("evidence")}:</strong> {relation.evidence_summary || m("noEvidence")}
                </p>
                <p className="text-xs text-text-secondary">{m("createdBy")}: {relation.created_by === user?.id ? m("you") : m("administrator")} · {formatDateTime(relation.created_at)}</p>
                <details className="text-xs text-text-secondary"><summary className="cursor-pointer">{m("identifiers")}</summary><dl className="mt-2 space-y-1 break-all"><div><dt>{m("createdBy")}</dt><dd>{relation.created_by}</dd></div><div><dt>{m("source")}</dt><dd>{relation.source.id}</dd></div><div><dt>{m("target")}</dt><dd>{relation.target.id}</dd></div></dl></details>

                {relation.status !== "rejected" && relation.status !== "retired" && (
                  <Button type="button" size="sm" variant="secondary" disabled={saving || bulkBusy || reviewingProposalId !== null} onClick={() => startEdit(relation)}>
                    {m(relation.status === "approved" ? "revise" : "edit")}
                  </Button>
                )}
                {(relation.status === "pending_review" || relation.status === "draft") && relation.created_by === user?.id && <p id={`own-relation-${relation.id}`} role="note" className="text-sm text-text-secondary">{tk("ownApproval")}</p>}

                {isSuperAdmin && (relation.status === "pending_review" || relation.status === "draft" || relation.status === "approved") && (
                  <div className="flex flex-wrap items-center gap-2">
                    <input
                      value={reviewReasons[relation.id] ?? ""}
                      onChange={(event) => setReviewReasons((current) => ({ ...current, [relation.id]: event.target.value }))}
                      aria-label={m("reviewReason")}
                      placeholder={m("reviewReasonPlaceholder")}
                      disabled={bulkBusy || reviewingProposalId !== null || saving}
                      maxLength={1000}
                      className="w-full min-w-0 flex-none rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm sm:w-auto sm:min-w-[min(100%,16rem)] sm:flex-1"
                    />
                    {relation.status !== "approved" ? (
                      <>
                        <Button type="button" size="sm" aria-describedby={relation.created_by === user?.id ? `own-relation-${relation.id}` : undefined} disabled={saving || bulkBusy || reviewingProposalId !== null || relation.created_by === user?.id} onClick={() => void reviewRelation(relation, "approve")}>{m("approve")}</Button>
                        <Button type="button" size="sm" disabled={saving || bulkBusy || reviewingProposalId !== null} variant="danger" onClick={() => void reviewRelation(relation, "reject")}>{m("reject")}</Button>
                      </>
                    ) : (
                      <Button type="button" size="sm" disabled={saving || bulkBusy || reviewingProposalId !== null} variant="secondary" onClick={() => void reviewRelation(relation, "retire")}>{m("deactivate")}</Button>
                    )}
                  </div>
                )}
              </article>
            ))}
          </div>
        )}
        <div className="border-t border-border p-4">
          <ListPagination
            page={relationPage}
            pageSize={LIST_PAGE_SIZE}
            total={relationTotal}
            onPageChange={changeRelationPage}
            disabled={loading || saving || reviewingProposalId !== null}
          />
        </div>
      </section>
      </div>

      <div id="knowledge-panel-proposals" role="tabpanel" aria-labelledby="knowledge-tab-proposals" hidden={activeTab !== "proposals"}>
      <section className="rounded-lg border border-border bg-bg-surface" aria-labelledby="knowledge-proposal-heading">
        {pendingProposalId && <p role="status" className="border-b border-border p-4 text-sm text-text-secondary">{tk("revisionPending")}</p>}
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border p-4">
          <div>
            <h2 id="knowledge-proposal-heading" className="font-semibold">{m("tabs.proposals")}</h2>
          </div>
          <div className="flex flex-wrap gap-2">
            <label className="text-sm text-text-secondary">
              <span className="sr-only">{m("statusFilter")}</span>
              <select
                aria-label={m("statusFilter")}
                value={proposalStateFilter}
                onChange={(event) => changeProposalStateFilter(event.target.value as ProposalStateFilter)}
                className="rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm"
              >
                <option value="all">{m("allStates")}</option>
                {PROPOSAL_STATE_OPTIONS.map((item) => <option key={item.value} value={item.value}>{m(`states.${item.value}`)}</option>)}
              </select>
            </label>
            <label className="text-sm text-text-secondary">
              <span className="sr-only">{m("originFilter")}</span>
              <select
                aria-label={m("originFilter")}
                value={proposalOriginFilter}
                onChange={(event) => changeProposalOriginFilter(event.target.value as ProposalOriginFilter)}
                className="rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm"
              >
                <option value="all">{m("allOrigins")}</option>
                {PROPOSAL_ORIGIN_OPTIONS.map((item) => <option key={item.value} value={item.value}>{m(`origins.${item.value}`)}</option>)}
              </select>
            </label>
          </div>
        </div>

        {isSuperAdmin && knowledgeExtensionsEnabled() && <KnowledgeProposalBulkReview
          selectedCount={selectedProposalIds.length}
          allSelected={proposals.length > 0 && proposals.every((item) => selectedProposalIds.includes(item.id))}
          reason={bulkReason}
          disabled={saving || proposalLoading || proposalError || bulkBusy || reviewingProposalId !== null}
          canApprove={!proposals.some((item) => selectedProposalIds.includes(item.id) && item.created_by === user?.id)}
          results={bulkResults}
          onSelectPage={(selected) => setSelectedProposalIds(selected ? proposals.map((item) => item.id) : [])}
          onReasonChange={setBulkReason}
          onReview={(action) => void reviewBulk(action)}
          onReload={() => void Promise.all([loadProposals(), loadRelations()])}
        />}
        {proposalLoading ? (
          <p role="status" className="p-8 text-center text-text-muted">{m("loading")}</p>
        ) : proposalError ? <div role="alert" className="flex flex-wrap items-center gap-3 p-4 text-sm text-accent-error">{tk("loadFailed")}<button type="button" onClick={() => void loadProposals()} className="text-accent-primary">{tk("retry")}</button></div> : proposals.length === 0 ? (
          <p className="p-8 text-center text-text-muted">{m("proposalsEmpty")}</p>
        ) : (
          <div className="divide-y divide-border">
            {proposals.map((proposal) => {
              const isOwnProposal = Boolean(user?.id && proposal.created_by === user.id);
              const isPendingReview = proposal.state === "pending_review";
              const canReview = isSuperAdmin && (isPendingReview || proposal.state === "approved");
              const isReviewing = reviewingProposalId === proposal.id;

              return (
                <article key={proposal.id} className="space-y-3 p-4">
                  {isSuperAdmin && knowledgeExtensionsEnabled() && <label className="inline-flex max-w-full items-start gap-2 break-words text-sm"><input type="checkbox" className="mt-1 shrink-0" disabled={saving || bulkBusy || reviewingProposalId !== null} checked={selectedProposalIds.includes(proposal.id)} onChange={(event) => setSelectedProposalIds((current) => event.target.checked ? [...current, proposal.id] : current.filter((id) => id !== proposal.id))} />{tk("selectProposal", { name: `${proposal.source.name} ${proposal.is_directed ? "→" : "↔"} ${proposal.target.name}` })}</label>}
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="break-words font-semibold">{proposal.source.name}</span>
                        <span className="text-text-muted">({m(`contentTypes.${proposal.source.type}`)})</span>
                        <span aria-label={m(proposal.is_directed ? "directedLabel" : "bidirectionalLabel")} className="text-accent-primary">
                          {proposal.is_directed ? "→" : "↔"}
                        </span>
                        <span className="break-words font-semibold">{proposal.target.name}</span>
                        <span className="text-text-muted">({m(`contentTypes.${proposal.target.type}`)})</span>
                      </div>
                      <p className="mt-1 text-xs text-text-muted">
                        {m(`relationTypes.${proposal.relation_type}`)} · {m("quality", { strength: Math.round(proposal.strength * 100), confidence: Math.round(proposal.confidence * 100) })} · {m(`origins.${proposal.origin}`)}
                      </p>
                    </div>
                    <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${proposalStateClass(proposal.state)}`}>
                      {m(`states.${proposal.state}`)}
                    </span>
                  </div>

                  <p className="rounded-lg bg-bg-elevated px-3 py-2 text-sm text-text-secondary">
                    <strong className="font-medium text-text-primary">{m("evidence")}:</strong> {proposal.evidence_summary || m("noEvidence")}
                  </p>

                  <dl className="grid gap-2 text-xs text-text-muted sm:grid-cols-2">
                    <div><dt className="font-medium text-text-secondary">{m("createdBy")}</dt><dd>{proposal.created_by ? proposal.created_by === user?.id ? m("you") : m("administrator") : m("system")}</dd></div>
                    <div><dt className="font-medium text-text-secondary">{m("createdAt")}</dt><dd>{formatDateTime(proposal.created_at)}</dd></div>
                  </dl>
                  {proposal.base_relation_id && (proposal.state === "draft" || proposal.state === "pending_review") && <p className="text-sm text-text-secondary">{m("pendingRevision")}</p>}

                  <details className="rounded-lg border border-border bg-bg-primary p-3">
                    <summary className="cursor-pointer font-medium text-text-primary">
                      {m("evidenceHistory", { evidence: proposal.evidence.length, reviews: proposal.reviews.length })}
                    </summary>
                    <div className="mt-3 space-y-4 text-sm text-text-secondary">
                      <div>
                        <h3 className="font-medium text-text-primary">{m("evidence")}</h3>
                        {proposal.evidence.length === 0 ? (
                          <p className="mt-1 text-text-muted">{m("noEvidence")}</p>
                        ) : (
                          <ul className="mt-2 space-y-2">
                            {proposal.evidence.map((evidence) => (
                              <li key={evidence.id} className="rounded-md border border-border bg-bg-surface p-3">
                                <p><span className="font-medium text-text-primary">[{evidence.evidence_kind}]</span> {evidence.summary}</p>
                                <dl className="mt-2 grid gap-1 text-xs text-text-muted sm:grid-cols-2">
                                  <div><dt className="inline font-medium text-text-secondary">{m("source")}:</dt>{" "}<dd className="inline">{evidence.source ? `${evidence.source.name} (${m(`contentTypes.${evidence.source.type}`)})` : m("noRecord")}</dd></div>
                                  <div><dt className="inline font-medium text-text-secondary">{m("generator")}:</dt>{" "}<dd className="inline">{evidence.generator_version || m("noRecord")}</dd></div>
                                  <div><dt className="inline font-medium text-text-secondary">{m("locator")}:</dt>{" "}<dd className="inline break-all">{evidence.locator || m("noRecord")}</dd></div>
                                  <div><dt className="inline font-medium text-text-secondary">{m("sourceDigest")}:</dt>{" "}<dd className="inline break-all">{evidence.source_digest || m("noRecord")}</dd></div>
                                </dl>
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>

                      <div>
                        <h3 className="font-medium text-text-primary">{m("reviewHistory")}</h3>
                        {proposal.reviews.length === 0 ? (
                          <p className="mt-1 text-text-muted">{m("noReviews")}</p>
                        ) : (
                          <ol className="mt-2 space-y-2 border-l border-border pl-4">
                            {proposal.reviews.map((review) => (
                              <li key={review.id}>
                                <p><span className="font-medium text-text-primary">{m(`reviewActions.${review.action}`)}</span> · {formatDateTime(review.created_at)}</p>
                                <p className="break-words text-xs text-text-secondary">{m("reviewer")}: {review.reviewer_id === user?.id ? m("you") : m("administrator")} · {m("reviewReason")}: {review.reason || m("noRecord")}</p>
                                <p className="mt-1 break-all text-xs text-text-muted">{review.reviewer_id}</p>
                              </li>
                            ))}
                          </ol>
                        )}
                      </div>
                    </div>
                  </details>

                  <details className="text-xs text-text-secondary"><summary className="cursor-pointer">{m("identifiers")}</summary><dl className="mt-2 space-y-1 break-all"><div><dt>{m("createdBy")}</dt><dd>{proposal.created_by || m("system")}</dd></div><div><dt>{m("generationRun")}</dt><dd>{proposal.generation_run_id || m("noRecord")}</dd></div><div><dt>{m("qualityVersion")}</dt><dd>{proposal.quality_version || m("noRecord")}</dd></div></dl></details>

                  {isPendingReview && isOwnProposal && (
                    <p id={`own-proposal-${proposal.id}`} role="note" className="rounded-lg bg-accent-warning/10 px-3 py-2 text-sm text-text-secondary">
                      {tk("ownApproval")}
                    </p>
                  )}

                  {canReview ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <label className="sr-only" htmlFor={`proposal-review-reason-${proposal.id}`}>{m("reviewReason")}</label>
                      <input
                        id={`proposal-review-reason-${proposal.id}`}
                        value={proposalReviewReasons[proposal.id] ?? ""}
                        onChange={(event) => setProposalReviewReasons((current) => ({ ...current, [proposal.id]: event.target.value }))}
                        placeholder={m("reviewReasonPlaceholder")}
                        disabled={bulkBusy || reviewingProposalId !== null}
                        maxLength={1000}
                        className="w-full min-w-0 flex-none rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm sm:w-auto sm:min-w-[min(100%,16rem)] sm:flex-1"
                      />
                      {isPendingReview ? (
                        <>
                          <Button
                            type="button"
                            size="sm"
                            disabled={isReviewing || isOwnProposal || bulkBusy || reviewingProposalId !== null}
                            aria-describedby={isOwnProposal ? `own-proposal-${proposal.id}` : undefined}
                            onClick={() => void reviewProposal(proposal, "approve")}
                          >
                            {m("approve")}
                          </Button>
                          <Button
                            type="button"
                            size="sm"
                            variant="danger"
                            disabled={isReviewing || bulkBusy || reviewingProposalId !== null}
                            onClick={() => void reviewProposal(proposal, "reject")}
                          >
                            {m("reject")}
                          </Button>
                        </>
                      ) : (
                        <Button
                          type="button"
                          size="sm"
                          variant="secondary"
                          disabled={isReviewing || bulkBusy || reviewingProposalId !== null}
                          onClick={() => void reviewProposal(proposal, "retire")}
                        >
                          {m("deactivate")}
                        </Button>
                      )}
                    </div>
                  ) : isPendingReview ? (
                    <p className="text-xs text-text-muted">{m("adminReviewOnly")}</p>
                  ) : null}
                </article>
              );
            })}
          </div>
        )}
        <div className="border-t border-border p-4">
          <ListPagination
            page={proposalPage}
            pageSize={LIST_PAGE_SIZE}
            total={proposalTotal}
            onPageChange={changeProposalPage}
            disabled={proposalLoading}
          />
        </div>
      </section>
      </div>

      <div id="knowledge-panel-automation" role="tabpanel" aria-labelledby="knowledge-tab-automation" hidden={activeTab !== "automation"}>
      <section className="rounded-lg border border-border bg-bg-surface" aria-labelledby="knowledge-candidate-run-heading">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border p-4">
          <div>
            <h2 id="knowledge-candidate-run-heading" className="font-semibold">{m("tabs.automation")}</h2>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {isSuperAdmin && (
              <>
                <Button
                  type="button"
                  size="sm"
                  variant="secondary"
                  disabled={candidateGenerationBusy}
                  onClick={() => void generateCandidates(true)}
                >
                  {m(candidateGenerationBusy ? "generating" : "previewCandidates")}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  disabled={candidateGenerationBusy}
                  onClick={() => void generateCandidates(false)}
                >
                  {m("generateCandidates")}
                </Button>
              </>
            )}
          </div>
          <label className="text-sm text-text-secondary">
            <span className="sr-only">{m("runStateFilter")}</span>
            <select
              aria-label={m("runStateFilter")}
              value={candidateRunStateFilter}
              onChange={(event) => changeCandidateRunFilter(event.target.value as CandidateRunStateFilter)}
              className="rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm"
            >
              <option value="all">{m("allStates")}</option>
              {CANDIDATE_RUN_STATE_OPTIONS.map((item) => <option key={item.value} value={item.value}>{m(`runStates.${item.value}`)}</option>)}
            </select>
          </label>
        </div>

        {candidatePreview && (
          <section className="m-4 border-b border-border pb-4" aria-live="polite">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h3 className="font-semibold text-text-primary">{m("previewCandidates")}</h3>
              </div>
              <span className="rounded-full bg-bg-surface px-2.5 py-1 text-xs font-medium text-text-secondary">
                {m("itemCount", { count: candidatePreview.length })}
              </span>
            </div>
            {candidatePreview.length === 0 ? (
              <p className="mt-3 text-sm text-text-muted">{m("candidatesEmpty")}</p>
            ) : (
              <ol className="mt-3 divide-y divide-border overflow-hidden rounded-lg border border-border bg-bg-surface">
                {candidatePreview.map((candidate, index) => {
                  const sharedTagCount = candidate.score_component.shared_tag_count;
                  return (
                    <li key={`${candidate.source.type}:${candidate.source.id}:${candidate.target.type}:${candidate.target.id}`} className="space-y-1 p-3 text-sm">
                      <p className="font-medium text-text-primary">
                        {index + 1}. {candidate.source.name} <span className="text-accent-primary">→</span> {candidate.target.name}
                      </p>
                      <p className="text-xs text-text-secondary">
                        {candidate.evidence_summary} · {m("quality", { strength: Math.round(candidate.strength * 100), confidence: Math.round(candidate.confidence * 100) })}
                        {typeof sharedTagCount === "number" ? ` · ${m("sharedTags", { count: sharedTagCount })}` : ""}
                      </p>
                    </li>
                  );
                })}
              </ol>
            )}
            {candidatePreviewTruncated && (
              <p className="mt-3 text-xs text-text-muted">{m("previewTruncated")}</p>
            )}
          </section>
        )}

        {candidateRunLoading ? (
          <p role="status" className="p-8 text-center text-text-muted">{m("loading")}</p>
        ) : candidateRunError ? <div role="alert" className="flex flex-wrap items-center gap-3 p-4 text-sm text-accent-error">{m("runsFailed")}<button type="button" onClick={() => void loadCandidateRuns()} className="text-accent-primary">{m("retry")}</button></div> : candidateRuns.length === 0 ? (
          <p className="p-8 text-center text-text-muted">{m("runsEmpty")}</p>
        ) : (
          <div className="divide-y divide-border">
            {candidateRuns.map((run) => (
              <article key={run.id} className="space-y-3 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="font-medium text-text-primary">{m(`runKinds.${run.run_kind}`)}</p>
                    <p className="mt-1 text-xs text-text-muted">{formatDateTime(run.created_at)} · {run.requested_by ? run.requested_by === user?.id ? m("you") : m("administrator") : m("system")}</p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${run.state === "succeeded" ? "bg-accent-success/15 text-accent-success" : run.state === "failed" ? "bg-accent-error/15 text-accent-error" : "bg-bg-elevated text-text-secondary"}`}>
                      {CANDIDATE_RUN_STATE_OPTIONS.some((item) => item.value === run.state) ? m(`runStates.${run.state}`) : run.state}
                    </span>
                    <span className="rounded-full bg-bg-elevated px-2.5 py-1 text-xs font-medium text-text-secondary">
                      {m(run.is_dry_run ? "previewOnly" : "savedCandidates")}
                    </span>
                  </div>
                </div>
                <dl className="grid gap-2 text-xs text-text-muted sm:grid-cols-2 lg:grid-cols-5">
                  <div><dt className="font-medium text-text-secondary">{m("inputCount")}</dt><dd>{run.input_count}</dd></div>
                  <div><dt className="font-medium text-text-secondary">{m("requestedLimit")}</dt><dd>{run.requested_max_candidates ?? m("noRecord")}</dd></div>
                  <div><dt className="font-medium text-text-secondary">{m("beforeLimit")}</dt><dd>{run.candidate_count_before_limit}{run.preview_truncated ? ` (${m("limited")})` : ""}</dd></div>
                  <div><dt className="font-medium text-text-secondary">{m("outputCount")}</dt><dd>{run.output_count}</dd></div>
                  <div><dt className="font-medium text-text-secondary">{m("duplicateCount")}</dt><dd>{run.duplicate_count}</dd></div>
                  <div><dt className="font-medium text-text-secondary">{m("errorCount")}</dt><dd>{run.error_count}</dd></div>
                  <div><dt className="font-medium text-text-secondary">{m("inputFormat")}</dt><dd>{run.input_snapshot_schema_version}</dd></div>
                </dl>
                <details className="rounded-lg border border-border bg-bg-primary p-3 text-xs text-text-secondary">
                  <summary className="cursor-pointer font-medium text-text-primary">{m("identifiers")}</summary>
                  <dl className="mt-3 space-y-2 break-all">
                    <div><dt className="font-medium">{m("generationRun")}</dt><dd>{run.id}</dd></div>
                    <div><dt className="font-medium">{m("createdBy")}</dt><dd>{run.requested_by || m("system")}</dd></div>
                    <div><dt className="font-medium">{m("generator")}</dt><dd>{run.algorithm_version}</dd></div>
                    <div><dt className="font-medium">{m("configurationDigest")}</dt><dd>{run.configuration_digest}</dd></div>
                    <div><dt className="font-medium">{m("inputDigest")}</dt><dd>{run.input_snapshot_digest}</dd></div>
                    <div><dt className="font-medium">{m("requestedLimit")}</dt><dd>{String(run.execution_options.max_candidates ?? m("noRecord"))}</dd></div>
                    <div><dt className="font-medium">{m("snapshotCount")}</dt><dd>{run.input_snapshot.length}</dd></div>
                    <div><dt className="font-medium">{m("startedFinished")}</dt><dd>{formatDateTime(run.started_at)} / {formatDateTime(run.finished_at)}</dd></div>
                    {run.error_code && <div><dt className="font-medium">{m("errorCode")}</dt><dd>{run.error_code}</dd></div>}
                  </dl>
                </details>
              </article>
            ))}
          </div>
        )}
        <div className="border-t border-border p-4">
          <ListPagination
            page={candidateRunPage}
            pageSize={LIST_PAGE_SIZE}
            total={candidateRunTotal}
            onPageChange={changeCandidateRunPage}
            disabled={candidateRunLoading}
          />
        </div>
      </section>
      </div>
    </div>
  );
}
