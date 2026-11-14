"use client";

import { useEffect, useRef, useCallback, useState } from "react";
import { useTranslations } from "next-intl";

interface WebTerminalProps {
  instanceId: string;
  wsUrl?: string;
  onDisconnect?: () => void;
}

/**
 * xterm.js 기반 웹 터미널 컴포넌트.
 *
 * WebSocket으로 오케스트레이터의 /ws/terminal/{instance_id}에 연결하여
 * 컨테이너 셸 입출력을 양방향 프록시한다.
 *
 * 의존성: @xterm/xterm, @xterm/addon-fit, @xterm/addon-web-links
 * (npm install 후 사용 가능)
 *
 * 현재는 xterm 미설치 환경을 위해 fallback textarea UI를 제공한다.
 */
const MAX_OUTPUT_LINES = 1000;

/** output 배열이 MAX_OUTPUT_LINES를 넘으면 앞부분을 잘라낸다. */
function appendOutput(prev: string[], ...lines: string[]): string[] {
  const next = [...prev, ...lines];
  return next.length > MAX_OUTPUT_LINES ? next.slice(next.length - MAX_OUTPUT_LINES) : next;
}

export function WebTerminal({ instanceId, wsUrl, onDisconnect }: WebTerminalProps) {
  const t = useTranslations("terminal");
  const termRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const onDisconnectRef = useRef(onDisconnect);
  onDisconnectRef.current = onDisconnect;
  const [connected, setConnected] = useState(false);
  const [output, setOutput] = useState<string[]>([]);
  const [input, setInput] = useState("");

  // 초기 메시지 설정 (t()는 useState 초기값에서 사용 불가)
  useEffect(() => {
    setOutput([
      `\x1b[32m[CUHA Memory]\x1b[0m ${t("connecting")}`,
      `${t("instance", { id: instanceId })}`,
      "",
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Caddy 리버스 프록시를 통해 연결 — 프로토콜 자동 감지 (https → wss, http → ws)
  const baseUrl = wsUrl || (typeof window !== "undefined"
    ? `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`
    : "ws://localhost");
  const fullUrl = `${baseUrl}/ctf/ws/terminal/${instanceId}`;

  const connect = useCallback(() => {
    try {
      const ws = new WebSocket(fullUrl);
      wsRef.current = ws;
      ws.binaryType = "arraybuffer";

      ws.onopen = () => {
        setConnected(true);
        setOutput((prev) => appendOutput(prev, `\x1b[32m[${t("connected")}]\x1b[0m ${t("sessionStarted")}\n`));
      };

      ws.onmessage = async (event) => {
        if (typeof event.data === "string") {
          setOutput((prev) => appendOutput(prev, event.data));
          return;
        }
        try {
          const bytes = event.data instanceof Blob
            ? await event.data.arrayBuffer()
            : event.data instanceof ArrayBuffer
              ? event.data
              : null;
          if (bytes) {
            setOutput((prev) => appendOutput(prev, new TextDecoder().decode(bytes)));
          }
        } catch {
          setOutput((prev) => appendOutput(prev, `\x1b[31m[Error]\x1b[0m ${t("wsError")}`));
        }
      };

      ws.onclose = () => {
        setConnected(false);
        setOutput((prev) => appendOutput(prev, `\n\x1b[31m[${t("disconnect")}]\x1b[0m ${t("sessionEnded")}`));
        onDisconnectRef.current?.();
      };

      ws.onerror = () => {
        setOutput((prev) => appendOutput(prev, `\x1b[31m[Error]\x1b[0m ${t("wsError")}`));
      };
    } catch {
      setOutput((prev) => appendOutput(prev, `\x1b[31m[Error]\x1b[0m ${t("cannotConnect")}`));
    }
  }, [fullUrl, t]);

  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSend = () => {
    if (!input.trim() || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(input + "\n");
    setOutput((prev) => appendOutput(prev, `$ ${input}`));
    setInput("");
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex flex-col rounded-xl border border-border bg-bg-primary font-mono text-sm">
      {/* 타이틀 바 */}
      <div className="flex items-center justify-between border-b border-border px-4 py-2">
        <div className="flex items-center gap-2">
          <div className={`h-2.5 w-2.5 rounded-full ${connected ? "bg-accent-success" : "bg-accent-error"}`} />
          <span className="text-xs text-text-muted">
            {connected ? t("connected") : t("disconnected")} — {instanceId}
          </span>
        </div>
        <button
          onClick={connected ? () => wsRef.current?.close() : connect}
          className="rounded px-2 py-0.5 text-xs text-text-muted hover:bg-bg-surface hover:text-text-primary"
        >
          {connected ? t("disconnect") : t("connect")}
        </button>
      </div>

      {/* 출력 영역 */}
      <div
        ref={termRef}
        className="h-80 overflow-y-auto whitespace-pre-wrap p-4 text-green-400"
      >
        {output.map((line, i) => (
          <div key={i}>{line}</div>
        ))}
      </div>

      {/* 입력 */}
      <div className="flex items-center border-t border-border px-4 py-2">
        <span className="mr-2 text-green-500">$</span>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={!connected}
          placeholder={connected ? t("inputPlaceholder") : t("clickConnect")}
          className="flex-1 bg-transparent text-green-400 placeholder:text-text-muted focus:outline-none"
        />
      </div>
    </div>
  );
}
