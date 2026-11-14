"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import type {
  CoursePrerequisiteCandidate,
  CoursePrerequisiteReplaceResponse,
} from "@/types/course-prerequisite";

interface CoursePrerequisiteManagerProps {
  courseId: string;
  initialPrerequisiteIds: string[];
  disabled?: boolean;
  onSaved: (prerequisiteIds: string[]) => void;
}

function normalizeIds(ids: string[]): string[] {
  return [...new Set(ids)].sort();
}

function isSelectableCandidate(candidate: CoursePrerequisiteCandidate): boolean {
  return (
    !candidate.is_deleted
    && candidate.is_published
    && candidate.status === "approved"
  );
}

export function CoursePrerequisiteManager({
  courseId,
  initialPrerequisiteIds,
  disabled = false,
  onSaved,
}: CoursePrerequisiteManagerProps) {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const { toast } = useToast();

  const [candidates, setCandidates] = useState<CoursePrerequisiteCandidate[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>(() => normalizeIds(initialPrerequisiteIds));
  const [savedIds, setSavedIds] = useState<string[]>(() => normalizeIds(initialPrerequisiteIds));
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);

  const loadCandidates = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const response = await apiFetch<CoursePrerequisiteCandidate[]>(
        "/api/learn/courses?size=100",
      );
      setCandidates(response.filter((candidate) => candidate.id !== courseId));
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.message : tc("error"));
    } finally {
      setLoading(false);
    }
  }, [courseId, tc]);

  useEffect(() => {
    void loadCandidates();
  }, [loadCandidates]);

  const selectableCandidates = useMemo(
    () => candidates.filter(isSelectableCandidate),
    [candidates],
  );
  const selectableIdSet = useMemo(
    () => new Set(selectableCandidates.map((candidate) => candidate.id)),
    [selectableCandidates],
  );
  const unavailableSelectedIds = useMemo(
    () => selectedIds.filter((id) => !selectableIdSet.has(id)),
    [selectedIds, selectableIdSet],
  );
  const selectedKey = normalizeIds(selectedIds).join("\u0001");
  const savedKey = savedIds.join("\u0001");
  const hasChanges = selectedKey !== savedKey;

  const togglePrerequisite = (candidateId: string) => {
    setSelectedIds((current) => (
      current.includes(candidateId)
        ? current.filter((id) => id !== candidateId)
        : [...current, candidateId]
    ));
  };

  const removeUnavailableSelections = () => {
    setSelectedIds((current) => current.filter((id) => selectableIdSet.has(id)));
  };

  const savePrerequisites = async () => {
    if (disabled || saving || !hasChanges) return;

    setSaving(true);
    try {
      const result = await apiFetch<CoursePrerequisiteReplaceResponse>(
        `/api/learn/courses/${courseId}/prerequisites`,
        {
          method: "PUT",
          body: { prerequisite_ids: normalizeIds(selectedIds) },
        },
      );
      const savedIds = normalizeIds(result.prerequisite_ids);
      setSelectedIds(savedIds);
      setSavedIds(savedIds);
      onSaved(savedIds);
      toast("success", tc("save"));
    } catch (error) {
      toast("error", error instanceof ApiError ? error.message : tc("error"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="min-w-0 rounded-xl border border-border bg-bg-surface p-6" aria-labelledby="course-prerequisites-title">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 id="course-prerequisites-title" className="text-lg font-semibold">{t("coursePrerequisites")}</h2>
          <p className="mt-1 break-words text-sm text-text-muted">{t("coursePrerequisitesHelp")}</p>
        </div>
        <Button onClick={savePrerequisites} disabled={disabled || saving || !hasChanges}>
          {saving ? tc("processing") : tc("save")}
        </Button>
      </div>

      {disabled && (
        <p className="rounded-lg border border-accent-warning/30 bg-accent-warning/10 px-4 py-3 text-sm text-text-secondary">
          {t("coursePrerequisiteArchivedReadOnly")}
        </p>
      )}

      {!disabled && loading && (
        <p className="rounded-lg bg-bg-elevated px-4 py-3 text-sm text-text-muted">{tc("loading")}</p>
      )}

      {!disabled && !loading && loadError && (
        <div className="rounded-lg border border-accent-error/30 bg-accent-error/10 px-4 py-3 text-sm text-text-secondary">
          <p>{loadError}</p>
          <button
            type="button"
            onClick={() => { void loadCandidates(); }}
            className="mt-2 font-medium text-accent-primary hover:underline"
          >
            {tc("retry")}
          </button>
        </div>
      )}

      {!disabled && !loading && !loadError && (
        <>
          {unavailableSelectedIds.length > 0 && (
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-accent-warning/30 bg-accent-warning/10 px-4 py-3 text-sm text-text-secondary">
              <p>{t("coursePrerequisiteUnavailableSelection", { count: unavailableSelectedIds.length })}</p>
              <Button type="button" variant="secondary" size="sm" onClick={removeUnavailableSelections}>
                {t("coursePrerequisiteRemoveUnavailable")}
              </Button>
            </div>
          )}

          {selectableCandidates.length === 0 ? (
            <p className="rounded-lg bg-bg-elevated px-4 py-3 text-sm text-text-muted">
              {t("coursePrerequisiteNoCandidates")}
            </p>
          ) : (
            <ul className="grid min-w-0 grid-cols-1 gap-2 sm:grid-cols-2">
              {selectableCandidates.map((candidate) => {
                const selected = selectedIds.includes(candidate.id);
                return (
                  <li key={candidate.id} className="min-w-0">
                    <label className={`flex w-full min-w-0 cursor-pointer items-center gap-3 rounded-lg border px-3 py-2.5 text-sm transition-colors ${selected ? "border-accent-primary bg-accent-primary/10" : "border-border hover:bg-bg-elevated"}`}>
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={() => togglePrerequisite(candidate.id)}
                        className="h-4 w-4 accent-accent-primary"
                      />
                      <span className="min-w-0 truncate font-medium">{candidate.title}</span>
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
