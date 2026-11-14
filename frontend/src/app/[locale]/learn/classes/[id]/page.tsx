"use client";

import { useParams } from "next/navigation";
import { StudentClassDetail } from "@/components/teaching/StudentClassDetail";

export default function StudentClassDetailPage() {
  const params = useParams<{ id: string }>();
  return <StudentClassDetail classId={params.id} />;
}
