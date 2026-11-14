/** Validate a server page so malformed data is an explicit failure rather than a false empty list. */
export function parseListPage(value, expectedPage, expectedSize) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Invalid list response: envelope is missing");
  }
  if (!Array.isArray(value.items)) {
    throw new Error("Invalid list response: items array is missing");
  }
  if (!Number.isSafeInteger(value.total) || value.total < 0) {
    throw new Error("Invalid list response: total is missing");
  }
  if (!Number.isSafeInteger(value.page) || value.page < 1) {
    throw new Error("Invalid list response: page is missing");
  }
  if (!Number.isSafeInteger(value.size) || value.size < 1) {
    throw new Error("Invalid list response: size is missing");
  }
  if (expectedPage !== undefined && value.page !== expectedPage) {
    throw new Error("Invalid list response: page does not match request");
  }
  if (expectedSize !== undefined && value.size !== expectedSize) {
    throw new Error("Invalid list response: size does not match request");
  }
  return value;
}

/** Validate an offset/limit server page used by administrative ledgers. */
export function parseOffsetListPage(value, expectedOffset, expectedLimit) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Invalid list response: envelope is missing");
  }
  if (!Array.isArray(value.items)) {
    throw new Error("Invalid list response: items array is missing");
  }
  if (!Number.isSafeInteger(value.total) || value.total < 0) {
    throw new Error("Invalid list response: total is missing");
  }
  if (!Number.isSafeInteger(value.offset) || value.offset < 0) {
    throw new Error("Invalid list response: offset is missing");
  }
  if (!Number.isSafeInteger(value.limit) || value.limit < 1) {
    throw new Error("Invalid list response: limit is missing");
  }
  if (expectedOffset !== undefined && value.offset !== expectedOffset) {
    throw new Error("Invalid list response: offset does not match request");
  }
  if (expectedLimit !== undefined && value.limit !== expectedLimit) {
    throw new Error("Invalid list response: limit does not match request");
  }
  return value;
}
