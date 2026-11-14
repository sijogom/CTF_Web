"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { useTranslations } from "next-intl";
import type RFB from "@novnc/novnc/core/rfb";

/* ---------- 상태 타입 ---------- */

type ConnectionStatus = "connecting" | "connected" | "disconnected" | "error";

/* ---------- Props ---------- */

interface VncViewerProps {
  /** WebSocket URL (ws:// 또는 wss://) — env-orchestrator VNC 프록시 */
  wsUrl: string;
  /** 연결 해제 시 부모에 알림 */
  onDisconnect?: () => void;
  /** 인스턴스 ID (타이틀 바 표시용, 선택) */
  instanceId?: string;
}

/* ---------- 컴포넌트 ---------- */

export function VncViewer({ wsUrl, onDisconnect, instanceId }: VncViewerProps) {
  const t = useTranslations("vnc");
  const containerRef = useRef<HTMLDivElement>(null);
  const rfbRef = useRef<RFB | null>(null);
  const onDisconnectRef = useRef(onDisconnect);
  onDisconnectRef.current = onDisconnect;

  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [errorMsg, setErrorMsg] = useState("");
  const [isFullscreen, setIsFullscreen] = useState(false);

  /* noVNC RFB 연결 — useEffect 내 동적 import로 SSR 방지 */
  useEffect(() => {
    if (!containerRef.current || !wsUrl) return;

    let cancelled = false;
    let rfb: RFB | null = null;

    const connect = async () => {
      try {
        // 브라우저 전용 동적 import (Next.js SSR에서 실행되지 않음)
        const { default: RFBClass } = await import("@novnc/novnc/core/rfb");

        if (cancelled || !containerRef.current) return;

        rfb = new RFBClass(containerRef.current, wsUrl, {
          wsProtocols: ["binary"],
        });
        rfb.scaleViewport = true;
        rfb.resizeSession = true;
        rfb.clipViewport = true;

        rfb.addEventListener("connect", () => {
          if (!cancelled) setStatus("connected");
        });

        rfb.addEventListener("disconnect", (e) => {
          if (cancelled) return;
          setStatus("disconnected");
          if (!e.detail.clean) {
            setErrorMsg(t("disconnectedUnclean"));
          }
          onDisconnectRef.current?.();
        });

        rfb.addEventListener("securityfailure", () => {
          if (cancelled) return;
          setStatus("error");
          setErrorMsg(t("authFailed"));
        });

        rfbRef.current = rfb;
      } catch {
        if (!cancelled) {
          setStatus("error");
          setErrorMsg(t("loadFailed"));
        }
      }
    };

    connect();

    return () => {
      cancelled = true;
      if (rfb) {
        try {
          rfb.disconnect();
        } catch {
          /* 무시 */
        }
      }
      rfbRef.current = null;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsUrl]);

  /* 전체 화면 토글 */
  const toggleFullscreen = useCallback(() => {
    const el = containerRef.current?.parentElement;
    if (!el) return;

    if (!document.fullscreenElement) {
      el.requestFullscreen().then(() => setIsFullscreen(true)).catch(() => {});
    } else {
      document.exitFullscreen().then(() => setIsFullscreen(false)).catch(() => {});
    }
  }, []);

  /* 전체 화면 해제 이벤트 동기화 */
  useEffect(() => {
    const handler = () => {
      if (!document.fullscreenElement) setIsFullscreen(false);
    };
    document.addEventListener("fullscreenchange", handler);
    return () => document.removeEventListener("fullscreenchange", handler);
  }, []);

  /* Ctrl+Alt+Del 전송 */
  const sendCtrlAltDel = useCallback(() => {
    rfbRef.current?.sendCtrlAltDel();
  }, []);

  return (
    <div className="flex flex-col rounded-xl border border-border bg-bg-surface overflow-hidden">
      {/* 타이틀 바 */}
      <div className="flex items-center gap-2 border-b border-border px-4 py-2">
        <svg
          className="h-4 w-4 text-text-muted"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
        >
          <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
          <line x1="8" y1="21" x2="16" y2="21" />
          <line x1="12" y1="17" x2="12" y2="21" />
        </svg>
        <span className="text-xs text-text-muted">
          {t("remoteDesktop")}
          {instanceId ? ` — ${instanceId}` : ""}
        </span>

        {/* 상태 인디케이터 */}
        <div
          className={`h-2 w-2 rounded-full ${
            status === "connected"
              ? "bg-accent-success animate-pulse"
              : status === "connecting"
                ? "bg-accent-warning animate-pulse"
                : "bg-text-muted"
          }`}
        />

        {/* 우측 툴바 */}
        <div className="ml-auto flex items-center gap-1">
          {status === "connected" && (
            <>
              {/* Ctrl+Alt+Del 버튼 */}
              <button
                type="button"
                onClick={sendCtrlAltDel}
                className="rounded px-2 py-0.5 text-[10px] font-medium text-text-muted transition-colors hover:bg-bg-primary hover:text-text-primary"
                title="Ctrl+Alt+Del"
              >
                CAD
              </button>
              {/* 전체 화면 토글 */}
              <button
                type="button"
                onClick={toggleFullscreen}
                className="rounded p-1 text-text-muted transition-colors hover:bg-bg-primary hover:text-text-primary"
                title={t("fullscreen")}
              >
                {isFullscreen ? (
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 9V4.5M9 9H4.5M9 9L3.75 3.75M9 15v4.5M9 15H4.5M9 15l-5.25 5.25M15 9h4.5M15 9V4.5M15 9l5.25-5.25M15 15h4.5M15 15v4.5m0-4.5l5.25 5.25" />
                  </svg>
                ) : (
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9M3.75 20.25v-4.5m0 4.5h4.5m-4.5 0L9 15M20.25 3.75h-4.5m4.5 0v4.5m0-4.5L15 9m5.25 11.25h-4.5m4.5 0v-4.5m0 4.5L15 15" />
                  </svg>
                )}
              </button>
            </>
          )}
        </div>
      </div>

      {/* VNC 뷰어 영역 */}
      <div className="relative bg-black" style={{ minHeight: "480px" }}>
        {/* 연결 중 오버레이 */}
        {status === "connecting" && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/80">
            <div className="text-center">
              <div className="mx-auto mb-3 h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
              <p className="text-sm text-text-muted">{t("connecting")}</p>
            </div>
          </div>
        )}

        {/* 연결 해제/에러 오버레이 */}
        {(status === "disconnected" || status === "error") && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/80">
            <div className="text-center">
              <svg
                className="mx-auto mb-3 h-10 w-10 text-accent-error"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M12 9v2m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                />
              </svg>
              <p className="text-sm text-text-secondary">
                {errorMsg || t("disconnectedClean")}
              </p>
            </div>
          </div>
        )}

        {/* noVNC 캔버스 마운트 포인트 — RFB가 여기에 <canvas>를 삽입 */}
        <div ref={containerRef} className="w-full" />
      </div>
    </div>
  );
}

/* default export도 제공 (dynamic import 호환) */
export default VncViewer;
