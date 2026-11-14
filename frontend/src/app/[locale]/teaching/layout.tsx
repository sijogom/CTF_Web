"use client";

import { TeachingAccessGate } from "@/components/teaching/TeachingAccessGate";
import { TeachingNavigation } from "@/components/teaching/TeachingNavigation";
import { EducationAccessProvider } from "@/components/teaching/EducationAccessProvider";

export default function TeachingLayout({ children }: { children: React.ReactNode }) {
  return (
    <EducationAccessProvider>
      <TeachingAccessGate>
        <div className="mx-auto w-full max-w-7xl space-y-6">
          <TeachingNavigation />
          {children}
        </div>
      </TeachingAccessGate>
    </EducationAccessProvider>
  );
}
