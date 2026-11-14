const DESKTOP_CARD = { width: 224, height: 116 };
const COMPACT_CARD = { width: 148, height: 116 };

function ringSlots(radius) {
  const slots = [];
  for (let x = 0; x <= radius; x += 1) slots.push({ x, y: -radius });
  for (let y = -radius + 1; y <= radius; y += 1) slots.push({ x: radius, y });
  for (let x = radius - 1; x >= -radius; x -= 1) slots.push({ x, y: radius });
  for (let y = radius - 1; y >= -radius; y -= 1) slots.push({ x: -radius, y });
  for (let x = -radius + 1; x < 0; x += 1) slots.push({ x, y: -radius });
  return slots;
}

function compareNodes(left, right) {
  return left.type.localeCompare(right.type) || left.id.localeCompare(right.id);
}

export function createKnowledgeMapPositions(graph, { compact = false } = {}) {
  const center = { x: 620, y: 410 };
  const positions = new Map([[graph.focus.id, center]]);
  const nodes = graph.nodes.filter((node) => node.id !== graph.focus.id);
  const distance = (node) => Math.max(1, graph.layout_hints?.distance_by_node?.[node.id] ?? node.distance ?? 1);
  nodes.sort((left, right) => distance(left) - distance(right) || compareNodes(left, right));

  // A small phone graph fits in two readable columns, not a desktop-sized
  // circle shrunk until every title becomes a few pixels tall.
  if (compact && nodes.length <= 7) {
    const rows = Math.ceil((nodes.length + 1) / 2);
    const focusRow = Math.floor((rows - 1) / 2);
    const slots = [];
    for (let row = 0; row < rows; row += 1) {
      for (let column = 0; column < 2; column += 1) {
        if (row === focusRow && column === 0) continue;
        slots.push({ x: center.x + column * 180, y: center.y + (row - focusRow) * 152 });
      }
    }
    slots.sort((left, right) => Math.abs(left.y - center.y) - Math.abs(right.y - center.y) || left.y - right.y || left.x - right.x);
    nodes.forEach((node, index) => positions.set(node.id, slots[index]));
    return positions;
  }

  let radius = 1;
  let offset = 0;
  while (offset < nodes.length) {
    const depth = distance(nodes[offset]);
    const group = nodes.slice(offset).filter((node) => distance(node) === depth);
    let groupOffset = 0;
    while (groupOffset < group.length) {
      const slots = ringSlots(radius);
      const count = Math.min(slots.length, group.length - groupOffset);
      for (let index = 0; index < count; index += 1) {
        const slot = slots[Math.floor(index * slots.length / count)];
        positions.set(group[groupOffset + index].id, {
          x: center.x + slot.x * 296,
          y: center.y + slot.y * 168,
        });
      }
      groupOffset += count;
      radius += 1;
    }
    offset += group.length;
  }
  return positions;
}

export function fitKnowledgeMapViewport(nodes, bounds, { compact = false, minZoom = 0.05, maxZoom = 1 } = {}) {
  if (!nodes.length || !bounds || bounds.right <= bounds.left || bounds.bottom <= bounds.top) return null;
  const card = compact ? COMPACT_CARD : DESKTOP_CARD;
  let left = Infinity;
  let right = -Infinity;
  let top = Infinity;
  let bottom = -Infinity;
  for (const node of nodes) {
    const width = node.measured?.width ?? node.width ?? card.width;
    const height = node.measured?.height ?? node.height ?? card.height;
    if (![node.position?.x, node.position?.y, width, height].every(Number.isFinite)) return null;
    left = Math.min(left, node.position.x - width / 2);
    right = Math.max(right, node.position.x + width / 2);
    top = Math.min(top, node.position.y - height / 2);
    bottom = Math.max(bottom, node.position.y + height / 2);
  }
  const zoom = Math.max(minZoom, Math.min(maxZoom, (bounds.right - bounds.left) / Math.max(1, right - left), (bounds.bottom - bounds.top) / Math.max(1, bottom - top)));
  return {
    x: (bounds.left + bounds.right) / 2 - ((left + right) / 2) * zoom,
    y: (bounds.top + bounds.bottom) / 2 - ((top + bottom) / 2) * zoom,
    zoom,
  };
}
