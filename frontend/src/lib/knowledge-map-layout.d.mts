import type { KnowledgeMapV2Focus } from "@/types/knowledge";

type Point = { x: number; y: number };
type LayoutNode = {
  position: Point;
  measured?: { width?: number; height?: number };
  width?: number;
  height?: number;
};

export function createKnowledgeMapPositions(graph: KnowledgeMapV2Focus, options?: { compact?: boolean }): Map<string, Point>;
export function fitKnowledgeMapViewport(nodes: readonly LayoutNode[], bounds: { left: number; right: number; top: number; bottom: number } | null, options?: { compact?: boolean; minZoom?: number; maxZoom?: number }): (Point & { zoom: number }) | null;
