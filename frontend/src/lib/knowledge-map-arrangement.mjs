export const KNOWLEDGE_MAP_POSITION_STORAGE_KEY = "ctf-web:knowledge-map:positions:v7";
export const KNOWLEDGE_MAP_POSITION_LAYOUT_VERSION = "balanced-rings-v2";
export const KNOWLEDGE_MAP_VIEWPORT_STORAGE_KEY = "ctf-web:knowledge-map:viewports:v1";
export const KNOWLEDGE_MAP_VIEWPORT_LAYOUT_VERSION = "readable-focus-v1";
export const KNOWLEDGE_MAP_MAX_STORED_POSITIONS = 360;
export const KNOWLEDGE_MAP_MAX_STORED_VIEWPORTS = 60;

export function knowledgeMapPositionKey(graph, nodeId) {
  return `${graph.graph_revision}:${graph.focus.id}:${nodeId}`;
}

export function knowledgeMapViewportKey(graph) {
  return `${graph.graph_revision}:${graph.focus.id}`;
}

function isPoint(value) {
  return value && Number.isFinite(value.x) && Number.isFinite(value.y);
}

function isViewport(value) {
  return isPoint(value) && Number.isFinite(value.zoom) && value.zoom >= 0.05 && value.zoom <= 2.2;
}

function readEnvelope(raw, version) {
  if (raw === null) return { version, entries: {} };
  const value = JSON.parse(raw);
  if (!value || typeof value !== "object" || Array.isArray(value)
    || value.version !== version || !value.entries || typeof value.entries !== "object" || Array.isArray(value.entries)) {
    throw new Error("Unsupported map storage");
  }
  return value;
}

// Keep the original bytes until recovery is confirmed. A later write from
// another map/tab is a conflict, not permission to overwrite that newer data.
export function recoverKnowledgeMapLayout(storage, recovery) {
  const pending = [];
  for (const entry of recovery) {
    try {
      const current = storage.getItem(entry.key);
      if (current === entry.previousRaw) continue;
      if (current !== entry.writtenRaw) {
        pending.push(entry);
        continue;
      }
      if (entry.previousRaw === null) storage.removeItem(entry.key);
      else storage.setItem(entry.key, entry.previousRaw);
    } catch {
      pending.push(entry);
    }
  }
  return { ok: pending.length === 0, recovery: pending };
}

export function persistKnowledgeMapLayout(storage, graph, positionChanges, viewport) {
  const written = [];
  try {
    if (!isViewport(viewport)) throw new Error("Invalid map viewport");
    const positionRaw = storage.getItem(KNOWLEDGE_MAP_POSITION_STORAGE_KEY);
    const viewportRaw = storage.getItem(KNOWLEDGE_MAP_VIEWPORT_STORAGE_KEY);
    const positionEnvelope = readEnvelope(positionRaw, KNOWLEDGE_MAP_POSITION_LAYOUT_VERSION);
    const viewportEnvelope = readEnvelope(viewportRaw, KNOWLEDGE_MAP_VIEWPORT_LAYOUT_VERSION);
    const positions = new Map(Object.entries(positionEnvelope.entries));
    const viewports = new Map(Object.entries(viewportEnvelope.entries));
    const positionLimit = Math.max(KNOWLEDGE_MAP_MAX_STORED_POSITIONS, positions.size);
    const viewportLimit = Math.max(KNOWLEDGE_MAP_MAX_STORED_VIEWPORTS, viewports.size);
    const nodeIds = new Set(graph.nodes.map((node) => node.id));
    for (const [nodeId, position] of positionChanges) {
      if (!nodeIds.has(nodeId) || (position !== null && !isPoint(position))) {
        throw new Error("Invalid map position change");
      }
      const key = knowledgeMapPositionKey(graph, nodeId);
      if (position === null) positions.delete(key);
      else positions.set(key, { x: position.x, y: position.y });
    }
    viewports.set(knowledgeMapViewportKey(graph), { ...viewport });
    if (positions.size > positionLimit || viewports.size > viewportLimit) {
      throw new Error("Map storage capacity reached");
    }
    const updates = [
      {
        key: KNOWLEDGE_MAP_POSITION_STORAGE_KEY,
        previousRaw: positionRaw,
        writtenRaw: JSON.stringify({ ...positionEnvelope, entries: Object.fromEntries(positions) }),
      },
      {
        key: KNOWLEDGE_MAP_VIEWPORT_STORAGE_KEY,
        previousRaw: viewportRaw,
        writtenRaw: JSON.stringify({ ...viewportEnvelope, entries: Object.fromEntries(viewports) }),
      },
    ];
    for (const update of updates) {
      storage.setItem(update.key, update.writtenRaw);
      written.push(update);
    }
    return {
      ok: true,
      positions: new Map([...positions].filter(([, value]) => isPoint(value))),
      viewports: new Map([...viewports].filter(([, value]) => isViewport(value))),
    };
  } catch {
    const result = recoverKnowledgeMapLayout(storage, written);
    return { ok: false, recovery: result.recovery };
  }
}
