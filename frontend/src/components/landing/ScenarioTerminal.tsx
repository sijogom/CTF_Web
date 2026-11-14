"use client";

import { useState, useEffect, useRef, useCallback } from "react";

// ──── 시나리오 타입 정의 ────

export type ScenarioStep =
  | { type: "typing"; text: string; speed?: number; style?: "prompt" | "command" | "output" | "success" }
  | { type: "scramble"; text: string; duration?: number; style?: "prompt" | "command" | "output" | "success" }
  | { type: "pause"; duration: number }
  | { type: "clear" };

interface TerminalLine {
  text: string;
  style: "prompt" | "command" | "output" | "success";
}

interface ScenarioTerminalProps {
  scenario: ScenarioStep[];
  className?: string;
}

// ──── 상수 ────

const STYLE_CLASS: Record<string, string> = {
  prompt:  "text-emerald-400",
  command: "text-gray-100",
  output:  "text-gray-400",
  success: "font-bold text-cyan-400",
};

const SCRAMBLE_CHARS = "█▓▒░▐▌@#$%&*!?<>{}=|~^";

// ──── 컴포넌트 ────

export function ScenarioTerminal({ scenario, className = "" }: ScenarioTerminalProps) {
  const [lines, setLines] = useState<TerminalLine[]>([]);
  const [cursorVisible, setCursorVisible] = useState(true);
  const cancelledRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  // 새 줄 추가 시 자동 스크롤
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [lines]);

  // ── 타이핑 step ──
  const runTyping = useCallback((step: Extract<ScenarioStep, { type: "typing" }>) => {
    return new Promise<void>((resolve) => {
      const { text, speed = 30, style = "output" } = step;

      if (speed === 0 || text === "") {
        setLines((prev) => [...prev, { text, style }]);
        resolve();
        return;
      }

      let currentText = "";
      let charIdx = 0;
      setLines((prev) => [...prev, { text: "", style }]);

      const interval = setInterval(() => {
        if (cancelledRef.current) { clearInterval(interval); resolve(); return; }
        if (charIdx >= text.length) { clearInterval(interval); resolve(); return; }
        currentText += text[charIdx];
        charIdx++;
        setLines((prev) => {
          const next = [...prev];
          next[next.length - 1] = { text: currentText, style };
          return next;
        });
      }, speed);
    });
  }, []);

  // ── 스크램블 디코드 step ──
  // 랜덤 문자가 빠르게 순환 → 왼쪽부터 하나씩 실제 글자로 고정
  const runScramble = useCallback((step: Extract<ScenarioStep, { type: "scramble" }>) => {
    return new Promise<void>((resolve) => {
      const { text, duration = 2000, style = "success" } = step;

      if (text === "") {
        setLines((prev) => [...prev, { text: "", style }]);
        resolve();
        return;
      }

      const frameMs = 40;
      const warmupFrames = 12; // ~480ms 순수 스크램블
      const totalFrames = Math.floor(duration / frameMs);
      const revealEvery = Math.max(1, Math.floor((totalFrames - warmupFrames) / text.length));
      let frame = 0;
      let revealed = 0;

      setLines((prev) => [...prev, { text: "", style }]);

      const timer = setInterval(() => {
        if (cancelledRef.current) { clearInterval(timer); resolve(); return; }

        frame++;

        // 워밍업 이후 매 revealEvery 프레임마다 한 글자씩 확정
        if (frame > warmupFrames && frame % revealEvery === 0 && revealed < text.length) {
          revealed++;
        }

        // 확정된 부분 + 스크램블 중인 부분 조합
        let display = "";
        for (let i = 0; i < text.length; i++) {
          if (i < revealed || text[i] === " ") {
            display += text[i];
          } else {
            display += SCRAMBLE_CHARS[Math.floor(Math.random() * SCRAMBLE_CHARS.length)];
          }
        }

        setLines((prev) => {
          const next = [...prev];
          next[next.length - 1] = { text: display, style };
          return next;
        });

        if (revealed >= text.length) {
          clearInterval(timer);
          resolve();
        }
      }, frameMs);
    });
  }, []);

  // ── pause step ──
  const runPause = useCallback((step: Extract<ScenarioStep, { type: "pause" }>) => {
    return new Promise<void>((resolve) => {
      if (cancelledRef.current) { resolve(); return; }
      setTimeout(() => resolve(), step.duration);
    });
  }, []);

  // ── clear step ──
  const runClear = useCallback(() => {
    setLines([]);
    return Promise.resolve();
  }, []);

  // ── 시나리오 실행기 (자동 반복) ──
  // Strict Mode에서 2번 실행되어도 이전 실행이 확실히 중단되도록
  // 각 실행마다 고유 AbortController 사용
  useEffect(() => {
    const abortController = new AbortController();
    cancelledRef.current = false;
    setLines([]);

    const sleep = (ms: number) =>
      new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, ms);
        abortController.signal.addEventListener("abort", () => {
          clearTimeout(timer);
          resolve();
        });
      });

    const run = async () => {
      // Strict Mode 이중 실행 방지: 첫 렌더 직후 약간 대기
      await sleep(50);
      if (abortController.signal.aborted) return;

      while (!abortController.signal.aborted) {
        for (const step of scenario) {
          if (abortController.signal.aborted) return;
          switch (step.type) {
            case "typing":   await runTyping(step); break;
            case "scramble":  await runScramble(step); break;
            case "pause":    await runPause(step); break;
            case "clear":    await runClear(); break;
          }
        }
      }
    };

    run();
    return () => {
      cancelledRef.current = true;
      abortController.abort();
    };
  }, [scenario, runTyping, runScramble, runPause, runClear]);

  // 커서 깜빡임
  useEffect(() => {
    const timer = setInterval(() => setCursorVisible((v) => !v), 530);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className={`overflow-hidden rounded-xl border border-gray-700/50 bg-[#0d1117] shadow-2xl shadow-black/50 ${className}`}>
      {/* 제목바 */}
      <div className="flex items-center gap-2 border-b border-gray-700/50 bg-[#161b22] px-4 py-2.5">
        <span className="h-3 w-3 rounded-full bg-[#FF5F57]" />
        <span className="h-3 w-3 rounded-full bg-[#FEBC2E]" />
        <span className="h-3 w-3 rounded-full bg-[#28C840]" />
        <span className="ml-3 font-mono text-[11px] text-gray-500">user@cuha:~/challenges</span>
      </div>

      {/* 터미널 — 고정 높이 + 자동 스크롤 */}
      <div
        ref={scrollRef}
        className="h-[380px] overflow-y-auto p-5 font-mono text-[13px] leading-[1.7] [&::-webkit-scrollbar]:hidden [-ms-overflow-style:none] [scrollbar-width:none]"
      >
        {lines.map((line, i) => (
          <div key={i} className={STYLE_CLASS[line.style] ?? STYLE_CLASS.output}>
            {line.text || "\u00A0"}
          </div>
        ))}
        {cursorVisible && (
          <span className="inline-block h-[18px] w-[8px] bg-emerald-400" />
        )}
      </div>
    </div>
  );
}
