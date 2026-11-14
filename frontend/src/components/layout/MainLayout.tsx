"use client";

import { Header } from "./Header";
import { Footer } from "./Footer";
import { useAuth } from "@/providers/AuthProvider";

export function MainLayout({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  // unknown은 기존 user를 보존한다. 확정된 계정 변경만 이전 화면 상태를 폐기한다.
  const accountKey = user?.id ?? "anonymous";
  return (
    <div className="flex min-h-screen flex-col">
      <Header />
      <main key={accountKey} id="main-content" className="mx-auto w-full max-w-7xl flex-1 px-6 py-8">
        {children}
      </main>
      <Footer />
    </div>
  );
}
