"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api-client";
import {
  createClassResource,
  deleteClassResource,
} from "@/components/teaching/education-api";
import type {
  EducationResourceLink,
  EducationResourceLinkCreatePayload,
} from "@/types/education";

type ResourceType = EducationResourceLink["resource_type"];

interface EducationResourceManagerProps {
  classId: string;
  resources: EducationResourceLink[];
  canManage: boolean;
  canAdd: boolean;
  onChanged: () => Promise<void>;
}

function resourceFallbackLabel(resource: EducationResourceLink): string {
  if (resource.resource_type === "wargame") return `Wargame #${resource.challenge_id ?? ""}`;
  return resource.resource_type;
}

export function EducationResourceManager({
  classId,
  resources,
  canManage,
  canAdd,
  onChanged,
}: EducationResourceManagerProps) {
  const t = useTranslations("education");
  const tc = useTranslations("common");
  const [resourceType, setResourceType] = useState<ResourceType>("course");
  const [targetId, setTargetId] = useState("");
  const [label, setLabel] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<EducationResourceLink | null>(null);
  const [error, setError] = useState<string | null>(null);

  const addResource = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalizedTarget = targetId.trim();
    if (!normalizedTarget) return;

    let body: EducationResourceLinkCreatePayload;
    if (resourceType === "wargame") {
      const challengeId = Number(normalizedTarget);
      if (!Number.isSafeInteger(challengeId) || challengeId < 1) {
        setError(t("resources.invalidChallengeId"));
        return;
      }
      body = {
        resource_type: resourceType,
        challenge_id: challengeId,
        label: label.trim() || null,
      };
    } else {
      body = {
        resource_type: resourceType,
        [resourceType === "course" ? "course_id" : "lesson_id"]: normalizedTarget,
        label: label.trim() || null,
      };
    }

    setSubmitting(true);
    setError(null);
    try {
      await createClassResource(classId, body);
      setTargetId("");
      setLabel("");
      await onChanged();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setSubmitting(false);
    }
  };

  const removeResource = async () => {
    if (!deleteTarget) return;
    setSubmitting(true);
    setError(null);
    try {
      await deleteClassResource(classId, deleteTarget.id);
      setDeleteTarget(null);
      await onChanged();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : tc("error"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="rounded-xl border border-border bg-bg-surface p-5" aria-labelledby="education-resources-title">
      <div>
        <h2 id="education-resources-title" className="text-lg font-semibold">{t("resources.title")}</h2>
        <p className="mt-1 text-sm text-text-secondary">
          {canManage ? t("resources.managerDescription") : t("resources.studentDescription")}
        </p>
      </div>

      {canManage && canAdd && (
        <form className="mt-5 grid gap-3 rounded-lg border border-border bg-bg-primary p-4 md:grid-cols-[minmax(8rem,0.8fr)_minmax(0,1.4fr)_minmax(0,1.2fr)_auto]" onSubmit={addResource} noValidate>
          <div>
            <label htmlFor="education-resource-type" className="mb-1 block text-xs font-medium">{t("resources.type")}</label>
            <select id="education-resource-type" value={resourceType} onChange={(event) => setResourceType(event.target.value as ResourceType)} className="w-full rounded-lg border border-border bg-bg-surface px-3 py-2 text-sm">
              <option value="course">{t("resources.types.course")}</option>
              <option value="lesson">{t("resources.types.lesson")}</option>
              <option value="wargame">{t("resources.types.wargame")}</option>
            </select>
          </div>
          <div>
            <label htmlFor="education-resource-target" className="mb-1 block text-xs font-medium">{t("resources.targetId")}</label>
            <input id="education-resource-target" required value={targetId} onChange={(event) => setTargetId(event.target.value)} placeholder={resourceType === "wargame" ? t("resources.challengeIdPlaceholder") : t("resources.uuidPlaceholder")} className="w-full rounded-lg border border-border bg-bg-surface px-3 py-2 text-sm" />
          </div>
          <div>
            <label htmlFor="education-resource-label" className="mb-1 block text-xs font-medium">{t("resources.label")}</label>
            <input id="education-resource-label" maxLength={200} value={label} onChange={(event) => setLabel(event.target.value)} placeholder={t("resources.labelPlaceholder")} className="w-full rounded-lg border border-border bg-bg-surface px-3 py-2 text-sm" />
          </div>
          <div className="flex items-end">
            <Button type="submit" disabled={submitting || !targetId.trim()} className="w-full md:w-auto">{submitting ? tc("processing") : t("resources.add")}</Button>
          </div>
        </form>
      )}

      {canManage && !canAdd && <p className="mt-4 rounded-lg border border-accent-warning/30 bg-accent-warning/10 px-3 py-2 text-sm text-text-secondary">{t("resources.archivedCannotAdd")}</p>}
      {error && <p role="alert" className="mt-4 text-sm text-accent-error">{error}</p>}

      {resources.length === 0 ? (
        <p className="mt-4 text-sm text-text-muted">{t("resources.empty")}</p>
      ) : (
        <ul className="mt-4 divide-y divide-border overflow-hidden rounded-lg border border-border bg-bg-primary">
          {resources.map((resource) => {
            const labelText = resource.label ?? resourceFallbackLabel(resource);
            return (
              <li key={resource.id} className="flex flex-col gap-3 p-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  {resource.href ? (
                    <Link href={resource.href} className="block truncate text-sm font-medium text-accent-primary hover:underline">{labelText}</Link>
                  ) : (
                    <p className="truncate text-sm font-medium text-text-secondary">{labelText}</p>
                  )}
                  <p className="mt-1 text-xs text-text-muted">
                    {t(`resources.types.${resource.resource_type}`)}
                    {!resource.href && <span className="ml-2 text-accent-warning">{t("resources.stale")}</span>}
                  </p>
                </div>
                {canManage && (
                  <button type="button" onClick={() => setDeleteTarget(resource)} disabled={submitting} className="shrink-0 text-sm font-medium text-accent-error hover:underline disabled:opacity-50">{tc("delete")}</button>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {deleteTarget && (
        <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/50 p-4">
          <section role="alertdialog" aria-modal="true" aria-labelledby="education-resource-delete-title" className="w-full max-w-md rounded-xl border border-border bg-bg-primary p-6">
            <h3 id="education-resource-delete-title" className="text-lg font-semibold">{t("resources.removeTitle")}</h3>
            <p className="mt-2 text-sm text-text-secondary">{t("resources.removeDescription", { name: deleteTarget.label ?? resourceFallbackLabel(deleteTarget) })}</p>
            <div className="mt-5 flex justify-end gap-3">
              <Button type="button" variant="ghost" onClick={() => setDeleteTarget(null)} disabled={submitting}>{tc("cancel")}</Button>
              <Button type="button" variant="danger" onClick={() => void removeResource()} disabled={submitting}>{submitting ? tc("processing") : tc("delete")}</Button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}
