"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { WebTerminal } from "@/components/terminal/WebTerminal";
import type { EnvInstance } from "@/types/challenge";

interface EnvInstanceSectionProps {
  envInstance: EnvInstance | null;
  envLoading: boolean;
  envError: string | null;
  timeRemaining: string;
  extendCount: number | null;
  maxExtendCount: number | null;
  extensionDisabled: boolean;
  environmentReady: boolean;
  environmentUnavailableTitle: string;
  environmentUnavailableDescription: string;
  onStart: () => void;
  onStop: () => void;
  onExtend: () => void;
  showStopConfirm: boolean;
  onStopConfirmToggle: (show: boolean) => void;
  getAccessUrl: (instanceId: string, port: number) => string;
}

export function EnvInstanceSection({
  envInstance,
  envLoading,
  envError,
  timeRemaining,
  extendCount,
  maxExtendCount,
  extensionDisabled,
  environmentReady,
  environmentUnavailableTitle,
  environmentUnavailableDescription,
  onStart,
  onStop,
  onExtend,
  showStopConfirm,
  onStopConfirmToggle,
  getAccessUrl,
}: EnvInstanceSectionProps) {
  const t = useTranslations("wargame");
  const tt = useTranslations("terminal");
  const tc = useTranslations("common");
  const [copied, setCopied] = useState(false);
  const [showTerminal, setShowTerminal] = useState(false);
  const accessUrl = envInstance ? getAccessUrl(envInstance.id, envInstance.port) : "";

  return (
    <>
      <div className="rounded-xl border border-border bg-bg-surface overflow-hidden">
        {/* 헤더 */}
        <div className="flex items-center gap-2 border-b border-border bg-bg-primary/50 px-6 py-3">
          <svg className="h-4 w-4 text-accent-primary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2" />
          </svg>
          <h2 className="text-sm font-semibold">{t("practiceEnv")}</h2>
        </div>

        <div className="p-6">
          {!envInstance ? (
            <div className="space-y-4">
              <p className="text-sm text-text-secondary">{t("envDescription")}</p>

              {!environmentReady && (
                <div className="rounded-lg border border-accent-warning/30 bg-accent-warning/10 p-3" role="status">
                  <p className="text-sm font-medium text-accent-warning">{environmentUnavailableTitle}</p>
                  <p className="mt-1 text-xs text-text-secondary">{environmentUnavailableDescription}</p>
                </div>
              )}

              {envLoading ? (
                /* 로딩 상태 -- 서버 생성 중 */
                <div className="flex flex-col items-center gap-4 rounded-lg border border-accent-primary/20 bg-accent-primary/5 py-8">
                  <div className="relative">
                    <svg className="h-10 w-10 animate-spin text-accent-primary" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                  </div>
                  <div className="text-center">
                    <p className="text-sm font-semibold text-text-primary">{t("envPreparing")}</p>
                    <p className="mt-1 text-xs text-text-muted">{t("envPreparingDesc")}</p>
                  </div>
                </div>
              ) : (
                /* 서버 생성 버튼 */
                <button
                  onClick={onStart}
                  disabled={!environmentReady}
                  className="flex w-full items-center justify-center gap-2 rounded-lg bg-accent-primary py-3.5 text-sm font-semibold text-white transition-all hover:bg-accent-primary/90 active:scale-[0.98]"
                >
                  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                    <path strokeLinecap="round" strokeLinejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  {t("startEnv")}
                </button>
              )}

              {envError && (
                <div className="rounded-lg border border-accent-error/30 bg-accent-error/10 p-3">
                  <p className="text-sm text-accent-error">{envError}</p>
                  <button
                    onClick={onStart}
                    disabled={envLoading || !environmentReady}
                    className="mt-2 rounded-lg bg-accent-error/20 px-4 py-1.5 text-xs font-medium text-accent-error transition-colors hover:bg-accent-error/30 disabled:opacity-50"
                  >
                    {tc("retry")}
                  </button>
                </div>
              )}
            </div>
          ) : (
            <div className="space-y-4">
              {/* 상단: 상태 + 남은 시간 */}
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="inline-block h-2 w-2 rounded-full bg-accent-success animate-pulse" />
                  <span className="text-sm font-medium text-accent-success">{t("envRunning")}</span>
                </div>
                <div className="flex items-center gap-1.5 rounded-full bg-bg-primary px-3 py-1">
                  <svg className="h-3.5 w-3.5 text-text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <span className="font-mono text-sm font-semibold text-text-primary">{timeRemaining}</span>
                </div>
              </div>

              {/* 접속 URL */}
              <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-primary">
                <div className="flex-1 overflow-x-auto px-4 py-3">
                  <a
                    href={accessUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="font-mono text-sm font-medium text-accent-primary hover:underline"
                  >
                    {accessUrl}
                  </a>
                </div>
                <button
                  onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(accessUrl);
                      setCopied(true);
                      setTimeout(() => setCopied(false), 2000);
                    } catch {
                      // 클립보드 API 차단 시 (HTTP 환경 등) 무시
                    }
                  }}
                  className="flex shrink-0 items-center gap-1 border-l border-border px-4 py-3 text-xs font-medium text-text-muted transition-colors hover:bg-bg-surface hover:text-text-primary"
                >
                  {copied ? (
                    <>
                      <svg className="h-3.5 w-3.5 text-accent-success" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                      </svg>
                      {t("copied")}
                    </>
                  ) : (
                    <>
                      <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                      </svg>
                      {t("copy")}
                    </>
                  )}
                </button>
              </div>

              {/* 액션 버튼 */}
              <div className="flex flex-wrap gap-2">
                <button
                  onClick={() => setShowTerminal((open) => !open)}
                  className="flex items-center gap-1.5 rounded-lg border border-accent-primary/30 px-4 py-2 text-xs font-medium text-accent-primary transition-colors hover:bg-accent-primary/10"
                  aria-expanded={showTerminal}
                >
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
                  </svg>
                  {showTerminal ? tt("disconnect") : tt("connect")}
                </button>
                <button
                  onClick={onExtend}
                  disabled={extensionDisabled}
                  aria-label={`${t("extendTime")} (${extendCount ?? "—"}/${maxExtendCount ?? "—"})`}
                  className="flex items-center gap-1.5 rounded-lg border border-border px-4 py-2 text-xs font-medium text-text-secondary transition-colors hover:bg-bg-primary hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6l4 2" />
                    <circle cx="12" cy="12" r="9" />
                  </svg>
                  {t("extendTime")} ({extendCount ?? "—"}/{maxExtendCount ?? "—"})
                </button>
                <button
                  onClick={() => onStopConfirmToggle(true)}
                  className="flex items-center gap-1.5 rounded-lg border border-accent-error/30 px-4 py-2 text-xs font-medium text-accent-error transition-colors hover:bg-accent-error/10"
                >
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" />
                  </svg>
                  {t("stopEnv")}
                </button>
              </div>
              {showTerminal && (
                <WebTerminal instanceId={envInstance?.id ?? ""} />
              )}
            </div>
          )}
        </div>

      {/* 환경 정지 확인 */}
      </div>

      <ConfirmModal
        open={showStopConfirm}
        title={t("stopEnvTitle")}
        description={t("stopEnvDesc")}
        confirmLabel={t("stopEnv")}
        variant="danger"
        onConfirm={() => { onStopConfirmToggle(false); onStop(); }}
        onCancel={() => onStopConfirmToggle(false)}
      />
    </>
  );
}
