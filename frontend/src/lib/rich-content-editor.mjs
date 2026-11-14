import { Table, TableRow, TableHeader, TableCell } from "@tiptap/extension-table";
import { ListItem } from "@tiptap/extension-list";
import { mergeAttributes } from "@tiptap/core";
import DOMPurify from "isomorphic-dompurify";
import { classifyRichContent } from "./rich-content.mjs";

const TABLE_ALIGNMENTS = new Set(["left", "center", "right"]);
const EDITOR_UNSUPPORTED_TAGS = new Set(["div", "span", "thead", "tfoot"]);
const LINK_CLASSES = new Set(["text-accent-primary", "underline"]);
const IMAGE_CLASSES = new Set([
  "my-4", "h-auto", "w-full", "max-w-4xl", "cursor-zoom-in", "rounded-xl",
  "border", "border-border", "object-contain", "shadow-sm",
]);

function tableAlignmentAttributes(parentAttributes) {
  return {
    ...parentAttributes,
    align: {
      default: null,
      parseHTML: (element) => {
        const align = element.getAttribute("align")?.toLowerCase();
        return align && TABLE_ALIGNMENTS.has(align) ? align : null;
      },
      renderHTML: (attributes) => (
        attributes.align && TABLE_ALIGNMENTS.has(attributes.align)
          ? { align: attributes.align }
          : {}
      ),
    },
  };
}

function boundedListNumber(value) {
  if (value === null || !/^-?\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && Math.abs(parsed) <= 1_000_000 ? parsed : null;
}

const AlignedTableHeader = TableHeader.extend({
  addAttributes() {
    return {
      ...tableAlignmentAttributes(this.parent?.()),
      scope: {
        default: null,
        parseHTML: (element) => {
          const scope = element.getAttribute("scope")?.toLowerCase();
          return scope === "row" || scope === "col" || scope === "rowgroup" || scope === "colgroup" ? scope : null;
        },
        renderHTML: (attributes) => (
          attributes.scope === "row" || attributes.scope === "col" || attributes.scope === "rowgroup" || attributes.scope === "colgroup"
            ? { scope: attributes.scope }
            : {}
        ),
      },
    };
  },
});

const AlignedTableCell = TableCell.extend({
  addAttributes() {
    return tableAlignmentAttributes(this.parent?.());
  },
});

// TipTap's stock serializer emits layout-only style/colgroup markup even when
// resizing is disabled. The editor NodeView may use that layout internally,
// but persisted HTML must remain inside the shared rich-content contract.
const CanonicalTable = Table.extend({
  renderHTML({ HTMLAttributes }) {
    return [
      "table",
      mergeAttributes(this.options.HTMLAttributes, HTMLAttributes),
      ["tbody", 0],
    ];
  },
});

const ValuedListItem = ListItem.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      value: {
        default: null,
        parseHTML: (element) => boundedListNumber(element.getAttribute("value")),
        renderHTML: (attributes) => {
          const value = boundedListNumber(attributes.value === null || attributes.value === undefined ? null : String(attributes.value));
          return value === null ? {} : { value };
        },
      },
    };
  },
});

export const RICH_CONTENT_PARSE_OPTIONS = Object.freeze({ preserveWhitespace: "full" });

function hasEditorSafeClass(element) {
  const tokens = element.getAttribute("class")?.split(/\s+/).filter(Boolean) ?? [];
  if (element.tagName === "CODE") return tokens.every((token) => /^language-[A-Za-z\d_+#.-]+$/.test(token));
  if (element.tagName === "A") return tokens.every((token) => LINK_CLASSES.has(token));
  if (element.tagName === "IMG") return tokens.every((token) => IMAGE_CLASSES.has(token));
  return tokens.length === 0;
}

/**
 * TipTap repairs ragged/overlapping grids and out-of-bounds row spans by
 * inserting cells or changing spans. Keep that legacy source in review mode
 * instead of presenting a repaired table as an unchanged draft.
 */
function hasUnstableTableGrid(table) {
  const rowGroups = Array.from(table.children).filter((child) => (
    child.tagName === "THEAD" || child.tagName === "TBODY" || child.tagName === "TFOOT"
  ));
  if (rowGroups.length > 1) return true;
  const groups = rowGroups.length > 0 ? rowGroups : [table];
  let expectedWidth = null;
  for (const group of groups) {
    const rows = Array.from(group.children).filter((child) => child.tagName === "TR");
    let activeRowspans = [];
    for (let rowIndex = 0; rowIndex < rows.length; rowIndex += 1) {
      const remainingRows = rows.length - rowIndex;
      const occupied = activeRowspans.map((remaining) => remaining > 0);
      let cursor = 0;
      for (const cell of rows[rowIndex].children) {
        if (cell.tagName !== "TH" && cell.tagName !== "TD") continue;
        const colspan = Number(cell.getAttribute("colspan") ?? "1");
        const rowspan = Number(cell.getAttribute("rowspan") ?? "1");
        if (rowspan > remainingRows) return true;

        while (occupied[cursor]) cursor += 1;
        if (Array.from({ length: colspan }, (_, offset) => occupied[cursor + offset]).some(Boolean)) return true;
        for (let offset = 0; offset < colspan; offset += 1) {
          const column = cursor + offset;
          occupied[column] = true;
          activeRowspans[column] = Math.max(activeRowspans[column] ?? 0, rowspan);
        }
        cursor += colspan;
      }
      const width = occupied.reduce((last, value, index) => (value ? index + 1 : last), 0);
      if (occupied.slice(0, width).some((value) => !value)) return true;
      if (expectedWidth === null) expectedWidth = width;
      else if (width !== expectedWidth) return true;
      activeRowspans = activeRowspans.map((remaining) => Math.max(remaining - 1, 0));
    }
    if (activeRowspans.some((remaining) => remaining > 0)) return true;
  }
  return false;
}

const EDITOR_ATTRIBUTES_BY_TAG = Object.freeze({
  A: new Set(["href", "target", "rel", "class"]),
  IMG: new Set(["src", "alt", "title", "class", "loading"]),
  CODE: new Set(["class"]),
  OL: new Set(["start"]),
  LI: new Set(["value"]),
  TH: new Set(["colspan", "rowspan", "align", "scope"]),
  TD: new Set(["colspan", "rowspan", "align"]),
});

/** True when TipTap cannot round-trip the original structure/attributes safely. */
export function requiresRichContentEditorReview(value) {
  const format = classifyRichContent(value);
  if (format === "ambiguous") return true;
  if (format !== "html") return false;

  const root = DOMPurify.sanitize("", { RETURN_DOM: true });
  const template = root.ownerDocument.createElement("template");
  template.innerHTML = value;
  for (const table of template.content.querySelectorAll("table")) {
    if (hasUnstableTableGrid(table)) return true;
  }
  for (const element of template.content.querySelectorAll("*")) {
    if (EDITOR_UNSUPPORTED_TAGS.has(element.tagName.toLowerCase())) return true;
    const supportedAttributes = EDITOR_ATTRIBUTES_BY_TAG[element.tagName] ?? new Set();
    for (const attribute of element.attributes) {
      if (!supportedAttributes.has(attribute.name.toLowerCase())) return true;
      if (attribute.name.toLowerCase() === "class" && !hasEditorSafeClass(element)) return true;
      if (element.tagName === "IMG" && attribute.name.toLowerCase() === "loading" && attribute.value !== "lazy") return true;
    }
  }
  return false;
}

export function createRichContentStructuralExtensions() {
  return [
    CanonicalTable.configure({ resizable: false }),
    TableRow,
    AlignedTableHeader,
    AlignedTableCell,
    ValuedListItem,
  ];
}
