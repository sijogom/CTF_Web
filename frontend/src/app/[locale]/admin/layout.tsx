"use client";

import { AdminGuard } from "@/components/admin/AdminGuard";
import { AdminSidebar } from "@/components/admin/AdminSidebar";
import { useAuth } from "@/providers/AuthProvider";

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const { isSuperAdmin } = useAuth();

  return (
    <AdminGuard>
      <div className="flex min-h-[calc(100vh-4rem)] flex-col lg:flex-row">
        <AdminSidebar isSuperAdmin={isSuperAdmin} />
        <div className="min-w-0 flex-1 overflow-auto p-[8px] sm:p-6">
          {children}
        </div>
      </div>
    </AdminGuard>
  );
}
