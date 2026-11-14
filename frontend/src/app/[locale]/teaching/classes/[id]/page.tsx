"use client";

import { useParams } from "next/navigation";
import { TeachingClassDetail } from "@/components/teaching/TeachingClassDetail";

export default function TeachingClassDetailPage() {
  const params = useParams<{ id: string }>();
  return <TeachingClassDetail classId={params.id} />;
}
