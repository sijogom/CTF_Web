import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  insertTextAtSelection,
  normalizeTextSelection,
  wrapTextSelection,
} from "../src/lib/markdown-editor-selection.mjs";
import {
  buildImageAltText,
  buildMarkdownImage,
} from "../src/lib/editor-image-alt.mjs";

test("bold wrapping retains an inline selection instead of replacing the document", () => {
  const result = wrapTextSelection({
    value: "before selected after",
    selection: { start: 7, end: 15 },
    before: "**",
    after: "**",
    placeholder: "bold",
  });

  assert.deepEqual(result, {
    value: "before **selected** after",
    selection: { start: 7, end: 19 },
  });
});

test("italic wrapping preserves every paragraph in a multiline selection", () => {
  const result = wrapTextSelection({
    value: "start\n\nfirst\n\nsecond\n\nend",
    selection: { start: 7, end: 20 },
    before: "*",
    after: "*",
    placeholder: "italic",
  });

  assert.equal(result.value, "start\n\n*first*\n\n*second*\n\nend");
  assert.deepEqual(result.selection, { start: 7, end: 24 });
});

test("collapsed selection inserts an editable placeholder and normalizes stale positions", () => {
  const result = wrapTextSelection({
    value: "본문",
    selection: { start: 0, end: 0 },
    before: "**",
    after: "**",
    placeholder: "굵게",
  });

  assert.equal(result.value, "**굵게**본문");
  assert.deepEqual(result.selection, { start: 2, end: 4 });
  assert.deepEqual(normalizeTextSelection("본문", { start: 999, end: -10 }), { start: 0, end: 2 });
});

test("image insertion replaces only the active range and retains surrounding text", () => {
  const result = insertTextAtSelection({
    value: "before remove after",
    selection: { start: 7, end: 13 },
    text: "![description](/api/learn/lesson-images/example.png)\n",
  });

  assert.equal(result.value, "before ![description](/api/learn/lesson-images/example.png)\n after");
  assert.equal(result.selection.start, result.selection.end);
});

test("image alternative text prefers user input and escapes Markdown delimiters", () => {
  assert.equal(buildImageAltText("  교실   실습  ", "ignored-file.png", "이미지"), "교실 실습");
  assert.equal(buildImageAltText("", "network_diagram-v2.png", "이미지"), "network diagram v2");
  assert.equal(
    buildMarkdownImage("/api/learn/lesson-images/example.png", "선택 [설명](초안)"),
    "![선택 \\[설명\\]\\(초안\\)](/api/learn/lesson-images/example.png)",
  );
});

test("MarkdownEditor compatibility entry point delegates canonical HTML editing", () => {
  const source = readFileSync(
    new URL("../src/components/ui/MarkdownEditor.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /import \{ RichTextEditor \} from "@\/components\/ui\/RichTextEditor";/);
  assert.match(source, /return <RichTextEditor \{\.\.\.props\} \/>;/);
});

test("course rich text editor retains TipTap undo, redo, paste, and image preview paths", () => {
  const source = readFileSync(
    new URL("../src/components/ui/RichTextEditor.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /toggleBold\(\)/);
  assert.match(source, /toggleItalic\(\)/);
  assert.match(source, /\.undo\(\)/);
  assert.match(source, /\.redo\(\)/);
  assert.match(source, /handlePaste:/);
  assert.match(source, /setPreviewImage/);
});
