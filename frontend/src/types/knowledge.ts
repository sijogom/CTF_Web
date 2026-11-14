/** 지식 태그 */
export interface KnowledgeTag {
  id: number;
  name: string;
  slug: string;
  parent_id?: number | null;
  color?: string | null;
  description?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ContentTagMappingEntry {
  source_relation_id?: string | null;
  tag_id: number;
  direction: "content_to_knowledge_tag";
  strength: number;
  confidence: number;
  evidence_summary: string;
  origin: "manual" | "import" | "rule" | "model_candidate";
  created_by?: string | null;
  created_at?: string | null;
  approved_at?: string | null;
}

export interface ContentTagMappingResponse {
  mapping_revision: string;
  entity_type: "challenge" | "course" | "lesson";
  entity_id: string;
  tag_ids: number[];
  mappings: ContentTagMappingEntry[];
}

export interface KnowledgeTagUnmappedContent {
  entity_type: "challenge" | "course" | "lesson";
  entity_id: string;
  name: string;
  href: string;
  course_id?: string | null;
}

export interface KnowledgeTagReconcileResponse {
  dry_run: boolean;
  scanned: number;
  candidates: number;
  created: number;
  existing: number;
  skipped: number;
  by_entity: Record<string, number>;
}

export interface ContentTagMappingAuditEvent {
  id: string;
  entity_type: "challenge" | "course" | "lesson";
  entity_id: string;
  tag_id: number;
  action: "attach" | "detach";
  actor_id: string;
  mapping_snapshot: Record<string, unknown>;
  created_at: string;
}

/** 학습 선행조건 (API 응답) */
export interface LearningPrerequisite {
  type: "course" | "lesson";
  id: string;
  name: string;
  completed: boolean;
  course_id?: string;
}

/** 챌린지 선행조건 상세 */
export interface PrerequisiteDetail {
  id: number;
  name: string;
  solved: boolean;
}

/** 태그 기반 추천 선행조건 */
export interface RecommendedPrerequisite {
  type: "course" | "lesson" | "challenge";
  id: string;
  name: string;
  course_id?: string;
}

/** V1 학습 경로 투영은 기존 선행조건을 바꾸지 않는 읽기 전용 설명 응답이다. */
export type LearningPathAccessState = "available" | "locked" | "sign_in_required";
export type LearningPathStepStatus = "completed" | "in_progress" | "not_started" | "unavailable" | "unknown";
export type LearningPathContentType = "challenge" | "course" | "lesson" | "knowledge_tag" | "hint";

export interface LearningPathTarget {
  type: "challenge";
  id: string;
  name: string;
  href: string | null;
}

export interface LearningPathGraphFocus {
  focus_type: LearningPathContentType;
  focus_id: string;
}

export interface LearningPathStep {
  type: "challenge" | "course" | "lesson";
  id: string;
  name: string;
  status: LearningPathStepStatus;
  completed_units?: number | null;
  total_units?: number | null;
  href: string;
  graph_focus: LearningPathGraphFocus;
  course_id?: string | null;
}

/** 승인된 지식 관계에서 파생한 선택형 도움 콘텐츠. 필수 선행조건이 아니다. */
export interface LearningPathHelpfulContent {
  type: LearningPathContentType;
  id: string;
  name: string;
  status: LearningPathStepStatus;
  href: string;
  graph_focus: LearningPathGraphFocus;
  course_id?: string | null;
  relation_type: "prepares_for" | "recommends" | "helps";
  strength: number;
  confidence: number;
  evidence_summary?: string | null;
  origin: string;
}

export interface LearningPathNextAction {
  kind: string;
  href: string | null;
}

export interface LearningPathProjection {
  target: LearningPathTarget;
  access_state: LearningPathAccessState;
  reason_code?: string | null;
  required_steps: LearningPathStep[];
  recommended_steps: LearningPathStep[];
  helpful_content: LearningPathHelpfulContent[];
  next_action?: LearningPathNextAction | null;
  map_focus_href?: string | null;
}

/** 그래프 노드 */
export interface GraphNode {
  id: string;
  type: "challenge" | "course" | "lesson" | "knowledge_tag" | "hint";
  name: string;
  tags: string[];
  status: "solved" | "completed" | "available" | "locked" | "unknown";
  entity_id?: string | null;
  href?: string | null;
  course_id?: string | null;
}

/** 그래프 엣지 */
export interface GraphEdge {
  source: string;
  target: string;
  type: string;
  is_directed?: boolean;
  strength?: number;
  confidence?: number;
  evidence_summary?: string | null;
  origin?: string | null;
}

/** 그래프 응답 */
export interface KnowledgeGraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

/**
 * V2 지식 맵은 전체 노드를 브라우저로 보내지 않는다. 서버가 현재 열람 권한을
 * 적용한 뒤 선택 콘텐츠 중심의 부분 그래프만 반환한다.
 */
export type KnowledgeMapNodeType =
  | "challenge"
  | "course"
  | "lesson"
  | "knowledge_tag"
  | "hint"
  | "event"
  | "assignment";

export interface KnowledgeMapV2Node {
  id: string;
  type: KnowledgeMapNodeType;
  entity_id: string;
  name: string;
  href: string | null;
  is_focus: boolean;
  distance: number;
}

export interface KnowledgeMapV2Edge {
  id: string;
  source: string;
  target: string;
  relation_type: string;
  is_directed: boolean;
  strength: number;
  confidence: number;
  evidence_summary: string | null;
  origin: string;
  created_at: string | null;
  approved_at: string | null;
}

export interface KnowledgeMapV2MappingCoverage {
  public_content: Partial<Record<Extract<KnowledgeMapNodeType, "challenge" | "course" | "lesson">, number>>;
  tagged_content: Partial<Record<Extract<KnowledgeMapNodeType, "challenge" | "course" | "lesson">, number>>;
  unmapped_content: Partial<Record<Extract<KnowledgeMapNodeType, "challenge" | "course" | "lesson">, number>>;
  coverage_percent: Partial<Record<Extract<KnowledgeMapNodeType, "challenge" | "course" | "lesson">, number>>;
}

export interface KnowledgeMapV2Overview {
  graph_revision: string;
  node_counts: Record<KnowledgeMapNodeType, number>;
  edge_counts: Record<string, number>;
  relation_count: number;
  default_focus?: KnowledgeMapV2Node | null;
  mapping_coverage?: KnowledgeMapV2MappingCoverage;
}

export interface KnowledgeMapV2Focus {
  next_cursor?: string | null;
  graph_revision: string;
  focus: KnowledgeMapV2Node;
  nodes: KnowledgeMapV2Node[];
  edges: KnowledgeMapV2Edge[];
  layout_hints: {
    strategy: "seeded_force_v1";
    seed: string;
    distance_by_node: Record<string, number>;
    strength_distance_rule: "stronger_relation_shorter";
    direction_visible: boolean;
  };
  truncated: boolean;
  omitted_counts: Record<string, number>;
  continuation: string | null;
  applied_filters: {
    types: string[];
    relation_types: string[];
    depth: number;
  };
}

export interface KnowledgeMapV2Canvas {
  graph_revision: string;
  nodes: KnowledgeMapV2Node[];
  edges: KnowledgeMapV2Edge[];
  layout_hints: KnowledgeMapV2Focus["layout_hints"];
  truncated: boolean;
  omitted_counts: Record<string, number>;
  applied_filters: {
    types: string[];
    relation_types: string[];
    mode: "canvas";
  };
}

export interface KnowledgeMapV2SearchItem {
  type: KnowledgeMapNodeType;
  /** 원본 콘텐츠의 안정적인 식별자. 검색 UI가 복합 그래프 노드 ID를 추측하지 않도록 분리한다. */
  entity_id: string;
  id: string;
  name: string;
  href: string | null;
}

export interface KnowledgeMapV2Search {
  query: string;
  items: KnowledgeMapV2SearchItem[];
  total: number;
  limit: number;
}

/** 관리자 지식 관계 원장 */
export type KnowledgeContentType =
  | "challenge"
  | "course"
  | "lesson"
  | "knowledge_tag"
  | "hint"
  | "event"
  | "assignment";
export type KnowledgeRelationType =
  | "requires"
  | "prepares_for"
  | "recommends"
  | "helps"
  | "related"
  | "similar"
  | "tag_affinity";
export type KnowledgeRelationStatus = "draft" | "pending_review" | "approved" | "rejected" | "retired";

export interface KnowledgeContentRef {
  type: KnowledgeContentType;
  id: string;
}

export interface KnowledgeContentSummary extends KnowledgeContentRef {
  name: string;
  is_active: boolean;
  href?: string | null;
  course_id?: string | null;
}

export interface KnowledgeContentList {
  items: KnowledgeContentSummary[];
  total: number;
  offset: number;
  limit: number;
}

export interface KnowledgeRelation {
  id: string;
  source: KnowledgeContentSummary;
  target: KnowledgeContentSummary;
  relation_type: KnowledgeRelationType;
  is_directed: boolean;
  strength: number;
  confidence: number;
  origin: "manual" | "rule" | "model_candidate" | "import" | "observation";
  evidence_summary?: string | null;
  evidence: Record<string, unknown>;
  quality_version?: string | null;
  status: KnowledgeRelationStatus;
  created_by: string;
  reviewed_by?: string | null;
  approved_at?: string | null;
  retired_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeRelationList {
  items: KnowledgeRelation[];
  total: number;
  offset: number;
  limit: number;
}

/** V2 지식 관계 제안은 공개 관계와 분리된 검토 원장이다. */
export type KnowledgeV2RelationType = KnowledgeRelationType | "prepares_for";
export type KnowledgeProposalOrigin = "manual" | "rule" | "model_candidate" | "import" | "observation";
export type KnowledgeProposalState = "draft" | "pending_review" | "approved" | "rejected" | "retired" | "superseded";

export interface KnowledgeProposalEvidence {
  id: string;
  evidence_kind: "manual" | "rule" | "model" | "metadata" | "reference";
  source?: KnowledgeContentSummary | null;
  source_digest?: string | null;
  locator?: string | null;
  summary: string;
  score_component?: Record<string, unknown> | null;
  generator_version?: string | null;
  created_at: string;
}

export interface KnowledgeProposalReview {
  id: string;
  reviewer_id: string;
  action: "submitted" | "approved" | "rejected" | "retired";
  reason?: string | null;
  created_at: string;
}

export interface KnowledgeRelationProposal {
  id: string;
  base_relation_id?: string | null;
  published_relation_id?: string | null;
  source: KnowledgeContentSummary;
  target: KnowledgeContentSummary;
  relation_type: KnowledgeV2RelationType;
  is_directed: boolean;
  strength: number;
  confidence: number;
  origin: KnowledgeProposalOrigin;
  evidence_summary?: string | null;
  quality_version?: string | null;
  generation_run_id?: string | null;
  state: KnowledgeProposalState;
  content_revision: string;
  created_by?: string | null;
  reviewed_by?: string | null;
  approved_at?: string | null;
  retired_at?: string | null;
  created_at: string;
  updated_at: string;
  evidence: KnowledgeProposalEvidence[];
  reviews: KnowledgeProposalReview[];
}

export interface KnowledgeProposalList {
  items: KnowledgeRelationProposal[];
  total: number;
  offset: number;
  limit: number;
}

export interface KnowledgeCandidateRun {
  id: string;
  run_kind: "rule" | "model" | "import";
  algorithm_version: string;
  configuration_digest: string;
  input_snapshot_digest: string;
  input_snapshot_schema_version: string;
  execution_options: Record<string, unknown>;
  input_snapshot: Array<Record<string, unknown>>;
  requested_by: string | null;
  requested_max_candidates: number | null;
  state: string;
  is_dry_run: boolean;
  started_at?: string | null;
  finished_at?: string | null;
  input_count: number;
  candidate_count_before_limit: number;
  preview_truncated: boolean;
  output_count: number;
  duplicate_count: number;
  error_count: number;
  error_code?: string | null;
  created_at: string;
}

export interface KnowledgeCandidateRunList {
  items: KnowledgeCandidateRun[];
  total: number;
  offset: number;
  limit: number;
}

export interface KnowledgeCandidatePreview {
  source: KnowledgeContentSummary;
  target: KnowledgeContentSummary;
  relation_type: "prepares_for" | "helps" | "tag_affinity";
  strength: number;
  confidence: number;
  evidence_summary: string;
  score_component: Record<string, unknown>;
}

export interface KnowledgeCandidateGenerationResult {
  run: KnowledgeCandidateRun;
  preview: KnowledgeCandidatePreview[];
  preview_truncated: boolean;
}
