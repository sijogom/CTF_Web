"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  Background,
  BackgroundVariant,
  ControlButton,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
  type OnSelectionChangeFunc,
  type ReactFlowInstance,
  type Viewport,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useTranslations } from "next-intl";
import { X, LockKeyhole, BookOpen, FileText, Flag, Tag, Lightbulb, CalendarDays, ClipboardList, Maximize2, LayoutGrid, Undo2 } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useRouter } from "@/i18n/navigation";
import { apiFetch, ApiError } from "@/lib/api-client";
import { writeUrlQuery } from "@/lib/url-query";
import { MAP_CONTENT_TYPES, DEFAULT_MAP_RELATIONS, readMapToolsState, mergeMapPage, internalContentHref } from "@/lib/map-ui-state.mjs";
import { createKnowledgeMapPositions, fitKnowledgeMapViewport } from "@/lib/knowledge-map-layout.mjs";
import {
  KNOWLEDGE_MAP_POSITION_STORAGE_KEY as POSITION_STORAGE_KEY,
  KNOWLEDGE_MAP_POSITION_LAYOUT_VERSION as POSITION_LAYOUT_VERSION,
  KNOWLEDGE_MAP_VIEWPORT_STORAGE_KEY as VIEWPORT_STORAGE_KEY,
  KNOWLEDGE_MAP_VIEWPORT_LAYOUT_VERSION as VIEWPORT_LAYOUT_VERSION,
  KNOWLEDGE_MAP_MAX_STORED_POSITIONS as MAX_STORED_POSITIONS,
  KNOWLEDGE_MAP_MAX_STORED_VIEWPORTS as MAX_STORED_VIEWPORTS,
  knowledgeMapPositionKey as positionMemoryKey,
  knowledgeMapViewportKey as viewportMemoryKey,
  persistKnowledgeMapLayout,
  recoverKnowledgeMapLayout,
  type KnowledgeMapLayoutRecovery,
} from "@/lib/knowledge-map-arrangement.mjs";
import { KnowledgeMapTools } from "@/components/learn/KnowledgeMapTools";
import { Button } from "@/components/ui/Button";
import type {
  KnowledgeMapNodeType,
  KnowledgeMapV2Edge,
  KnowledgeMapV2Focus,
  KnowledgeMapV2Node,
  KnowledgeMapV2Overview,
  KnowledgeMapV2Search,
} from "@/types/knowledge";

type PagedFocus = KnowledgeMapV2Focus & { next_cursor?: string | null };

type MapFlowNodeData = {
  mapNode: KnowledgeMapV2Node;
  nodeType: KnowledgeMapNodeType;
  typeLabel: string;
  distanceLabel: string;
  connectionCount: number;
  connectionLabel: string;
};

type MapFlowEdgeData = {
  mapEdge: KnowledgeMapV2Edge;
};

type MapFlowNode = Node<MapFlowNodeData>;
type MapFlowEdge = Edge<MapFlowEdgeData>;

type UserSelectionIntent = {
  targetKind: "node" | "edge" | "selection";
  targetId: string | null;
  expectedTargetSelected: boolean | null;
  routeFocusKey: string | null;
  graphFocusId: string | null;
  graphRequestId: number;
};

export type KnowledgeGraphInitialFocus = {
  type: KnowledgeMapNodeType;
  id: string;
};

interface KnowledgeGraphMapProps {
  initialFocus?: KnowledgeGraphInitialFocus | null;
  onFocusChange?: (focus: KnowledgeGraphInitialFocus) => void;
}

// The map is a learning-content map. Operational surfaces such as events,
// assignments, and hints are intentionally outside this focused public view;
// the canvas must show only the selected content's approved public neighborhood.
const NODE_STYLE: Record<KnowledgeMapNodeType, { border: string }> = {
  challenge: { border: "#EA580C" },
  course: { border: "#2563EB" },
  lesson: { border: "#16A34A" },
  knowledge_tag: { border: "#9333EA" },
  hint: { border: "#CA8A04" },
  event: { border: "#0891B2" },
  assignment: { border: "#E11D48" },
};

const NODE_ICON = {
  challenge: Flag,
  course: BookOpen,
  lesson: FileText,
  knowledge_tag: Tag,
  hint: Lightbulb,
  event: CalendarDays,
  assignment: ClipboardList,
};

const RELATION_STYLE: Record<string, { color: string; dash?: string; baseOpacity?: number; widthMultiplier?: number }> = {
  requires: { color: "#DC2626" },
  prepares_for: { color: "#2563EB" },
  helps: { color: "#0F766E" },
  recommends: { color: "#7C3AED", dash: "6 4" },
  related: { color: "#4B5563", dash: "4 3" },
  similar: { color: "#6B7280", dash: "2 4" },
  // Tag mappings remain visible, but sit behind learning-path edges so a
  // content hub does not become a purple tangle when several tags are shared.
  tag_affinity: { color: "#9333EA", dash: "5 4", baseOpacity: 0.22, widthMultiplier: 0.62 },
  contains: { color: "#15803D" },
  tag_parent: { color: "#A16207", dash: "3 3" },
};

const CANVAS_ARIA_LABEL = "knowledge-map-canvas";

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}


function relationMeta(relationType: string, label: string) {
  return {
    ...(RELATION_STYLE[relationType] ?? {
      color: "#4B5563",
      dash: "4 3",
    }),
    label,
  };
}

function relationExplanation(edge: KnowledgeMapV2Edge, relationLabel: (relationType: string) => string): string {
  return edge.evidence_summary?.trim() || relationLabel(edge.relation_type);
}

function sortedValues(values: Iterable<string>): string[] {
  return [...values].sort();
}

function sameValues(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function currentGraphMatchesRequest(
  graph: KnowledgeMapV2Focus,
  focus: KnowledgeMapV2Node,
  depth: number,
  contentTypes: readonly string[],
  relationTypes: readonly string[],
): boolean {
  return graph.focus.id === focus.id
    && graph.applied_filters.depth === depth
    && sameValues(sortedValues(graph.applied_filters.types), sortedValues(contentTypes))
    && sameValues(sortedValues(graph.applied_filters.relation_types), sortedValues(relationTypes));
}

function createPositions(graph: KnowledgeMapV2Focus): Map<string, { x: number; y: number }> {
  return createKnowledgeMapPositions(graph, {
    compact: typeof window !== "undefined" && window.innerWidth < 1024,
  });
}

type CanvasPosition = { x: number; y: number };
type LayoutUndo = {
  graph: PagedFocus;
  requestId: number;
  positions: Map<string, CanvasPosition>;
  savedPositions: Map<string, CanvasPosition | null>;
  viewport: Viewport;
};

// Existing manually dragged coordinates stay valid after the default layout
// changes; only nodes without a saved position use the compact arrangement.
function readPositionMemory(): Map<string, CanvasPosition> {
  if (typeof window === "undefined") return new Map();
  try {
    const raw = window.localStorage.getItem(POSITION_STORAGE_KEY);
    if (!raw) return new Map();
    const stored = JSON.parse(raw) as {
      version?: string;
      entries?: Record<string, CanvasPosition>;
    } | Record<string, CanvasPosition>;
    // The key is retained for backward compatibility with the existing
    // contract, but old radial/random coordinates are deliberately ignored.
    if (stored && typeof stored === "object" && "version" in stored) {
      if (stored.version !== POSITION_LAYOUT_VERSION || !stored.entries) return new Map();
    } else {
      return new Map();
    }
    const parsed = stored.entries ?? {};
    return new Map(
      Object.entries(parsed).filter(([, value]) => (
        Number.isFinite(value?.x) && Number.isFinite(value?.y)
      )),
    );
  } catch {
    return new Map();
  }
}

function writePositionMemory(memory: Map<string, CanvasPosition>): void {
  if (typeof window === "undefined") return;
  try {
    const entries = [...memory.entries()].slice(-MAX_STORED_POSITIONS);
    window.localStorage.setItem(POSITION_STORAGE_KEY, JSON.stringify({
      version: POSITION_LAYOUT_VERSION,
      entries: Object.fromEntries(entries),
    }));
  } catch {
    // A blocked or full localStorage must not make the graph unusable.
  }
}

function isStoredViewport(value: unknown): value is Viewport {
  if (!value || typeof value !== "object") return false;
  const viewport = value as Partial<Viewport>;
  return Number.isFinite(viewport.x)
    && Number.isFinite(viewport.y)
    && Number.isFinite(viewport.zoom)
    && (viewport.zoom ?? 0) >= 0.05
    && (viewport.zoom ?? 0) <= 2.2;
}

function readViewportMemory(): Map<string, Viewport> {
  if (typeof window === "undefined") return new Map();
  try {
    const raw = window.localStorage.getItem(VIEWPORT_STORAGE_KEY);
    if (!raw) return new Map();
    const stored = JSON.parse(raw) as { version?: string; entries?: Record<string, unknown> };
    if (stored.version !== VIEWPORT_LAYOUT_VERSION || !stored.entries) return new Map();
    return new Map(
      Object.entries(stored.entries).filter((entry): entry is [string, Viewport] => (
        isStoredViewport(entry[1])
      )),
    );
  } catch {
    return new Map();
  }
}

function writeViewportMemory(memory: Map<string, Viewport>): void {
  if (typeof window === "undefined") return;
  try {
    const entries = [...memory.entries()].slice(-MAX_STORED_VIEWPORTS);
    window.localStorage.setItem(VIEWPORT_STORAGE_KEY, JSON.stringify({
      version: VIEWPORT_LAYOUT_VERSION,
      entries: Object.fromEntries(entries),
    }));
  } catch {
    // A blocked or full localStorage must not make the graph unusable.
  }
}

type MapHandleSide = "top" | "right" | "bottom" | "left";

const HANDLE_POSITION: Record<MapHandleSide, Position> = {
  top: Position.Top,
  right: Position.Right,
  bottom: Position.Bottom,
  left: Position.Left,
};

const HANDLE_SIDES: readonly MapHandleSide[] = ["top", "right", "bottom", "left"];

function oppositeHandleSide(side: MapHandleSide): MapHandleSide {
  if (side === "top") return "bottom";
  if (side === "right") return "left";
  if (side === "bottom") return "top";
  return "right";
}

function handleSidesForEdge(
  source: CanvasPosition,
  target: CanvasPosition,
): { sourceSide: MapHandleSide; targetSide: MapHandleSide } {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  if (Math.abs(dx) >= Math.abs(dy)) {
    const sourceSide: MapHandleSide = dx >= 0 ? "right" : "left";
    return { sourceSide, targetSide: oppositeHandleSide(sourceSide) };
  }
  const sourceSide: MapHandleSide = dy >= 0 ? "bottom" : "top";
  return { sourceSide, targetSide: oppositeHandleSide(sourceSide) };
}

function resolveCanvasPositions(
  graph: KnowledgeMapV2Focus,
  positionMemory: Map<string, CanvasPosition>,
): Map<string, CanvasPosition> {
  const fallbackPositions = createPositions(graph);
  return new Map(graph.nodes.map((node) => [
    node.id,
    positionMemory.get(positionMemoryKey(graph, node.id))
      ?? fallbackPositions.get(node.id)
      ?? { x: 0, y: 0 },
  ]));
}

function KnowledgeCanvasNode({ data, selected }: NodeProps<MapFlowNode>) {
  const style = NODE_STYLE[data.nodeType];
  const Icon = NODE_ICON[data.nodeType];
  const mapNode = data.mapNode;
  const accessibleSummary = [
    `${data.typeLabel}: ${mapNode.name}`,
    data.distanceLabel,
    data.connectionLabel,
  ].filter(Boolean).join(". ");
  const accessibleState = [data.distanceLabel, data.connectionLabel].filter(Boolean).join(". ");
  return (
    <div
      className="knowledge-map-canvas-node relative min-h-[116px] w-[148px] rounded-lg border bg-bg-surface px-3 py-2.5 text-left transition-shadow lg:w-[224px]"
      style={{
        borderColor: style.border,
        borderWidth: 1.5,
        boxShadow: selected ? `0 0 0 2px ${style.border}55, 0 2px 6px rgba(15, 23, 42, 0.1)` : "0 1px 4px rgba(15, 23, 42, 0.08)",
      }}
      aria-label={accessibleSummary}
    >
      {HANDLE_SIDES.map((side) => (
        <Handle
          key={`target-${side}`}
          id={`target-${side}`}
          type="target"
          position={HANDLE_POSITION[side]}
          isConnectable={false}
          style={{ opacity: 0, pointerEvents: "none" }}
        />
      ))}
      <div className="flex items-start gap-1.5 text-[11px] leading-4 text-text-secondary">
        <Icon aria-hidden="true" className="mt-px h-3.5 w-3.5 shrink-0" style={{ color: style.border }} />
        <span className="min-w-0 break-words">{data.typeLabel}{mapNode.is_focus ? "" : ` · ${data.distanceLabel}`}</span>
      </div>
      <p className="mt-2 line-clamp-3 break-words text-sm font-semibold leading-5 text-text-primary lg:line-clamp-2" title={mapNode.name}>
        {mapNode.name}
      </p>
      {accessibleState && <div className="sr-only" aria-live="polite">{accessibleState}</div>}
      {HANDLE_SIDES.map((side) => (
        <Handle
          key={`source-${side}`}
          id={`source-${side}`}
          type="source"
          position={HANDLE_POSITION[side]}
          isConnectable={false}
          style={{ opacity: 0, pointerEvents: "none" }}
        />
      ))}
    </div>
  );
}

const FLOW_NODE_TYPES = { knowledge: KnowledgeCanvasNode };

function graphToFlowNodes(
  graph: KnowledgeMapV2Focus,
  nodeLabel: (nodeType: KnowledgeMapNodeType) => string,
  describeDistance: (distance: number) => string,
  connectionLabel: (count: number) => string,
  positionMemory: Map<string, CanvasPosition>,
  selectedNodeId: string | null = graph.focus.id,
  positions: Map<string, CanvasPosition> = resolveCanvasPositions(graph, positionMemory),
): MapFlowNode[] {
  const connectionCounts = new Map<string, number>();
  for (const edge of graph.edges) {
    connectionCounts.set(edge.source, (connectionCounts.get(edge.source) ?? 0) + 1);
    connectionCounts.set(edge.target, (connectionCounts.get(edge.target) ?? 0) + 1);
  }
  return graph.nodes.map((mapNode) => {
    const position = positions.get(mapNode.id) ?? { x: 0, y: 0 };
    const connectionCount = connectionCounts.get(mapNode.id) ?? 0;
    return {
      id: mapNode.id,
      type: "knowledge",
      selected: mapNode.id === selectedNodeId,
      position,
      data: {
        mapNode,
        nodeType: mapNode.type,
        typeLabel: nodeLabel(mapNode.type),
        distanceLabel: mapNode.is_focus ? "" : describeDistance(mapNode.distance),
        connectionCount,
        connectionLabel: connectionLabel(connectionCount),
      },
      ariaLabel: [
        `${nodeLabel(mapNode.type)}: ${mapNode.name}`,
        mapNode.is_focus ? "" : describeDistance(mapNode.distance),
      ].filter(Boolean).join(". "),
    };
  });
}

function graphToFlowEdges(
  graph: KnowledgeMapV2Focus,
  relationLabel: (relationType: string) => string,
  describeEdge: (label: string, strength: number, confidence: number) => string,
  positions: Map<string, CanvasPosition>,
): MapFlowEdge[] {
  return graph.edges.map((mapEdge) => {
    const style = relationMeta(mapEdge.relation_type, relationLabel(mapEdge.relation_type));
    const strength = clamp(mapEdge.strength, 0, 1);
    const sourcePosition = positions.get(mapEdge.source) ?? { x: 0, y: 0 };
    const targetPosition = positions.get(mapEdge.target) ?? { x: 0, y: 0 };
    const { sourceSide, targetSide } = handleSidesForEdge(sourcePosition, targetPosition);
    return {
      id: mapEdge.id,
      source: mapEdge.source,
      target: mapEdge.target,
      sourceHandle: `source-${sourceSide}`,
      targetHandle: `target-${targetSide}`,
      type: "default",
      pathOptions: { curvature: 0.18 },
      data: { mapEdge },
      // 관계명은 모든 엣지에 상시 그리지 않는다. 허브 그래프에서 라벨이
      // 카드와 서로 겹히는 대신, 엣지 클릭 시 우측 상세 패널에서 읽는다.
      style: {
        stroke: style.color,
        strokeWidth: (1.25 + strength * 0.75) * (style.widthMultiplier ?? 1),
        strokeOpacity: (style.baseOpacity ?? 0.4) + mapEdge.confidence * (1 - (style.baseOpacity ?? 0.4)),
        strokeDasharray: style.dash,
      },
      markerEnd: mapEdge.is_directed
        ? { type: MarkerType.ArrowClosed, color: style.color, width: 16, height: 16 }
        : undefined,
      ariaLabel: describeEdge(style.label, strength, clamp(mapEdge.confidence, 0, 1)),
    };
  });
}

export function KnowledgeGraphMap({
  initialFocus = null,
  onFocusChange,
}: KnowledgeGraphMapProps) {
  const router = useRouter();
  const t = useTranslations("knowledgeMap");
  const searchParams = useSearchParams();
  const routeTools = readMapToolsState(searchParams.toString());
  const routeToolsKey = JSON.stringify(routeTools);
  const hasExplicitRelationFilters = searchParams.has("map_relations");
  const [tools, setTools] = useState(routeTools);
  const [searchInput, setSearchInput] = useState(routeTools.query);
  const [searchResult, setSearchResult] = useState<KnowledgeMapV2Search | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState(false);
  const [searchRetry, setSearchRetry] = useState(0);
  const [pageError, setPageError] = useState(false);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [overview, setOverview] = useState<KnowledgeMapV2Overview | null>(null);
  const [focus, setFocus] = useState<KnowledgeMapV2Node | null>(() => (
    initialFocus
      ? {
        id: `${initialFocus.type}:${initialFocus.id}`,
        type: initialFocus.type,
        entity_id: initialFocus.id,
        name: initialFocus.id,
        href: null,
        is_focus: true,
        distance: 0,
      }
      : null
  ));
  const [graph, setGraph] = useState<PagedFocus | null>(null);
  const graphRef = useRef<PagedFocus | null>(null);
  const [nodes, setNodes, onNodesChange] = useNodesState<MapFlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<MapFlowEdge>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<KnowledgeMapV2Edge | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);
  // 처음에는 선택한 콘텐츠에 직접 연결된 공개 관계만 펼친다. 먼 관계는
  // 별도 목록을 늘어놓지 않고 사용자가 노드를 선택해 새 중심으로 이동할
  // 때 같은 1-hop 캔버스를 다시 요청한다.
  const depth = 1;
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [overviewError, setOverviewError] = useState(false);
  const [graphLoading, setGraphLoading] = useState(false);
  const [graphError, setGraphError] = useState(false);
  const [overviewRetry, setOverviewRetry] = useState(0);
  const [flowReady, setFlowReady] = useState(false);
  const [layoutUndo, setLayoutUndo] = useState<LayoutUndo | null>(null);
  const [layoutBusy, setLayoutBusy] = useState(false);
  const [layoutSaveError, setLayoutSaveError] = useState<"save" | "recovery" | null>(null);
  const graphRequestRef = useRef<{ id: number; controller: AbortController | null }>({ id: 0, controller: null });
  const graphDataRequestIdRef = useRef(0);
  const userSelectionIntentRef = useRef<UserSelectionIntent | null>(null);
  const selectedNodeIdRef = useRef<string | null>(null);
  const positionMemoryRef = useRef<Map<string, CanvasPosition>>(new Map());
  const viewportMemoryRef = useRef<Map<string, Viewport>>(new Map());
  const flowInstanceRef = useRef<ReactFlowInstance<MapFlowNode, MapFlowEdge> | null>(null);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const selectionPanelRef = useRef<HTMLDivElement | null>(null);
  const restoredViewportKeyRef = useRef<string | null>(null);
  const automaticViewportRef = useRef(true);
  const compactLayoutRef = useRef<boolean | null>(null);
  const resizeFrameRef = useRef<number | null>(null);
  const layoutBusyRef = useRef(false);
  const layoutUndoRef = useRef<LayoutUndo | null>(null);
  const layoutCameraLockRef = useRef(0);
  const layoutCameraFrameRef = useRef<number | null>(null);
  const layoutRecoveryRef = useRef<KnowledgeMapLayoutRecovery>([]);
  const failedLayoutCommandRef = useRef<{ kind: "arrange" | "undo"; graph: PagedFocus; requestId: number } | null>(null);
  const initialFocusKeyRef = useRef<string | null>(
    initialFocus ? `${initialFocus.type}:${initialFocus.id}` : null,
  );

  const requestedContentTypes = useMemo(() => {
    const types = new Set(tools.types);
    if (focus) types.add(focus.type);
    return sortedValues(types);
  }, [focus, tools.types]);
  const requestedRelationTypes = useMemo(() => {
    const relations = new Set(tools.relations);
    // 콘텐츠 중심 지도에서는 태그의 분류 계층을 자동으로 끌어오지 않는다.
    // 태그를 직접 중심으로 선택했을 때만 부모 계층을 열어, 매핑된 콘텐츠와
    // 태그가 taxonomy 선으로 뒤섞여 일자형 목록처럼 보이지 않게 한다.
    if (focus?.type === "knowledge_tag" && !hasExplicitRelationFilters) relations.add("tag_parent");
    return sortedValues(relations);
  }, [focus, tools.relations, hasExplicitRelationFilters]);

  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    const restored = JSON.parse(routeToolsKey) as ReturnType<typeof readMapToolsState>;
    setTools((current) => ({
      query: restored.query,
      types: sameValues(sortedValues(current.types), sortedValues(restored.types)) ? current.types : restored.types,
      relations: sameValues(sortedValues(current.relations), sortedValues(restored.relations)) ? current.relations : restored.relations,
    }));
    setSearchInput(restored.query);
    return () => { if (searchTimerRef.current) clearTimeout(searchTimerRef.current); };
    // Focus-only URL changes must not cancel a pending search edit.
  }, [routeToolsKey]);

  useEffect(() => {
    const controller = new AbortController();
    if (!tools.query.trim()) { setSearchResult(null); setSearchLoading(false); setSearchError(false); return () => controller.abort(); }
    setSearchLoading(true);
    setSearchError(false);
    const params = new URLSearchParams({ q: tools.query.trim() });
    tools.types.forEach((type) => params.append("types", type));
    void apiFetch<KnowledgeMapV2Search>(`/api/learn/knowledge-map/v2/search?${params}`, { signal: controller.signal })
      .then((result) => { if (!controller.signal.aborted) setSearchResult(result); })
      .catch(() => { if (!controller.signal.aborted) setSearchError(true); })
      .finally(() => { if (!controller.signal.aborted) setSearchLoading(false); });
    return () => controller.abort();
  }, [tools.query, tools.types, searchRetry]);

  const updateFilter = (key: "types" | "relations", value: string, checked: boolean) => {
    const values = checked ? [...tools[key], value] : tools[key].filter((item) => item !== value);
    if (!values.length) return;
    const next = { ...tools, [key]: sortedValues(new Set(values)) };
    setTools(next);
    writeUrlQuery({ [key === "types" ? "map_types" : "map_relations"]: next[key].join(",") }, "push");
  };

  const changeSearch = (value: string) => {
    setSearchInput(value);
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    searchTimerRef.current = setTimeout(() => {
      setTools((current) => ({ ...current, query: value }));
      writeUrlQuery({ map_q: value || null });
    }, 300);
  };
  const selectNode = useCallback((nodeId: string | null) => {
    selectedNodeIdRef.current = nodeId;
    setSelectedNodeId(nodeId);
  }, []);

  const nodeLabel = useCallback(
    (nodeType: KnowledgeMapNodeType) => t(`nodeTypes.${nodeType}`),
    [t],
  );
  const relationLabel = useCallback(
    (relationType: string) => t(`relationTypes.${relationType}`),
    [t],
  );
  const describeDistance = useCallback((value: number) => t("step", { step: value }), [t]);
  const describeConnections = useCallback((value: number) => t("connectionCount", { count: value }), [t]);
  const describeEdge = useCallback(
    (label: string, strength: number, confidence: number) => `${label}. ${t("relationStrength")} ${Math.round(strength * 100)}%. ${t("evidenceConfidence")} ${Math.round(confidence * 100)}%.`,
    [t],
  );
  useEffect(() => {
    positionMemoryRef.current = readPositionMemory();
    viewportMemoryRef.current = readViewportMemory();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setOverviewLoading(true);
    setOverviewError(false);
    void apiFetch<KnowledgeMapV2Overview>("/api/learn/knowledge-map/v2/overview", { signal: controller.signal })
      .then((value) => setOverview(value))
      .catch(() => {
        if (!controller.signal.aborted) setOverviewError(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) setOverviewLoading(false);
      });
    return () => controller.abort();
  }, [overviewRetry]);

  const handleNodesChange = useCallback((changes: NodeChange<MapFlowNode>[]) => {
    if (layoutBusyRef.current || layoutRecoveryRef.current.length) {
      onNodesChange(changes.filter((change) => change.type !== "position"));
      return;
    }
    if (graph) {
      for (const change of changes) {
        if (change.type !== "position" || !change.position) continue;
        positionMemoryRef.current.set(positionMemoryKey(graph, change.id), change.position);
      }
      if (changes.some((change) => change.type === "position" && Boolean(change.position))) {
        writePositionMemory(positionMemoryRef.current);
        const positions = resolveCanvasPositions(graph, positionMemoryRef.current);
        setEdges((current) => {
          const selectedIds = new Set(current.filter((edge) => edge.selected).map((edge) => edge.id));
          return graphToFlowEdges(graph, relationLabel, describeEdge, positions).map((edge) => ({ ...edge, selected: selectedIds.has(edge.id) }));
        });
      }
    }
    onNodesChange(changes);
  }, [describeEdge, graph, onNodesChange, relationLabel, setEdges]);

  const loadFocus = useCallback(async (nextFocus: KnowledgeMapV2Node, cursor?: string) => {
    userSelectionIntentRef.current = null;
    layoutUndoRef.current = null;
    setLayoutUndo(null);
    failedLayoutCommandRef.current = null;
    if (!layoutRecoveryRef.current.length) setLayoutSaveError(null);
    graphRequestRef.current.controller?.abort();
    const controller = new AbortController();
    const requestId = graphRequestRef.current.id + 1;
    graphRequestRef.current = { id: requestId, controller };
    setGraphLoading(true);
    setGraphError(false);
    setPageError(false);
    try {
      const params = new URLSearchParams();
      if (cursor) params.set("cursor", cursor);
      else {
      params.set("focus_type", nextFocus.type);
      params.set("focus_id", nextFocus.entity_id);
      params.set("depth", String(depth));
      requestedContentTypes.forEach((type) => params.append("types", type));
      requestedRelationTypes.forEach((relation) => params.append("relation_types", relation));
      }
      const page = await apiFetch<PagedFocus>(`/api/learn/knowledge-map/v2/focus?${params.toString()}`, { signal: controller.signal });
      const result = cursor && graphRef.current ? mergeMapPage(graphRef.current, page) : page;
      if (graphRequestRef.current.id !== requestId || result.focus.id !== nextFocus.id) return;
      if (!currentGraphMatchesRequest(result, nextFocus, depth, requestedContentTypes, requestedRelationTypes)) {
        return;
      }
      if (cursor && graphRef.current) {
        for (const node of flowInstanceRef.current?.getNodes() ?? []) positionMemoryRef.current.set(positionMemoryKey(result, node.id), node.position);
      }
      graphDataRequestIdRef.current = requestId;
      graphRef.current = result;
      setGraph(result);
      const retainedNodeId = selectedNodeIdRef.current;
      const nextSelectedNodeId = result.nodes.some((node) => node.id === retainedNodeId)
        ? retainedNodeId
        : result.focus.id;
      const canvasPositions = resolveCanvasPositions(result, positionMemoryRef.current);
      compactLayoutRef.current = window.innerWidth < 1024;
      selectNode(nextSelectedNodeId);
      setNodes(graphToFlowNodes(
        result,
        nodeLabel,
        describeDistance,
        describeConnections,
        positionMemoryRef.current,
        nextSelectedNodeId,
        canvasPositions,
      ));
      setEdges(graphToFlowEdges(result, relationLabel, describeEdge, canvasPositions));
      setSelectedEdge(null);
    } catch (error) {
      if (
        graphRequestRef.current.id === requestId
        && !(error instanceof DOMException && error.name === "AbortError")
      ) {
        setGraphError(true);
        setPageError(Boolean(cursor));
        if (cursor && error instanceof ApiError && error.status === 409) {
          graphRef.current = graphRef.current ? { ...graphRef.current, next_cursor: null } : null;
          setGraph(graphRef.current);
        }
      }
    } finally {
      if (graphRequestRef.current.id === requestId) setGraphLoading(false);
    }
  }, [depth, describeConnections, describeDistance, describeEdge, nodeLabel, relationLabel, requestedContentTypes, requestedRelationTypes, selectNode, setEdges, setNodes]);

  useEffect(() => {
    if (!focus) return;
    void loadFocus(focus);
  }, [focus, loadFocus]);

  useEffect(() => () => graphRequestRef.current.controller?.abort(), []);

  useEffect(() => {
    if (focus || initialFocus || overviewLoading) return;
    if (overview?.default_focus) {
      const seed = overview.default_focus;
      setFocus(seed);
      selectNode(seed.id);
      return;
    }
  }, [focus, initialFocus, overview, overviewLoading, selectNode, t]);

  const initialFocusKey = initialFocus ? `${initialFocus.type}:${initialFocus.id}` : null;
  useEffect(() => {
    userSelectionIntentRef.current = null;
    const previousKey = initialFocusKeyRef.current;
    initialFocusKeyRef.current = initialFocusKey;
    if (initialFocus) {
      if (focus?.id === initialFocusKey) return;
      const nextFocus: KnowledgeMapV2Node = {
        id: initialFocusKey!,
        type: initialFocus.type,
        entity_id: initialFocus.id,
        name: initialFocus.id,
        href: null,
        is_focus: true,
        distance: 0,
      };
      setFocus(nextFocus);
      selectNode(nextFocus.id);
      setSelectedEdge(null);
      return;
    }
    if (previousKey && overview?.default_focus) {
      setFocus(overview.default_focus);
      selectNode(overview.default_focus.id);
      setSelectedEdge(null);
      setDetailsOpen(false);
    }
  }, [focus?.id, initialFocus, initialFocusKey, overview?.default_focus, selectNode]);

  const safeCanvasBounds = useCallback((): {
    left: number;
    right: number;
    top: number;
    bottom: number;
    x: number;
    y: number;
  } | null => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const mobile = !window.matchMedia("(min-width: 640px)").matches;
    const compact = !window.matchMedia("(min-width: 1024px)").matches;
    const panel = detailsOpen ? selectionPanelRef.current?.getBoundingClientRect() : null;
    // Compact controls occupy one horizontal row. Wide canvases reserve the
    // minimap's bottom band so a complete overview does not hide a node there.
    const left = compact ? 12 : 72;
    const right = Math.max(
      left + 80,
      panel && !mobile ? panel.left - rect.left - 12 : rect.width - (compact ? 12 : 24),
    );
    const top = compact ? 72 : 24;
    const bottom = Math.max(
      top + 80,
      panel && mobile ? panel.top - rect.top - 12 : rect.height - (compact ? 24 : 184),
    );
    return { left, right, top, bottom, x: (left + right) / 2, y: (top + bottom) / 2 };
  }, [detailsOpen]);

  const centerNodeInSafeArea = useCallback((
    nodeId: string,
    minimumZoom: number | null,
    duration = 0,
  ): boolean => {
    const instance = flowInstanceRef.current;
    const node = instance?.getNode(nodeId);
    const target = safeCanvasBounds();
    if (!instance || !node || !target) return false;
    const currentZoom = instance.getZoom();
    const measuredWidth = node.measured?.width ?? node.width ?? 250;
    const measuredHeight = node.measured?.height ?? node.height ?? 148;
    const fitZoom = clamp(Math.min(
      (target.right - target.left) / measuredWidth,
      (target.bottom - target.top) / measuredHeight,
    ), 0.05, 2.2);
    const preferredZoom = minimumZoom === null ? currentZoom : Math.max(currentZoom, minimumZoom);
    const zoom = Math.min(preferredZoom, fitZoom);
    void instance.setViewport({
      x: target.x - node.position.x * zoom,
      y: target.y - node.position.y * zoom,
      zoom,
    }, { duration });
    return true;
  }, [safeCanvasBounds]);

  const fitAllNodesInSafeArea = useCallback((duration = 0): boolean => {
    const instance = flowInstanceRef.current;
    if (!instance) return false;
    const viewport = fitKnowledgeMapViewport(instance.getNodes(), safeCanvasBounds(), {
      compact: window.innerWidth < 1024,
    });
    if (!viewport) return false;
    void instance.setViewport(viewport, { duration });
    return true;
  }, [safeCanvasBounds]);

  const ensureSelectedNodeVisible = useCallback(() => {
    if (layoutBusyRef.current) return;
    const instance = flowInstanceRef.current;
    const canvas = canvasRef.current;
    const nodeId = selectedNodeIdRef.current ?? graph?.focus.id;
    const node = nodeId ? instance?.getNode(nodeId) : undefined;
    if (!instance || !canvas || !node || !nodeId || !graph
      || restoredViewportKeyRef.current !== viewportMemoryKey(graph)) return;

    const rect = canvas.getBoundingClientRect();
    const target = safeCanvasBounds();
    if (!target) return;
    const screen = instance.flowToScreenPosition(node.position);
    const local = { x: screen.x - rect.left, y: screen.y - rect.top };
    const zoom = instance.getZoom();
    const measuredWidth = node.measured?.width ?? node.width ?? 250;
    const measuredHeight = node.measured?.height ?? node.height ?? 148;
    const halfWidth = (measuredWidth / 2) * zoom;
    const halfHeight = (measuredHeight / 2) * zoom;
    const visible = local.x - halfWidth >= target.left
      && local.x + halfWidth <= target.right
      && local.y - halfHeight >= target.top
      && local.y + halfHeight <= target.bottom;
    if (!visible || (detailsOpen && zoom < 0.86)) {
      centerNodeInSafeArea(nodeId, detailsOpen ? 0.86 : null, 180);
    }
  }, [centerNodeInSafeArea, detailsOpen, graph, safeCanvasBounds]);

  const graphNodesMeasured = nodes.length > 0 && nodes.every((node) => (
    Boolean(node.measured?.width && node.measured?.height)
  ));

  useEffect(() => {
    if (!graph || !graphNodesMeasured || !flowReady || !flowInstanceRef.current) return;
    const key = viewportMemoryKey(graph);
    if (restoredViewportKeyRef.current === key) return;
    const nodeId = selectedNodeIdRef.current ?? graph.focus.id;
    if (!flowInstanceRef.current.getNode(nodeId)) return;
    const frame = window.requestAnimationFrame(() => {
      const stored = viewportMemoryRef.current.get(key);
      if (stored) {
        // Older records do not distinguish automatic and user moves. Treat
        // them as user-owned rather than guessing and discarding a camera.
        automaticViewportRef.current = false;
        restoredViewportKeyRef.current = key;
        void flowInstanceRef.current?.setViewport(stored).then(() => {
          window.requestAnimationFrame(ensureSelectedNodeVisible);
        });
      } else {
        automaticViewportRef.current = true;
        if (fitAllNodesInSafeArea()) {
          // Only commit after measured cards have produced a real viewport.
          restoredViewportKeyRef.current = key;
        }
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [ensureSelectedNodeVisible, fitAllNodesInSafeArea, flowReady, graph, graphNodesMeasured]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (resizeFrameRef.current !== null) window.cancelAnimationFrame(resizeFrameRef.current);
      resizeFrameRef.current = window.requestAnimationFrame(() => {
        resizeFrameRef.current = null;
        if (layoutBusyRef.current) return;
        if (!graph) return;
        const compact = window.innerWidth < 1024;
        if (compactLayoutRef.current !== compact) {
          compactLayoutRef.current = compact;
          // Only default coordinates change with the card width. Explicit
          // drags remain authoritative and must survive rotation/resizing.
          const positions = resolveCanvasPositions(graph, positionMemoryRef.current);
          setNodes((current) => current.map((node) => ({ ...node, position: positions.get(node.id) ?? node.position })));
          setEdges((current) => {
            const selectedIds = new Set(current.filter((edge) => edge.selected).map((edge) => edge.id));
            return graphToFlowEdges(graph, relationLabel, describeEdge, positions).map((edge) => ({ ...edge, selected: selectedIds.has(edge.id) }));
          });
        }
        if (restoredViewportKeyRef.current !== viewportMemoryKey(graph)) return;
        // Card measurements settle after the responsive width changes.
        resizeFrameRef.current = window.requestAnimationFrame(() => {
          resizeFrameRef.current = null;
          if (layoutBusyRef.current) return;
          if (automaticViewportRef.current && !detailsOpen) fitAllNodesInSafeArea();
          else ensureSelectedNodeVisible();
        });
      });
    });
    observer.observe(canvas);
    if (selectionPanelRef.current) observer.observe(selectionPanelRef.current);
    return () => {
      observer.disconnect();
      if (resizeFrameRef.current !== null) window.cancelAnimationFrame(resizeFrameRef.current);
    };
  }, [describeEdge, detailsOpen, ensureSelectedNodeVisible, fitAllNodesInSafeArea, graph, relationLabel, setEdges, setNodes]);

  const handleViewportMoveEnd = useCallback((_event: MouseEvent | TouchEvent | null, viewport: Viewport) => {
    if (!graph || layoutBusyRef.current || layoutRecoveryRef.current.length
      || automaticViewportRef.current || restoredViewportKeyRef.current !== viewportMemoryKey(graph)) return;
    viewportMemoryRef.current.set(viewportMemoryKey(graph), viewport);
    writeViewportMemory(viewportMemoryRef.current);
  }, [graph]);

  const markViewportControlIntent = useCallback((target: EventTarget | null) => {
    if (target instanceof Element && target.closest("[data-map-layout-command]")) return;
    if (target instanceof Element && target.closest(".react-flow__controls, .react-flow__minimap")) {
      automaticViewportRef.current = false;
    }
  }, []);

  useEffect(() => () => {
    layoutCameraLockRef.current += 1;
    if (layoutCameraFrameRef.current !== null) window.cancelAnimationFrame(layoutCameraFrameRef.current);
  }, []);

  const applyLayout = useCallback((positions: Map<string, CanvasPosition>, viewport: Viewport) => {
    const instance = flowInstanceRef.current;
    if (!instance || !graph) return;
    const token = ++layoutCameraLockRef.current;
    layoutBusyRef.current = true;
    setLayoutBusy(true);
    automaticViewportRef.current = false;
    restoredViewportKeyRef.current = viewportMemoryKey(graph);
    setNodes((current) => current.map((node) => ({ ...node, position: positions.get(node.id) ?? node.position })));
    setEdges((current) => {
      const selectedIds = new Set(current.filter((edge) => edge.selected).map((edge) => edge.id));
      return graphToFlowEdges(graph, relationLabel, describeEdge, positions).map((edge) => ({ ...edge, selected: selectedIds.has(edge.id) }));
    });
    // Let node and panel effects settle before allowing automatic camera
    // corrections, so undo restores the exact camera captured before arrange.
    void instance.setViewport(viewport, { duration: 0 }).catch(() => {
      setLayoutSaveError("save");
    }).finally(() => {
      if (layoutCameraLockRef.current !== token) return;
      layoutCameraFrameRef.current = window.requestAnimationFrame(() => {
        layoutCameraFrameRef.current = window.requestAnimationFrame(() => {
          layoutCameraFrameRef.current = null;
          if (layoutCameraLockRef.current !== token) return;
          layoutBusyRef.current = false;
          setLayoutBusy(false);
        });
      });
    });
  }, [describeEdge, graph, relationLabel, setEdges, setNodes]);

  const persistLayout = useCallback((changes: Map<string, CanvasPosition | null>, viewport: Viewport): boolean => {
    if (!graph || layoutRecoveryRef.current.length) return false;
    try {
      const result = persistKnowledgeMapLayout(window.localStorage, graph, changes, viewport);
      if (result.ok) {
        positionMemoryRef.current = result.positions;
        viewportMemoryRef.current = result.viewports;
        setLayoutSaveError(null);
        return true;
      }
      layoutRecoveryRef.current = result.recovery;
      setLayoutSaveError(result.recovery.length ? "recovery" : "save");
    } catch {
      setLayoutSaveError("save");
    }
    return false;
  }, [graph]);

  const layoutAvailable = Boolean(graph && focus && !graphLoading && !layoutBusy
    && graphNodesMeasured && flowReady
    && graphRef.current === graph
    && graphDataRequestIdRef.current === graphRequestRef.current.id
    && (!initialFocusKey || initialFocusKey === graph.focus.id)
    && currentGraphMatchesRequest(graph, focus, depth, requestedContentTypes, requestedRelationTypes));
  const undoAvailable = layoutAvailable && layoutUndo?.graph === graph
    && layoutUndo.requestId === graphRequestRef.current.id;

  const handleAutoArrange = useCallback(() => {
    const instance = flowInstanceRef.current;
    if (!layoutAvailable || layoutBusyRef.current || layoutRecoveryRef.current.length || !graph || !instance
      || graphRef.current !== graph || graphDataRequestIdRef.current !== graphRequestRef.current.id) return;
    const currentNodes = instance.getNodes();
    const beforePositions = new Map(currentNodes.map((node) => [node.id, { ...node.position }]));
    const positions = createPositions(graph);
    const arrangedNodes = currentNodes.map((node) => ({ ...node, position: positions.get(node.id) ?? node.position }));
    const viewport = fitKnowledgeMapViewport(arrangedNodes, safeCanvasBounds(), { compact: window.innerWidth < 1024 });
    if (!viewport) return;
    const savedPositions = new Map(graph.nodes.map((node) => [node.id,
      positionMemoryRef.current.has(positionMemoryKey(graph, node.id)) ? beforePositions.get(node.id) ?? null : null,
    ]));
    const changes = new Map(graph.nodes.filter((node) => savedPositions.get(node.id) !== null)
      .map((node) => [node.id, positions.get(node.id) ?? null]));
    const undo: LayoutUndo = {
      graph,
      requestId: graphRequestRef.current.id,
      positions: beforePositions,
      savedPositions,
      viewport: { ...instance.getViewport() },
    };
    failedLayoutCommandRef.current = { kind: "arrange", graph, requestId: graphRequestRef.current.id };
    if (!persistLayout(changes, viewport)) return;
    failedLayoutCommandRef.current = null;
    layoutUndoRef.current = undo;
    setLayoutUndo(undo);
    applyLayout(positions, viewport);
  }, [applyLayout, graph, layoutAvailable, persistLayout, safeCanvasBounds]);

  const handleUndoArrange = useCallback(() => {
    if (!undoAvailable || layoutBusyRef.current || layoutRecoveryRef.current.length || !layoutUndo || !graph
      || graphRef.current !== graph || layoutUndoRef.current !== layoutUndo
      || layoutUndo.requestId !== graphRequestRef.current.id) return;
    failedLayoutCommandRef.current = { kind: "undo", graph, requestId: graphRequestRef.current.id };
    if (!persistLayout(layoutUndo.savedPositions, layoutUndo.viewport)) return;
    failedLayoutCommandRef.current = null;
    layoutUndoRef.current = null;
    setLayoutUndo(null);
    applyLayout(layoutUndo.positions, layoutUndo.viewport);
  }, [applyLayout, graph, layoutUndo, persistLayout, undoAvailable]);

  const retryLayoutCommand = () => {
    if (layoutRecoveryRef.current.length) {
      try {
        const result = recoverKnowledgeMapLayout(window.localStorage, layoutRecoveryRef.current);
        layoutRecoveryRef.current = result.recovery;
        if (!result.ok) return;
      } catch {
        return;
      }
    }
    setLayoutSaveError(null);
    const failed = failedLayoutCommandRef.current;
    if (failed?.graph !== graph || failed.requestId !== graphRequestRef.current.id) return;
    if (failed.kind === "arrange") handleAutoArrange();
    else handleUndoArrange();
  };

  useEffect(() => {
    if (!detailsOpen) return;
    const frame = window.requestAnimationFrame(ensureSelectedNodeVisible);
    return () => window.cancelAnimationFrame(frame);
  }, [detailsOpen, ensureSelectedNodeVisible]);

  const handleFlowInit = useCallback((instance: ReactFlowInstance<MapFlowNode, MapFlowEdge>) => {
    flowInstanceRef.current = instance;
    setFlowReady(true);
  }, []);

  const selectedNode = graph?.nodes.find((node) => node.id === selectedNodeId) ?? null;
  const selectedSource = graph?.nodes.find((node) => node.id === selectedEdge?.source);
  const selectedTarget = graph?.nodes.find((node) => node.id === selectedEdge?.target);

  const commitNodeSelection = useCallback((node: MapFlowNode) => {
    selectNode(node.data.mapNode.id);
    setSelectedEdge(null);
    setDetailsOpen(true);
    if (node.data.mapNode.id === focus?.id) return;
    setFocus(node.data.mapNode);
    onFocusChange?.({
      type: node.data.mapNode.type,
      id: node.data.mapNode.entity_id,
    });
  }, [focus?.id, onFocusChange, selectNode]);

  const commitEdgeSelection = useCallback((edge: MapFlowEdge) => {
    selectNode(null);
    setSelectedEdge(edge.data?.mapEdge ?? null);
    setDetailsOpen(true);
  }, [selectNode]);

  const clearSelection = useCallback(() => {
    selectNode(null);
    setSelectedEdge(null);
    setDetailsOpen(false);
  }, [selectNode]);

  const closeDetails = useCallback(() => {
    const selectedId = selectedNodeIdRef.current;
    clearSelection();
    window.requestAnimationFrame(() => {
      const selectedElement = selectedId
        ? canvasRef.current?.querySelector<HTMLElement>(`.react-flow__node[data-id="${CSS.escape(selectedId)}"]`)
        : null;
      (selectedElement ?? document.getElementById("knowledge-map-tools-toggle"))?.focus();
    });
  }, [clearSelection]);

  const commitResultingSelection = useCallback((
    selectedFlowNodes: MapFlowNode[],
    selectedFlowEdges: MapFlowEdge[],
  ) => {
    const selectedFlowNode = selectedFlowNodes[selectedFlowNodes.length - 1];
    const selectedFlowEdge = selectedFlowEdges[selectedFlowEdges.length - 1];
    if (selectedFlowNode) {
      commitNodeSelection(selectedFlowNode);
    } else if (selectedFlowEdge) {
      commitEdgeSelection(selectedFlowEdge);
    } else {
      clearSelection();
    }
  }, [clearSelection, commitEdgeSelection, commitNodeSelection]);

  const intentMatchesCurrentGraph = useCallback((intent: UserSelectionIntent) => (
    intent.routeFocusKey === initialFocusKey
    && intent.graphFocusId === (graph?.focus.id ?? null)
    && intent.graphRequestId === graphRequestRef.current.id
  ), [graph?.focus.id, initialFocusKey]);

  const onNodeClick = useCallback((_event: ReactMouseEvent, node: MapFlowNode) => {
    const intent = userSelectionIntentRef.current;
    if (
      !intent
      || intent.targetKind !== "node"
      || intent.targetId !== node.id
      || intent.expectedTargetSelected !== null
      || !intentMatchesCurrentGraph(intent)
    ) {
      return;
    }
    userSelectionIntentRef.current = null;
    // A selected node clicked without the multi-select key does not emit a
    // selection change. Reopen its panel without changing focus history.
    commitNodeSelection(node);
  }, [commitNodeSelection, intentMatchesCurrentGraph]);

  const onNodeDoubleClick = useCallback((_event: ReactMouseEvent, node: MapFlowNode) => {
    const href = internalContentHref(node.data.mapNode.href);
    if (href) router.push(href);
  }, [router]);

  const onEdgeClick = useCallback((_event: ReactMouseEvent, edge: MapFlowEdge) => {
    const intent = userSelectionIntentRef.current;
    if (
      !intent
      || intent.targetKind !== "edge"
      || intent.targetId !== edge.id
      || intent.expectedTargetSelected !== null
      || !intentMatchesCurrentGraph(intent)
    ) {
      return;
    }
    userSelectionIntentRef.current = null;
    commitEdgeSelection(edge);
  }, [commitEdgeSelection, intentMatchesCurrentGraph]);

  const onSelectionEnd = useCallback(() => {
    if (userSelectionIntentRef.current?.targetKind === "selection") {
      userSelectionIntentRef.current = null;
    }
  }, []);

  const markUserSelectionIntent = useCallback((
    target: EventTarget | null,
    mode: "select" | "toggle" | "deselect",
  ) => {
    userSelectionIntentRef.current = null;
    if (!(target instanceof Element)) return;
    const nodeElement = target.closest<HTMLElement>(".react-flow__node[data-id]");
    const edgeElement = target.closest<SVGGElement>(".react-flow__edge[data-id]");
    const targetKind = nodeElement ? "node" : edgeElement ? "edge" : null;
    const targetId = nodeElement?.dataset.id ?? edgeElement?.dataset.id;
    if (!targetKind || !targetId) return;
    const wasSelected = targetKind === "node"
      ? Boolean(nodes.find((node) => node.id === targetId)?.selected)
      : Boolean(edges.find((edge) => edge.id === targetId)?.selected);
    const expectedTargetSelected = mode === "toggle"
      ? !wasSelected
      : mode === "deselect"
        ? (wasSelected ? false : null)
        : (wasSelected ? null : true);
    userSelectionIntentRef.current = {
      targetKind,
      targetId,
      expectedTargetSelected,
      routeFocusKey: initialFocusKey,
      graphFocusId: graph?.focus.id ?? null,
      graphRequestId: graphRequestRef.current.id,
    };
  }, [edges, graph?.focus.id, initialFocusKey, nodes]);

  const onCanvasPointerDownCapture = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    markViewportControlIntent(event.target);
    markUserSelectionIntent(event.target, event.ctrlKey || event.metaKey ? "toggle" : "select");
  }, [markUserSelectionIntent, markViewportControlIntent]);

  const onCanvasKeyDownCapture = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Enter" && event.key !== " " && event.key !== "Escape") return;
    if (event.key !== "Escape") markViewportControlIntent(event.target);
    markUserSelectionIntent(
      event.target,
      event.key === "Escape"
        ? "deselect"
        : event.ctrlKey || event.metaKey
          ? "toggle"
          : "select",
    );
  }, [markUserSelectionIntent, markViewportControlIntent]);

  const onCanvasKeyDown = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Enter" && event.key !== " " && event.key !== "Escape") return;
    const intent = userSelectionIntentRef.current;
    if (!intent || intent.expectedTargetSelected !== null || !intentMatchesCurrentGraph(intent)) return;
    userSelectionIntentRef.current = null;
    if (event.key === "Escape") return;
    if (intent.targetKind === "node") {
      const selectedFlowNode = nodes.find((node) => node.id === intent.targetId);
      if (selectedFlowNode) commitNodeSelection(selectedFlowNode);
    } else if (intent.targetKind === "edge") {
      const selectedFlowEdge = edges.find((edge) => edge.id === intent.targetId);
      if (selectedFlowEdge) commitEdgeSelection(selectedFlowEdge);
    }
  }, [commitEdgeSelection, commitNodeSelection, edges, intentMatchesCurrentGraph, nodes]);

  const onSelectionStart = useCallback(() => {
    userSelectionIntentRef.current = {
      targetKind: "selection",
      targetId: null,
      expectedTargetSelected: null,
      routeFocusKey: initialFocusKey,
      graphFocusId: graph?.focus.id ?? null,
      graphRequestId: graphRequestRef.current.id,
    };
  }, [graph?.focus.id, initialFocusKey]);

  const onSelectionChange = useCallback<OnSelectionChangeFunc<MapFlowNode, MapFlowEdge>>(({
    nodes: selectedFlowNodes,
    edges: selectedFlowEdges,
  }) => {
    const intent = userSelectionIntentRef.current;
    if (!intent) return;
    if (!intentMatchesCurrentGraph(intent)) {
      userSelectionIntentRef.current = null;
      return;
    }
    if (intent.targetKind === "selection") {
      userSelectionIntentRef.current = null;
      commitResultingSelection(selectedFlowNodes, selectedFlowEdges);
      return;
    }
    if (intent.expectedTargetSelected === null || !intent.targetId) return;
    const targetIsSelected = intent.targetKind === "node"
      ? selectedFlowNodes.some((node) => node.id === intent.targetId)
      : selectedFlowEdges.some((edge) => edge.id === intent.targetId);
    if (targetIsSelected !== intent.expectedTargetSelected) return;
    userSelectionIntentRef.current = null;
    commitResultingSelection(selectedFlowNodes, selectedFlowEdges);
  }, [commitResultingSelection, intentMatchesCurrentGraph]);

  const selectedRelationMeta = selectedEdge
    ? relationMeta(selectedEdge.relation_type, relationLabel(selectedEdge.relation_type))
    : null;
  const hasSelection = detailsOpen && Boolean(selectedNode || selectedEdge);
  return (
    <section
      className="space-y-4"
      aria-label={t("canvasLabel")}
      data-testid={CANVAS_ARIA_LABEL}
      data-knowledge-map-version="v2"
      data-map-ui-revision="free-canvas-click-focus-v3"
      data-map-depth={depth}
    >
      <KnowledgeMapTools
        filters={{ ...tools, types: requestedContentTypes, relations: requestedRelationTypes }}
        focusType={focus?.type}
        input={searchInput}
        counts={graph ? { nodes: graph.nodes.length, edges: graph.edges.length } : null}
        search={{ loading: searchLoading, error: searchError, result: searchResult }}
        onSearch={changeSearch}
        onFilter={updateFilter}
        onReset={() => {
          setTools((current) => ({ ...current, types: [...MAP_CONTENT_TYPES], relations: [...DEFAULT_MAP_RELATIONS] }));
          writeUrlQuery({ map_types: null, map_relations: null }, "push");
        }}
        onRetry={() => setSearchRetry((value) => value + 1)}
        onPick={(item) => {
          const next: KnowledgeMapV2Node = { ...item, is_focus: true, distance: 0 };
          selectNode(next.id);
          setSelectedEdge(null);
          setDetailsOpen(true);
          if (next.id !== focus?.id) {
            setFocus(next);
            onFocusChange?.({ type: next.type, id: next.entity_id });
          }
        }}
      />
      {overviewLoading && !focus && (
        <div className="flex h-[min(68svh,680px)] min-h-[460px] items-center justify-center rounded-xl border border-border bg-bg-surface sm:min-h-[620px]" aria-busy="true">
          <span aria-hidden="true" className="h-1.5 w-28 animate-pulse rounded-full bg-accent-primary/60" />
        </div>
      )}
      {overviewError && !focus && (
        <div role="alert" className="flex h-[min(68svh,680px)] min-h-[460px] flex-col items-center justify-center gap-4 rounded-xl border border-border bg-bg-surface p-6 text-center sm:min-h-[620px]">
          <p className="text-sm text-text-secondary">{t("loadFailed")}</p>
          <Button type="button" variant="secondary" onClick={() => setOverviewRetry((value) => value + 1)}>
            {t("retry")}
          </Button>
        </div>
      )}
      {!overviewLoading && !overviewError && overview && !overview.default_focus && !focus && !initialFocus && (
        <div role="status" className="flex h-[min(68svh,680px)] min-h-[460px] items-center justify-center rounded-xl border border-border bg-bg-surface p-6 text-center sm:min-h-[620px]">
          <p className="text-sm text-text-secondary">{t("empty")}</p>
        </div>
      )}
      {focus && (
        <>
          {graphLoading && !graph && (
            <div className="flex h-[min(68svh,680px)] min-h-[460px] items-center justify-center rounded-xl border border-border bg-bg-surface sm:min-h-[620px]" aria-busy="true">
              <span aria-hidden="true" className="h-1.5 w-28 animate-pulse rounded-full bg-accent-primary/60" />
            </div>
          )}

          {graphError && !graph && (
            <div role="alert" className="flex h-[min(68svh,680px)] min-h-[460px] flex-col items-center justify-center gap-4 rounded-xl border border-border bg-bg-surface p-6 text-center sm:min-h-[620px]">
              <p className="text-sm text-text-secondary">{t("loadFailed")}</p>
              <Button type="button" variant="secondary" onClick={() => void loadFocus(focus)}>
                {t("retry")}
              </Button>
            </div>
          )}

          {graph && (
            <div className="block">
              {graph.truncated && <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-l-2 border-accent-warning pl-3 text-sm" role="status"><p className="min-w-0 break-words text-text-secondary">{t("truncated")}</p>{graph.next_cursor && <Button type="button" size="sm" variant="secondary" disabled={graphLoading} onClick={() => void loadFocus(focus, graph.next_cursor!)}>{t("loadMore")}</Button>}</div>}
              {layoutSaveError && (
                <div role="alert" className="mb-3 flex flex-wrap items-center justify-between gap-2 border-l-2 border-accent-warning pl-3 text-sm">
                  <p className="min-w-0 break-words text-text-secondary">{t(layoutSaveError === "recovery" ? "layoutRecoveryFailed" : "layoutSaveFailed")}</p>
                  <button type="button" className="min-h-11 shrink-0 px-3 font-semibold text-accent-primary" onClick={retryLayoutCommand}>{t("retry")}</button>
                </div>
              )}
              <div
                ref={canvasRef}
                className="relative h-[min(68svh,680px)] min-h-[460px] overflow-hidden rounded-lg border border-border bg-bg-surface sm:h-[min(78vh,820px)] sm:min-h-[620px]"
                data-map-layout="compact-canvas-v3"
                data-canvas-mode="obsidian-canvas"
                onPointerDownCapture={onCanvasPointerDownCapture}
                onKeyDownCapture={onCanvasKeyDownCapture}
                onKeyDown={onCanvasKeyDown}
              >
                {graphLoading && (
                  <div className="absolute inset-x-0 top-0 z-10 h-1 bg-bg-surface/95" aria-busy="true">
                    <span aria-hidden="true" className="block h-full w-1/3 animate-pulse rounded-r-full bg-accent-primary" />
                  </div>
                )}
                {graphError && !graphLoading && (
                  <div role="alert" className="absolute left-1/2 top-3 z-30 flex max-w-[calc(100%-7rem)] -translate-x-1/2 items-center gap-2 rounded-lg border border-accent-warning/40 bg-bg-surface/95 px-3 py-2 text-xs text-text-secondary shadow-lg">
                    <span>{t(pageError ? "pageFailed" : "loadFailed")}</span>
                    <button type="button" className="font-semibold text-accent-primary" onClick={() => void loadFocus(focus)}>
                      {t("retry")}
                    </button>
                  </div>
                )}
                <ReactFlow<MapFlowNode, MapFlowEdge>
                  nodes={nodes}
                  edges={edges}
                  nodeOrigin={[0.5, 0.5]}
                  nodeTypes={FLOW_NODE_TYPES}
                  onNodesChange={handleNodesChange}
                  onEdgesChange={onEdgesChange}
                  onNodeClick={onNodeClick}
                  onNodeDoubleClick={onNodeDoubleClick}
                  onEdgeClick={onEdgeClick}
                  onPaneClick={clearSelection}
                  onSelectionStart={onSelectionStart}
                  onSelectionEnd={onSelectionEnd}
                  onSelectionChange={onSelectionChange}
                  onInit={handleFlowInit}
                  onMoveStart={(event) => { if (event) automaticViewportRef.current = false; }}
                  onMoveEnd={handleViewportMoveEnd}
                  nodesDraggable={!layoutBusy && !layoutRecoveryRef.current.length}
                  selectNodesOnDrag={false}
                  nodesConnectable={false}
                  panOnDrag
                  panOnScroll
                  selectionOnDrag
                  zoomOnScroll
                  zoomOnPinch
                  zoomOnDoubleClick={false}
                  minZoom={0.05}
                  maxZoom={2.2}
                  proOptions={{ hideAttribution: true }}
                  aria-label={t("canvasLabel")}
                  ariaLabelConfig={{
                    "controls.ariaLabel": t("controls"),
                    "controls.zoomIn.ariaLabel": t("zoomIn"),
                    "controls.zoomOut.ariaLabel": t("zoomOut"),
                    "controls.fitView.ariaLabel": t("fitView"),
                    "controls.interactive.ariaLabel": t("toggleInteractive"),
                    "minimap.ariaLabel": t("miniMap"),
                  }}
                >
                  <Background variant={BackgroundVariant.Dots} color="#CBD5E1" gap={24} size={1} />
                  <Controls
                    position="top-left"
                    showFitView={false}
                    className="!flex-row !overflow-hidden !rounded-lg !border !border-border [&_button]:!h-11 [&_button]:!w-11 lg:!flex-col lg:[&_button]:!h-8 lg:[&_button]:!w-8"
                    style={{
                      "--xy-controls-button-background-color": "var(--color-bg-surface)",
                      "--xy-controls-button-background-color-hover": "var(--color-bg-elevated)",
                      "--xy-controls-button-color": "var(--color-text-primary)",
                      "--xy-controls-button-color-hover": "var(--color-accent-primary)",
                      "--xy-controls-button-border-color": "var(--color-border)",
                    } as CSSProperties}
                  >
                    <ControlButton
                      className="react-flow__controls-fitview"
                      onClick={() => {
                        automaticViewportRef.current = false;
                        fitAllNodesInSafeArea(180);
                      }}
                      title={t("fitView")}
                      aria-label={t("fitView")}
                    >
                      <Maximize2 aria-hidden="true" className="!fill-none" />
                    </ControlButton>
                    <ControlButton
                      data-map-layout-command="arrange"
                      onClick={handleAutoArrange}
                      disabled={!layoutAvailable || Boolean(layoutRecoveryRef.current.length)}
                      title={t("autoArrange")}
                      aria-label={t("autoArrange")}
                    >
                      <LayoutGrid aria-hidden="true" className="!fill-none" />
                    </ControlButton>
                    <ControlButton
                      data-map-layout-command="undo"
                      onClick={handleUndoArrange}
                      disabled={!undoAvailable || Boolean(layoutRecoveryRef.current.length)}
                      title={t("undoAutoArrange")}
                      aria-label={t("undoAutoArrange")}
                    >
                      <Undo2 aria-hidden="true" className="!fill-none" />
                    </ControlButton>
                  </Controls>
                  <MiniMap
                    pannable
                    zoomable
                    nodeColor={(node) => {
                      const data = node.data as MapFlowNodeData;
                      return NODE_STYLE[data.nodeType]?.border ?? "#64748B";
                    }}
                    nodeStrokeWidth={2}
                    maskColor="rgba(15, 23, 42, 0.08)"
                    className="!hidden lg:!block !bottom-4 !right-4 !left-auto !m-0 !rounded-lg !border !border-border !bg-bg-surface/90 !shadow-lg"
                  />
                </ReactFlow>
                {hasSelection && (
                  <div
                    ref={selectionPanelRef}
                    className="absolute inset-x-3 bottom-3 z-20 max-h-[36%] overflow-y-auto rounded-lg border border-border bg-bg-surface/95 p-3 text-sm shadow-lg backdrop-blur sm:inset-auto sm:right-4 sm:top-4 sm:w-[min(20rem,calc(100%-2rem))] sm:max-h-[calc(100%-2rem)]"
                    aria-live="polite"
                  >
                    <div className="mb-2 flex justify-end"><button type="button" onClick={closeDetails} title={t("closeDetails")} aria-label={t("closeDetails")} className="flex h-8 w-8 items-center justify-center rounded-md text-text-secondary hover:bg-bg-elevated"><X className="h-4 w-4" aria-hidden="true" /></button></div>
                    {selectedEdge && selectedRelationMeta ? (
                      <div className="space-y-2">
                        <p className="break-words text-sm font-medium text-text-primary">{selectedSource?.name} {selectedEdge.is_directed ? "→" : "↔"} {selectedTarget?.name}</p>
                        <p className="text-xs font-semibold" style={{ color: selectedRelationMeta.color }}>
                          {selectedRelationMeta.label}
                        </p>
                        <p className="flex items-start gap-1.5 text-xs text-text-secondary">{selectedEdge.origin === "system_prerequisite" && <LockKeyhole className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />}{t(selectedEdge.origin === "system_prerequisite" ? "requiredCondition" : "optionalRelation")}</p>
                        <p className="text-xs leading-5 text-text-secondary">
                          {relationExplanation(selectedEdge, relationLabel)}
                        </p>
                        <div className="flex gap-3 text-[11px] text-text-muted">
                          <span>{t("relationStrength")} {Math.round(clamp(selectedEdge.strength, 0, 1) * 100)}%</span>
                          <span>{t("evidenceConfidence")} {Math.round(clamp(selectedEdge.confidence, 0, 1) * 100)}%</span>
                        </div>
                      </div>
                    ) : selectedNode ? (
                      <div className="space-y-2">
                        <p className="text-xs font-medium text-text-secondary">{nodeLabel(selectedNode.type)}{selectedNode.is_focus ? ` · ${t("focusNode")}` : ""}</p>
                        <p className="font-semibold text-text-primary">{selectedNode.name}</p>
                        <div className="flex gap-2">
                          {internalContentHref(selectedNode.href) && (
                            <Button type="button" size="sm" onClick={() => router.push(internalContentHref(selectedNode.href)!)}>
                              {t("openSource")}
                            </Button>
                          )}
                        </div>
                      </div>
                    ) : null}
                  </div>
                )}
              </div>
            </div>
          )}

        </>
      )}

    </section>
  );
}
