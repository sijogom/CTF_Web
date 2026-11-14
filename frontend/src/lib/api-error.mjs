const FALLBACK_MESSAGE = "Request failed";

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function nonEmptyString(value) {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function messageFromDetail(detail) {
  const direct = nonEmptyString(detail);
  if (direct) return direct;

  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === "string") return nonEmptyString(item);
        if (!isRecord(item)) return undefined;
        return nonEmptyString(item.msg) ?? nonEmptyString(item.message);
      })
      .filter(Boolean);
    return messages.length > 0 ? messages.join(", ") : undefined;
  }

  if (!isRecord(detail)) return undefined;
  return (
    nonEmptyString(detail.message)
    ?? nonEmptyString(detail.msg)
    ?? messageFromDetail(detail.detail)
  );
}

function validationCode(detail) {
  if (!Array.isArray(detail)) return undefined;
  for (const item of detail) {
    if (isRecord(item)) {
      const type = nonEmptyString(item.type);
      if (type) return type;
    }
  }
  return undefined;
}

/**
 * FastAPI/Pydantic 및 Env 응답을 공통 ApiError 계약으로 정규화한다.
 * 객체 전체나 Pydantic input 값은 사용자 메시지에 직렬화하지 않는다.
 */
export function normalizeApiErrorPayload(payload) {
  const body = isRecord(payload) ? payload : {};
  const detail = body.detail ?? body.error ?? body.message ?? payload;
  const detailRecord = isRecord(detail) ? detail : {};
  const message = messageFromDetail(detail) ?? FALLBACK_MESSAGE;
  const code = (
    nonEmptyString(body.code)
    ?? nonEmptyString(detailRecord.code)
    ?? nonEmptyString(detailRecord.type)
    ?? validationCode(detail)
  );

  return {
    detail,
    message,
    code,
    log: nonEmptyString(body.log),
  };
}

