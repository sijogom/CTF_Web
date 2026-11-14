"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, ApiError } from "@/lib/api-client";
import { mappingSavePayload, mappingSnapshot, patchMapping, selectMappingTags } from "@/lib/knowledge-tag-mapping-state.mjs";
import type { ContentTagMappingEntry, ContentTagMappingResponse } from "@/types/knowledge";

export type MappingEntityType = "challenge" | "course" | "lesson";
export interface MappingEditorDraft {
  key: string;
  status: "loading" | "ready" | "error";
  error: string | null;
  revision: string | null;
  initialSnapshot: string | null;
  mappings: ContentTagMappingEntry[];
}

export function mappingEndpoint(entityType: MappingEntityType, entityId: string) {
  return `/api/learn/knowledge-tags/${entityType}s/${entityId}`;
}

export function useKnowledgeTagMappingDraft(entityType: MappingEntityType, entityId?: string | null) {
  const [draft, setDraft] = useState<MappingEditorDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const requestRef = useRef(0);
  const key = entityId ? `${entityType}:${entityId}` : null;
  const keyRef = useRef(key);
  keyRef.current = key;

  useEffect(() => {
    const generation = ++requestRef.current;
    if (!entityId || !key) {
      setDraft(null);
      return;
    }
    const controller = new AbortController();
    setDraft({ key, status: "loading", error: null, revision: null, initialSnapshot: null, mappings: [] });
    void apiFetch<ContentTagMappingResponse>(mappingEndpoint(entityType, entityId), { signal: controller.signal })
      .then((response) => {
        if (controller.signal.aborted || requestRef.current !== generation) return;
        setDraft({ key, status: "ready", error: null, revision: response.mapping_revision,
          initialSnapshot: mappingSnapshot(response.mappings), mappings: response.mappings });
      })
      .catch((error) => {
        if (controller.signal.aborted || requestRef.current !== generation) return;
        setDraft({ key, status: "error", error: error instanceof ApiError ? error.message : null,
          revision: null, initialSnapshot: null, mappings: [] });
      });
    return () => controller.abort();
  }, [entityId, entityType, key, attempt]);

  const currentDraft = draft?.key === key ? draft : null;
  const dirty = currentDraft?.status === "ready"
    && currentDraft.initialSnapshot !== mappingSnapshot(currentDraft.mappings);

  const selectTags = useCallback((ids: number[]) => {
    setDraft((current) => current?.key === keyRef.current && current.status === "ready"
      ? { ...current, mappings: selectMappingTags(current.mappings, ids) } : current);
  }, []);
  const updateMapping = useCallback((tagId: number, patch: Partial<ContentTagMappingEntry>) => {
    setDraft((current) => current?.key === keyRef.current && current.status === "ready"
      ? { ...current, mappings: patchMapping(current.mappings, tagId, patch) } : current);
  }, []);
  const save = useCallback(async () => {
    const payload = currentDraft && mappingSavePayload(currentDraft);
    if (!payload || !entityId || saving) return false;
    const savedKey = key;
    setSaving(true);
    try {
      const response = await apiFetch<ContentTagMappingResponse>(mappingEndpoint(entityType, entityId), {
        method: "PUT", body: payload,
      });
      if (keyRef.current === savedKey) {
        setDraft({ key: savedKey!, status: "ready", error: null, revision: response.mapping_revision,
          initialSnapshot: mappingSnapshot(response.mappings), mappings: response.mappings });
      }
      return true;
    } finally {
      setSaving(false);
    }
  }, [currentDraft, entityId, entityType, key, saving]);

  return { draft: currentDraft, dirty, saving, busy: saving || (!!key && (!currentDraft || currentDraft.status === "loading")),
    selectTags, updateMapping, save, retry: () => setAttempt((value) => value + 1),
    payload: currentDraft ? mappingSavePayload(currentDraft) : null };
}

export type MappingEditorController = ReturnType<typeof useKnowledgeTagMappingDraft>;
