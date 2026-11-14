export const EVENT_CATEGORIES = [
  "workshop",
  "seminar",
  "study",
  "networking",
  "notice",
  "other",
] as const;

export type EventCategory = string;

export interface EventCategoryDefinition {
  key: EventCategory;
  labels: Record<string, string>;
  sort_order: number;
  revision: number;
}

export interface EventCategoryListResponse {
  items: EventCategoryDefinition[];
}

export function eventCategoryLabel(labels: Record<string, string> | undefined, locale: string, fallback: string): string {
  return labels?.[locale] || labels?.ko || labels?.en || fallback;
}

export function readEventCategory(value: string | null): string {
  return value && /^[a-z][a-z0-9_-]{0,29}$/.test(value) ? value : "all";
}

export const EVENT_PHASES = ["upcoming", "active", "ended"] as const;

export type EventPhase = (typeof EVENT_PHASES)[number];

export const EVENT_STATUSES = ["draft", "published", "archived"] as const;

export type EventStatus = (typeof EVENT_STATUSES)[number];
export type EventWritableStatus = Exclude<EventStatus, "archived">;

export interface EventSummary {
  id: string;
  title: string;
  summary: string | null;
  category: EventCategory;
  category_labels?: Record<string, string>;
  location: string | null;
  external_url: string | null;
  starts_at: string;
  ends_at: string;
  event_phase: EventPhase;
}

export interface EventPublicDetail extends EventSummary {
  content_markdown: string;
  visible_from_at: string;
  visible_until_at: string | null;
}

export interface EventAdminDetail extends EventPublicDetail {
  status: EventStatus;
  published_at: string | null;
  archived_at: string | null;
  archive_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface EventListResponse<TEvent = EventSummary> {
  items: TEvent[];
  total: number;
  page: number;
  size: number;
}

export interface EventAuditEvent {
  id: string;
  actor_id: string;
  action: string;
  details: Record<string, unknown>;
  created_at: string;
}

export interface EventWritePayload {
  title: string;
  summary: string | null;
  content_markdown: string;
  category: EventCategory;
  location: string | null;
  external_url: string | null;
  starts_at: string;
  ends_at: string;
  visible_from_at: string;
  visible_until_at: string | null;
  status: EventWritableStatus;
}
