"use client";

import { useMemo } from "react";
import {
  classifyRichContent,
  richContentHtmlForDisplay,
  legacyRichContentToHtml,
} from "@/lib/rich-content.mjs";

const APP_BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH || "/ctf";

interface MarkdownRendererProps {
  content: string;
}

/** Compatibility hint. The common module remains the format authority. */
export function isRichTextHtml(text: string): boolean {
  return classifyRichContent(text) === "html";
}

/** @deprecated Import `legacyRichContentToHtml` from the common module. */
export function legacyMarkdownToHtml(content: string): string {
  return legacyRichContentToHtml(content);
}

export function MarkdownRenderer({ content }: MarkdownRendererProps) {
  const html = useMemo(() => richContentHtmlForDisplay(content, APP_BASE_PATH), [content]);

  return (
    <div
      className="rich-content prose max-w-none prose-headings:text-text-primary prose-p:text-text-secondary prose-code:rounded prose-code:bg-bg-primary prose-code:px-1.5 prose-code:py-0.5 prose-code:text-accent-primary prose-pre:bg-bg-primary prose-pre:border prose-pre:border-border prose-a:text-accent-primary prose-strong:text-text-primary prose-li:text-text-secondary"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
