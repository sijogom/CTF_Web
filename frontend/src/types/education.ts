/**
 * Learn 교육 운영 API의 실제 공개 계약.
 *
 * 서버가 교육 역할, 수강 등록, 공개 기간, 자료 다운로드를 최종 인가한다.
 * 이 타입은 UI 표현과 요청 직렬화에만 사용하며 클라이언트 역할 판단은 보안 경계가 아니다.
 */

export const EDUCATION_CLASS_STATUSES = ["draft", "published", "archived"] as const;
export type EducationClassStatus = (typeof EDUCATION_CLASS_STATUSES)[number];

export type EducationClassWritableStatus = Exclude<EducationClassStatus, "archived">;

export const EDUCATION_ASSIGNMENT_STATUSES = ["draft", "published", "archived"] as const;
export type EducationAssignmentStatus = (typeof EDUCATION_ASSIGNMENT_STATUSES)[number];

export type EducationAssignmentWritableStatus = Exclude<EducationAssignmentStatus, "archived">;

export const EDUCATION_ENROLLMENT_STATUSES = ["active", "dropped"] as const;
export type EducationEnrollmentStatus = (typeof EDUCATION_ENROLLMENT_STATUSES)[number];

export type EducationGradeStatus = "draft" | "published";

export interface EducationPermissions {
  user_id: string;
  is_education_manager: boolean;
  is_platform_admin: boolean;
  education_role: "professor_admin" | null;
}

export interface EducationClassSummary {
  id: string;
  title: string;
  summary: string | null;
  instructor_id: string;
  status: EducationClassStatus;
  starts_at: string | null;
  ends_at: string | null;
  created_at: string;
}

export interface EducationResourceLink {
  id: string;
  resource_type: "course" | "lesson" | "wargame";
  course_id: string | null;
  lesson_id: string | null;
  challenge_id: number | null;
  label: string | null;
  /** Server-resolved local route. Null means the linked content is stale. */
  href: string | null;
  created_at: string;
}

export interface EducationResourceLinkCreatePayload {
  resource_type: EducationResourceLink["resource_type"];
  course_id?: string;
  lesson_id?: string;
  challenge_id?: number;
  label?: string | null;
}

export interface EducationClassDetail extends EducationClassSummary {
  description_markdown: string;
  published_at: string | null;
  archived_at: string | null;
  archive_reason: string | null;
  resources: EducationResourceLink[];
  is_enrolled: boolean;
  is_education_manager: boolean;
}

export interface EducationEnrollment {
  id: string;
  class_id: string;
  student_id: string;
  status: EducationEnrollmentStatus;
  enrolled_at: string;
  dropped_at: string | null;
}

export interface EducationAssignmentSummary {
  id: string;
  class_id: string;
  title: string;
  status: EducationAssignmentStatus;
  max_score: number;
  available_from_at: string | null;
  due_at: string | null;
  created_at: string;
}

export interface EducationAssignment extends EducationAssignmentSummary {
  instructions_markdown: string;
  published_at: string | null;
  archived_at: string | null;
  archive_reason: string | null;
}

export interface EducationSubmission {
  id: string;
  assignment_id: string;
  student_id: string;
  submitted_at: string;
  updated_at: string;
  content_length: number;
  grade_status: EducationGradeStatus | null;
  content_markdown?: string;
}

export interface EducationGrade {
  id: string;
  submission_id: string;
  score: number;
  status: EducationGradeStatus;
  graded_by: string;
  graded_at: string;
  published_at: string | null;
  feedback_markdown: string | null;
}

export interface EducationOwnSubmissionResponse {
  submission: EducationSubmission & { content_markdown: string };
  grade: EducationGrade | null;
}

export interface EducationRoleGrant {
  id: string;
  subject_id: string;
  role: "professor_admin";
  granted_by: string;
  grant_reason: string;
  granted_at: string;
  revoked_by: string | null;
  revoke_reason: string | null;
  revoked_at: string | null;
  active: boolean;
}

export interface EducationAsset {
  id: string;
  class_id: string;
  assignment_id: string | null;
  scope: "class" | "assignment";
  stored_name: string;
  original_name: string;
  content_type: string;
  byte_size: number;
  uploaded_by: string;
  created_at: string;
  deleted_at: string | null;
}

export interface EducationAssetUsage {
  class_id: string;
  active_asset_count: number;
  active_byte_size: number;
  max_assets: number;
  max_byte_size: number;
  max_file_size: number;
}

export interface EducationCollection<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
}

export interface EducationClassWritePayload {
  title: string;
  summary: string | null;
  description_markdown: string;
  starts_at: string | null;
  ends_at: string | null;
  status: EducationClassWritableStatus;
}

export interface EducationAssignmentWritePayload {
  title: string;
  instructions_markdown: string;
  available_from_at: string | null;
  due_at: string | null;
  max_score: number;
  status: EducationAssignmentWritableStatus;
}

export interface EducationSubmissionWritePayload {
  content_markdown: string;
}

export interface EducationGradePayload {
  score: number;
  feedback_markdown: string | null;
  status: EducationGradeStatus;
}
