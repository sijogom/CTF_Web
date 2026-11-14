import type { KnowledgeMapV2Focus } from "@/types/knowledge";

type Point = { x: number; y: number };
type Viewport = Point & { zoom: number };
type Storage = Pick<globalThis.Storage, "getItem" | "setItem" | "removeItem">;
export type KnowledgeMapLayoutRecovery = { key: string; previousRaw: string | null; writtenRaw: string }[];

export const KNOWLEDGE_MAP_POSITION_STORAGE_KEY: string;
export const KNOWLEDGE_MAP_POSITION_LAYOUT_VERSION: string;
export const KNOWLEDGE_MAP_VIEWPORT_STORAGE_KEY: string;
export const KNOWLEDGE_MAP_VIEWPORT_LAYOUT_VERSION: string;
export const KNOWLEDGE_MAP_MAX_STORED_POSITIONS: number;
export const KNOWLEDGE_MAP_MAX_STORED_VIEWPORTS: number;
export function knowledgeMapPositionKey(graph: KnowledgeMapV2Focus, nodeId: string): string;
export function knowledgeMapViewportKey(graph: KnowledgeMapV2Focus): string;
export function recoverKnowledgeMapLayout(storage: Storage, recovery: KnowledgeMapLayoutRecovery): { ok: boolean; recovery: KnowledgeMapLayoutRecovery };
export function persistKnowledgeMapLayout(storage: Storage, graph: KnowledgeMapV2Focus, positionChanges: ReadonlyMap<string, Point | null>, viewport: Viewport):
  | { ok: true; positions: Map<string, Point>; viewports: Map<string, Viewport> }
  | { ok: false; recovery: KnowledgeMapLayoutRecovery };
