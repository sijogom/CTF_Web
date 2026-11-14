/**
 * 메인 히어로 터미널 시나리오
 *
 * 스크램블 디코드 효과 — 랜덤 문자 → 실제 글자로 고정
 * 가짜 통계 없음. 실제 카테고리 + 간결한 태그라인.
 */
import type { ScenarioStep } from "../ScenarioTerminal";

export const heroScenario: ScenarioStep[] = [
  { type: "typing", text: "> init cuha-ctf", style: "command", speed: 40 },
  { type: "pause", duration: 400 },
  { type: "typing", text: "Loading modules...", style: "output", speed: 20 },
  { type: "pause", duration: 300 },
  { type: "typing", text: "[web] [pwn] [rev] [crypto] [forensic]", style: "prompt", speed: 0 },
  { type: "typing", text: "", style: "output", speed: 0 },

  // 스크램블 디코드 — 핵심 어그로
  { type: "scramble", text: "  CUHA MEMORY CTF", style: "success", duration: 2000 },
  { type: "typing", text: "", style: "output", speed: 0 },

  { type: "typing", text: "  Break it to learn it.", style: "output", speed: 35 },
  { type: "typing", text: "", style: "output", speed: 0 },
  { type: "typing", text: "> _", style: "success", speed: 0 },

  { type: "pause", duration: 4000 },
  { type: "clear" },
];
