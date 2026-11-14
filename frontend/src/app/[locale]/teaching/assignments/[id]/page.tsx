"use client";

import { useParams } from "next/navigation";
import { TeachingAssignmentDetail } from "@/components/teaching/TeachingAssignmentDetail";

export default function TeachingAssignmentDetailPage() {
  const params = useParams<{ id: string }>();
  return <TeachingAssignmentDetail assignmentId={params.id} />;
}
