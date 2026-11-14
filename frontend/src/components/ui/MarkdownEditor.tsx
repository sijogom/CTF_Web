"use client";

import { RichTextEditor } from "@/components/ui/RichTextEditor";

interface MarkdownEditorProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  enableImageUpload?: boolean;
  imageUploadUrl?: string;
}

/**
 * Compatibility entry point for older imports. Rich content now crosses write
 * boundaries as canonical HTML, so this component delegates to the shared HTML
 * editor instead of producing a second Markdown-only representation.
 */
export function MarkdownEditor(props: MarkdownEditorProps) {
  return <RichTextEditor {...props} />;
}
