import assert from "node:assert/strict";
import test from "node:test";
import { hasLearningPathContent, learningPathAction } from "../src/lib/learning-path-presentation.mjs";

function projection(overrides = {}) {
  return { target: { href: "/wargame/42" }, required_steps: [], recommended_steps: [], helpful_content: [],
    next_action: { kind: "open_challenge", href: "/wargame/42" }, ...overrides };
}

test("a self-link does not create an empty learning path or generic next-action section", () => {
  const value = projection();
  assert.equal(learningPathAction(value), null);
  assert.equal(hasLearningPathContent(value), false);
});

test("real required recommended and helpful content remain visible independently", () => {
  for (const key of ["required_steps", "recommended_steps", "helpful_content"]) {
    assert.equal(hasLearningPathContent(projection({ [key]: [{ name: "공개 학습" }] })), true);
  }
});

test("a next lesson is named by its actual incomplete requirement", () => {
  const value = projection({ required_steps: [{ name: "설치와 시작", href: "/learn/course/lesson", status: "in_progress" }],
    next_action: { kind: "open_content", href: "/learn/course/lesson" } });
  assert.deepEqual(learningPathAction(value), { kind: "open_content", href: "/learn/course/lesson", name: "설치와 시작" });
  for (const status of ["completed", "unavailable"]) {
    assert.equal(learningPathAction({ ...value, required_steps: [{ ...value.required_steps[0], status }] }), null);
  }
});

test("unknown and unsafe actions cannot become generic content links", () => {
  for (const action of [{ kind: "unrecognized", href: "/anything" }, { kind: "open_content", href: "/missing" },
    { kind: "contact_admin", href: "//external.invalid" }, { kind: "sign_in", href: "/\\external.invalid" },
    { kind: "sign_in", href: "/login\u0000" }]) {
    assert.equal(learningPathAction(projection({ next_action: action })), null);
  }
});

test("sign-in and administrator recovery actions retain their specific meaning", () => {
  for (const [kind, href] of [["sign_in", "/login?next=/wargame/42"], ["contact_admin", "/inquiry"]]) {
    const value = projection({ next_action: { kind, href } });
    assert.deepEqual(learningPathAction(value), { kind, href });
    assert.equal(hasLearningPathContent(value), true);
  }
});
