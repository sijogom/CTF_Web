import assert from "node:assert/strict";
import test from "node:test";

import { normalizeApiErrorPayload } from "../src/lib/api-error.mjs";

test("uses structured rich-content message and stable detail code", () => {
  const result = normalizeApiErrorPayload({
    detail: {
      code: "rich_content_review_required",
      message: "이전 형식의 본문을 검토해 주세요.",
      field: "description",
    },
  });

  assert.equal(result.message, "이전 형식의 본문을 검토해 주세요.");
  assert.equal(result.code, "rich_content_review_required");
});

test("uses Pydantic messages and validation type without exposing input", () => {
  const result = normalizeApiErrorPayload({
    detail: [
      {
        type: "string_too_long",
        msg: "String should have at most 100 characters",
        input: "private-original-content",
      },
    ],
  });

  assert.equal(result.message, "String should have at most 100 characters");
  assert.equal(result.code, "string_too_long");
  assert.equal(result.message.includes("private-original-content"), false);
});

test("preserves Env code and safe log", () => {
  const result = normalizeApiErrorPayload({
    error: "환경 검증에 실패했습니다.",
    code: "verification_failed",
    log: "safe build log",
  });

  assert.equal(result.message, "환경 검증에 실패했습니다.");
  assert.equal(result.code, "verification_failed");
  assert.equal(result.log, "safe build log");
});

test("never serializes an unknown object into the user message", () => {
  const result = normalizeApiErrorPayload({
    detail: { input: "secret", context: { value: "private" } },
  });

  assert.equal(result.message, "Request failed");
  assert.equal(result.message.includes("secret"), false);
  assert.equal(result.message.includes("private"), false);
});
