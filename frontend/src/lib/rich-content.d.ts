export type RichContentFormat = "html" | "markdown" | "plain" | "ambiguous";

export const RICH_CONTENT_TAGS: readonly string[];
export const RICH_CONTENT_ATTRIBUTES: readonly string[];
export function classifyRichContent(value: string): RichContentFormat;
export function legacyRichContentToHtml(value: string): string;
export function sanitizeRichHtml(value: string): string;
export function richContentImageSrcForDisplay(src: string, basePath?: string): string;
export function richContentHtmlForDisplay(value: string, basePath?: string): string;
export function richContentToPlainText(value: string, maxChars?: number): string;
export function richContentVisibleLength(value: string): number;
export function isRichContentEmpty(value: string): boolean;
