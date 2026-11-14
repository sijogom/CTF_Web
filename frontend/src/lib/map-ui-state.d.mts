import type { KnowledgeMapV2Focus } from "@/types/knowledge";
export const MAP_CONTENT_TYPES: readonly ["challenge", "course", "lesson", "knowledge_tag"];
export const MAP_RELATION_TYPES: readonly string[];
export const DEFAULT_MAP_RELATIONS: readonly string[];
export function readMapToolsState(search: string): { query: string; types: string[]; relations: string[] };
export function mergeMapPage<T extends KnowledgeMapV2Focus>(current: T, page: T): T;
export function internalContentHref(value: unknown): string | null;
