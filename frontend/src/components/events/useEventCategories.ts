"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiFetch, ApiError } from "@/lib/api-client";
import type { EventCategoryDefinition, EventCategoryListResponse } from "@/types/event";

export function useEventCategories(admin = false) {
  const t = useTranslations("events.categoryManagement");
  const [categories, setCategories] = useState<EventCategoryDefinition[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const requestRef = useRef(0);

  const reload = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const request = ++requestRef.current;
    setLoading(true);
    setError(null);
    try {
      const response = await apiFetch<EventCategoryListResponse>(`/api/learn/${admin ? "admin/" : ""}event-categories`, { signal: controller.signal });
      if (!controller.signal.aborted && request === requestRef.current) setCategories(response.items);
    } catch (cause) {
      if (!controller.signal.aborted && request === requestRef.current) setError(cause instanceof ApiError ? cause.message : t("loadFailed"));
    } finally {
      if (!controller.signal.aborted && request === requestRef.current) setLoading(false);
    }
  }, [admin, t]);

  useEffect(() => {
    void reload();
    return () => controllerRef.current?.abort();
  }, [reload]);

  return { categories, loading, error, reload };
}
