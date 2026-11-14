export interface EditorTextSelection {
  start: number;
  end: number;
}

export function normalizeTextSelection(
  value: string,
  selection: EditorTextSelection,
): EditorTextSelection;

export function insertTextAtSelection(input: {
  value: string;
  selection: EditorTextSelection;
  text: string;
}): {
  value: string;
  selection: EditorTextSelection;
};

export function wrapTextSelection(input: {
  value: string;
  selection: EditorTextSelection;
  before: string;
  after: string;
  placeholder: string;
}): {
  value: string;
  selection: EditorTextSelection;
};
