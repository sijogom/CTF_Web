const MAP_FOCUS_TYPES = new Set([
  "challenge",
  "course",
  "lesson",
  "knowledge_tag",
]);

export function parseMapFocus(search) {
  const params = new URLSearchParams(search);
  const focusType = params.get("focus_type");
  const focusId = params.get("focus_id")?.trim();
  if (!focusType || !focusId || focusId.length > 64 || !MAP_FOCUS_TYPES.has(focusType)) {
    return null;
  }
  return { type: focusType, id: focusId };
}

export function getLearnHydrationSearch(routeSearch, pathname, locationSearch) {
  if (routeSearch !== "" || !/(?:^|\/)learn\/?$/.test(pathname)) return null;
  return locationSearch;
}
