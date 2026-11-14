/**
 * Textarea selection transforms used by MarkdownEditor.
 *
 * Keeping these transforms independent from React/DOM lets the regression
 * cases for toolbar clicks be tested without pretending that a browser E2E
 * run has happened.
 */

/**
 * @typedef {{ start: number, end: number }} EditorTextSelection
 */

/**
 * @param {string} value
 * @param {EditorTextSelection} selection
 * @returns {EditorTextSelection}
 */
export function normalizeTextSelection(value, selection) {
  const length = value.length;
  const start = Math.min(Math.max(selection.start, 0), length);
  const end = Math.min(Math.max(selection.end, 0), length);
  return start <= end ? { start, end } : { start: end, end: start };
}

/**
 * Inserts text over the active selection and places the cursor immediately
 * after the inserted text.
 *
 * @param {{ value: string, selection: EditorTextSelection, text: string }} input
 * @returns {{ value: string, selection: EditorTextSelection }}
 */
export function insertTextAtSelection({ value, selection, text }) {
  const normalized = normalizeTextSelection(value, selection);
  const nextValue = value.slice(0, normalized.start) + text + value.slice(normalized.end);
  const cursor = normalized.start + text.length;
  return {
    value: nextValue,
    selection: { start: cursor, end: cursor },
  };
}

/**
 * Wraps the active selection without relying on a React render-time value.
 *
 * For a selection spanning paragraph breaks, each non-empty paragraph is
 * wrapped independently. Markdown emphasis cannot safely span blank lines,
 * whereas this form keeps every selected paragraph and makes the intent
 * visible in the rendered result.
 *
 * @param {string} selected
 * @param {string} before
 * @param {string} after
 * @returns {string}
 */
function wrapSelectedParagraphs(selected, before, after) {
  return selected
    .split(/(\n\s*\n)/)
    .map((segment) => (/(?:\n\s*\n)/.test(segment) || segment.length === 0
      ? segment
      : before + segment + after))
    .join("");
}

/**
 * Wraps the active selection in Markdown tokens. A collapsed selection gets
 * an editable placeholder, which remains selected after the operation.
 *
 * @param {{ value: string, selection: EditorTextSelection, before: string, after: string, placeholder: string }} input
 * @returns {{ value: string, selection: EditorTextSelection }}
 */
export function wrapTextSelection({ value, selection, before, after, placeholder }) {
  const normalized = normalizeTextSelection(value, selection);
  const selected = value.slice(normalized.start, normalized.end);
  const wrapped = selected.length > 0
    ? wrapSelectedParagraphs(selected, before, after)
    : before + placeholder + after;
  const nextValue = value.slice(0, normalized.start) + wrapped + value.slice(normalized.end);

  if (selected.length === 0) {
    return {
      value: nextValue,
      selection: {
        start: normalized.start + before.length,
        end: normalized.start + before.length + placeholder.length,
      },
    };
  }

  return {
    value: nextValue,
    selection: {
      start: normalized.start,
      end: normalized.start + wrapped.length,
    },
  };
}
