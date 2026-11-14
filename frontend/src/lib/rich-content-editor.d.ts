import type { AnyExtension } from "@tiptap/core";
import type { ParseOptions } from "@tiptap/pm/model";

export const RICH_CONTENT_PARSE_OPTIONS: Readonly<ParseOptions>;
export function requiresRichContentEditorReview(value: string): boolean;
export function createRichContentStructuralExtensions(): AnyExtension[];
