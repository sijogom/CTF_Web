export const MAP_CONTENT_TYPES = ["challenge", "course", "lesson", "knowledge_tag"];
export const MAP_RELATION_TYPES = ["requires", "prepares_for", "helps", "recommends", "related", "similar", "tag_affinity", "contains", "tag_parent"];
export const DEFAULT_MAP_RELATIONS = MAP_RELATION_TYPES.filter((value) => value !== "tag_parent");

function readFilter(value, allowed, defaults) {
  if (value === null) return [...defaults];
  const selected = [...new Set(value.split(",").filter((item) => allowed.includes(item)))];
  return selected.length ? selected.sort() : [...defaults];
}

export function readMapToolsState(search) {
  const params = new URLSearchParams(search);
  return {
    query: (params.get("map_q") ?? "").slice(0, 100),
    types: readFilter(params.get("map_types"), MAP_CONTENT_TYPES, MAP_CONTENT_TYPES),
    relations: readFilter(params.get("map_relations"), MAP_RELATION_TYPES, DEFAULT_MAP_RELATIONS),
  };
}

export function mergeMapPage(current, page) {
  const sameFilter = (a, b) => [...a].sort().join(",") === [...b].sort().join(",");
  if (current.graph_revision !== page.graph_revision
    || current.focus.id !== page.focus.id
    || current.applied_filters.depth !== page.applied_filters.depth
    || !sameFilter(current.applied_filters.types, page.applied_filters.types)
    || !sameFilter(current.applied_filters.relation_types, page.applied_filters.relation_types)) {
    throw new Error("map_page_mismatch");
  }
  const nodes = new Map([...current.nodes, ...page.nodes].map((node) => [node.id, node]));
  const edges = new Map([...current.edges, ...page.edges].map((edge) => [edge.id, edge]));
  for (const edge of edges.values()) {
    if (!nodes.has(edge.source) || !nodes.has(edge.target)) throw new Error("map_page_missing_endpoint");
  }
  return {
    ...page,
    nodes: [...nodes.values()],
    edges: [...edges.values()],
    layout_hints: page.layout_hints ? {
      ...page.layout_hints,
      distance_by_node: { ...current.layout_hints?.distance_by_node, ...page.layout_hints.distance_by_node },
    } : current.layout_hints,
  };
}

export function internalContentHref(value) {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//")
    && !/[\\\u0000-\u001f]/.test(value) ? value : null;
}
