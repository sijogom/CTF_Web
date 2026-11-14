import { apiFetch } from "@/lib/api-client";
import { collectEducationPages } from "@/lib/education-state.mjs";
import type {
  EducationAssignment,
  EducationAssignmentSummary,
  EducationAsset,
  EducationAssetUsage,
  EducationClassDetail,
  EducationClassSummary,
  EducationCollection,
  EducationEnrollment,
  EducationOwnSubmissionResponse,
  EducationPermissions,
  EducationResourceLink,
  EducationResourceLinkCreatePayload,
  EducationRoleGrant,
  EducationSubmission,
} from "@/types/education";

export const EDUCATION_API = "/api/learn/education";

/** 일부 교육 목록은 배열, 나머지는 페이지네이션 객체로 응답한다. */
export function educationItems<T>(response: EducationCollection<T> | T[]): T[] {
  return Array.isArray(response) ? response : response.items;
}

export function getEducationClasses(page = 1, size = 100) {
  return apiFetch<EducationCollection<EducationClassSummary>>(`${EDUCATION_API}/classes?page=${page}&size=${size}`);
}

export function getEducationPermissions() {
  return apiFetch<EducationPermissions>(`${EDUCATION_API}/me/permissions`);
}

export function getEducationClass(classId: string) {
  return apiFetch<EducationClassDetail>(`${EDUCATION_API}/classes/${classId}`);
}

export function getClassEnrollments(classId: string) {
  return apiFetch<EducationEnrollment[]>(`${EDUCATION_API}/classes/${classId}/enrollments`);
}

export function getClassAssignments(classId: string, page = 1, size = 100) {
  return apiFetch<EducationCollection<EducationAssignmentSummary>>(`${EDUCATION_API}/classes/${classId}/assignments?page=${page}&size=${size}`);
}

export async function getAllClassAssignments(classId: string) {
  return collectEducationPages((page, size) => getClassAssignments(classId, page, size));
}

export async function getAllEducationClasses() {
  return collectEducationPages((page, size) => getEducationClasses(page, size));
}

export function getEducationAssignment(assignmentId: string) {
  return apiFetch<EducationAssignment>(`${EDUCATION_API}/assignments/${assignmentId}`);
}

export function getOwnSubmission(assignmentId: string) {
  return apiFetch<EducationOwnSubmissionResponse>(`${EDUCATION_API}/assignments/${assignmentId}/submission`);
}

export function getAssignmentSubmissions(assignmentId: string, page = 1, size = 100) {
  return apiFetch<EducationCollection<EducationSubmission>>(`${EDUCATION_API}/assignments/${assignmentId}/submissions?page=${page}&size=${size}`);
}

export async function getAllAssignmentSubmissions(assignmentId: string) {
  return collectEducationPages((page, size) => getAssignmentSubmissions(assignmentId, page, size));
}

export function getAssignmentSubmissionForGrading(assignmentId: string, studentId: string) {
  return apiFetch<EducationOwnSubmissionResponse>(`${EDUCATION_API}/assignments/${assignmentId}/submissions/${studentId}`);
}

export function getRoleGrants() {
  return apiFetch<EducationRoleGrant[]>(`${EDUCATION_API}/role-grants`);
}

export function getClassAssets(classId: string, page = 1, size = 100) {
  return apiFetch<EducationCollection<EducationAsset>>(`${EDUCATION_API}/classes/${classId}/assets?page=${page}&size=${size}`);
}

export function getAssignmentAssets(assignmentId: string, page = 1, size = 100) {
  return apiFetch<EducationCollection<EducationAsset>>(`${EDUCATION_API}/assignments/${assignmentId}/assets?page=${page}&size=${size}`);
}

export async function getAllClassAssets(classId: string) {
  return collectEducationPages((page, size) => getClassAssets(classId, page, size));
}

export async function getAllAssignmentAssets(assignmentId: string) {
  return collectEducationPages((page, size) => getAssignmentAssets(assignmentId, page, size));
}

export function getClassAssetUsage(classId: string) {
  return apiFetch<EducationAssetUsage>(`${EDUCATION_API}/classes/${classId}/asset-usage`);
}

export function createClassResource(classId: string, body: EducationResourceLinkCreatePayload) {
  return apiFetch<EducationResourceLink>(`${EDUCATION_API}/classes/${classId}/resources`, {
    method: "POST",
    body,
  });
}

export function deleteClassResource(classId: string, resourceId: string) {
  return apiFetch<void>(`${EDUCATION_API}/classes/${classId}/resources/${resourceId}`, {
    method: "DELETE",
  });
}

export function educationAssetDownloadHref(assetId: string): string {
  const basePath = process.env.NEXT_PUBLIC_BASE_PATH || "/ctf";
  return `${basePath}${EDUCATION_API}/assets/${assetId}/download`;
}
