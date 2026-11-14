export const DEFAULT_SITE_NAME = "CUHA Memory";

export function normalizeSiteName(value) {
  if (typeof value !== "string") return DEFAULT_SITE_NAME;
  const normalized = value.trim();
  return normalized && normalized.length <= 100 ? normalized : DEFAULT_SITE_NAME;
}

export function titleWithSiteName(currentTitle, siteName, previousSiteName = DEFAULT_SITE_NAME) {
  const brand = normalizeSiteName(siteName);
  const current = typeof currentTitle === "string" ? currentTitle.trim() : "";
  if (!current) return brand;

  const separators = [" | ", " · ", " — ", " - "];
  const knownBrands = [...new Set([previousSiteName, DEFAULT_SITE_NAME, brand])]
    .filter((value) => typeof value === "string" && value)
    .sort((left, right) => right.length - left.length);
  for (const knownBrand of knownBrands) {
    if (current === knownBrand) return brand;
    const separator = separators.find((item) => current.endsWith(`${item}${knownBrand}`));
    if (separator) {
      if (knownBrand === brand) return current;
      return `${current.slice(0, -(separator.length + knownBrand.length))}${separator}${brand}`;
    }
  }
  return `${current} | ${brand}`;
}

export function createLatestRequestTracker() {
  let active = null;
  let nextId = 0;

  return {
    begin() {
      active?.controller.abort();
      active = { id: ++nextId, controller: new AbortController() };
      return active;
    },
    isCurrent(request) {
      return active?.id === request.id && !request.controller.signal.aborted;
    },
    clear(request) {
      if (active?.id === request.id) active = null;
    },
    dispose() {
      active?.controller.abort();
      active = null;
    },
  };
}
