"use client";

import { type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";
import type { EventCategory, EventPhase, EventStatus } from "@/types/event";
import { eventCategoryLabel, EVENT_CATEGORIES } from "@/types/event";

const CATEGORY_STYLES: Record<EventCategory, string> = {
  workshop: "bg-accent-primary/10 text-accent-primary",
  seminar: "bg-violet-500/10 text-violet-300",
  study: "bg-accent-success/10 text-accent-success",
  networking: "bg-sky-500/10 text-sky-300",
  notice: "bg-accent-warning/10 text-accent-warning",
  other: "bg-bg-primary text-text-secondary",
};

const PHASE_STYLES: Record<EventPhase, string> = {
  upcoming: "bg-accent-primary/10 text-accent-primary",
  active: "bg-accent-success/10 text-accent-success",
  ended: "bg-bg-primary text-text-muted",
};

const STATUS_STYLES: Record<EventStatus, string> = {
  draft: "bg-accent-warning/10 text-accent-warning",
  published: "bg-accent-success/10 text-accent-success",
  archived: "bg-accent-error/10 text-accent-error",
};

function Badge({ children, className }: { children: ReactNode; className: string }) {
  return (
    <span className={`inline-flex max-w-full rounded-full px-2.5 py-0.5 text-xs font-medium ${className}`}>
      <span className="min-w-0 [overflow-wrap:anywhere]">{children}</span>
    </span>
  );
}

export function EventCategoryBadge({ category, labels }: { category: EventCategory; labels?: Record<string, string> }) {
  const t = useTranslations("events");
  const locale = useLocale();
  const fallback = (EVENT_CATEGORIES as readonly string[]).includes(category) ? t(`categories.${category}`) : category;
  return <Badge className={CATEGORY_STYLES[category] ?? CATEGORY_STYLES.other}>{eventCategoryLabel(labels, locale, fallback)}</Badge>;
}

export function EventPhaseBadge({ phase }: { phase: EventPhase }) {
  const t = useTranslations("events");
  return <Badge className={PHASE_STYLES[phase]}>{t(`phases.${phase}`)}</Badge>;
}

export function EventStatusBadge({ status }: { status: EventStatus }) {
  const t = useTranslations("events");
  return <Badge className={STATUS_STYLES[status]}>{t(`statuses.${status}`)}</Badge>;
}
