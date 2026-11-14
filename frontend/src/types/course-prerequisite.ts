/** 관리자 코스 선행조건 API의 전용 계약. */
export interface CoursePrerequisiteCandidate {
  id: string;
  title: string;
  is_published: boolean;
  is_deleted: boolean;
  status: string;
  lesson_count: number;
}

export interface CoursePrerequisiteReplaceResponse {
  course_id: string;
  prerequisite_ids: string[];
}

export interface CoursePrerequisiteAccess {
  access_state: "available" | "locked" | "sign_in_required";
  reason_code:
    | "all_prerequisites_completed"
    | "authentication_required"
    | "prerequisites_incomplete"
    | "required_content_unavailable";
  next_action: {
    kind: "open_course" | "open_prerequisite_course" | "sign_in" | "contact_admin";
    href: string;
  };
}
