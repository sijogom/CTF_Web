export interface EducationPage<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
}

export function collectEducationPages<T extends { id: string }>(
  fetchPage: (page: number, size: number) => Promise<EducationPage<T>>,
  pageSize?: number,
): Promise<T[]>;

export type EducationSubmissionAvailability = "open" | "classUnavailable" | "assignmentUnavailable" | "notOpen" | "closed" | "manager";

export function educationSubmissionAvailability(
  classItem: { status: string; is_education_manager: boolean },
  assignment: { status: string; available_from_at: string | null; due_at: string | null },
  now: number,
): EducationSubmissionAvailability;
