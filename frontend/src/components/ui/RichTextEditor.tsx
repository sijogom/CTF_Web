"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useEditor, EditorContent, type Editor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Image from "@tiptap/extension-image";
import LinkExtension from "@tiptap/extension-link";
import Underline from "@tiptap/extension-underline";
import { Placeholder } from "@tiptap/extensions";
import CodeBlockLowlight from "@tiptap/extension-code-block-lowlight";
import { common, createLowlight } from "lowlight";
import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";
import { apiFormFetch } from "@/lib/api-client";
import { buildImageAltText } from "@/lib/editor-image-alt.mjs";
import { classifyRichContent, legacyRichContentToHtml, richContentImageSrcForDisplay } from "@/lib/rich-content.mjs";
import { createRichContentStructuralExtensions, requiresRichContentEditorReview, RICH_CONTENT_PARSE_OPTIONS } from "@/lib/rich-content-editor.mjs";
import { useToast } from "@/providers/ToastProvider";

const lowlight = createLowlight(common);
const APP_BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH || "/ctf";
const MAX_IMAGE_SIZE = 10 * 1024 * 1024;
const ALLOWED_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);

const DisplayImage = Image.extend({
  addNodeView() {
    const configuredAttributes = this.options.HTMLAttributes;
    const imageNodeType = this.type;
    return ({ node }) => {
      const dom = document.createElement("img");
      let appliedAttributes = new Map<string, string>();

      const applyAttributes = (attributes: Record<string, unknown>) => {
        const nextAttributes = new Map<string, string>();
        for (const [name, value] of Object.entries({
            ...configuredAttributes,
            ...attributes,
            src: richContentImageSrcForDisplay(String(attributes.src ?? ""), APP_BASE_PATH),
          })) {
          if (value === null || value === undefined) continue;
          nextAttributes.set(name, String(value));
        }

        for (const [name, previousValue] of appliedAttributes) {
          if (nextAttributes.has(name)) continue;
          if (name === "class") {
            for (const token of previousValue.split(/\s+/).filter(Boolean)) dom.classList.remove(token);
          } else {
            dom.removeAttribute(name);
          }
        }
        for (const [name, nextValue] of nextAttributes) {
          const previousValue = appliedAttributes.get(name);
          if (previousValue === nextValue) continue;
          if (name === "class") {
            for (const token of (previousValue ?? "").split(/\s+/).filter(Boolean)) dom.classList.remove(token);
            for (const token of nextValue.split(/\s+/).filter(Boolean)) dom.classList.add(token);
          } else {
            dom.setAttribute(name, nextValue);
          }
        }
        appliedAttributes = nextAttributes;
      };

      applyAttributes(node.attrs);
      return {
        dom,
        update(updatedNode) {
          if (updatedNode.type !== imageNodeType) return false;
          applyAttributes(updatedNode.attrs);
          return true;
        },
      };
    };
  },
});

function toEditorContent(value: string): string {
  if (!value.trim()) return "<p></p>";
  if (requiresRichContentEditorReview(value) && classifyRichContent(value) !== "ambiguous") {
    const escaped = value
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    return `<pre><code>${escaped}</code></pre>`;
  }
  return legacyRichContentToHtml(value);
}

interface RichTextEditorProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  ariaLabel?: string;
  enableImageUpload?: boolean;
  imageUploadUrl?: string;
}

interface PreviewImage {
  src: string;
  alt: string;
}

export function RichTextEditor({
  value,
  onChange,
  placeholder,
  ariaLabel,
  enableImageUpload = false,
  imageUploadUrl = "/api/learn/lessons/upload-image",
}: RichTextEditorProps) {
  const t = useTranslations("common");
  const { toast } = useToast();
  const onChangeRef = useRef(onChange);
  const editorRef = useRef<Editor | null>(null);
  const imageUploadHandlerRef = useRef<(file: File) => void>(() => undefined);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const applyingExternalValueRef = useRef(false);
  const uploadAbortControllerRef = useRef<AbortController | null>(null);
  const uploadInFlightRef = useRef(false);
  const uploadInsertionRef = useRef<{ position: number; document: string } | null>(null);
  const [imageUploading, setImageUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [previewImage, setPreviewImage] = useState<PreviewImage | null>(null);
  const [imageAltText, setImageAltText] = useState("");

  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  useEffect(() => {
    const format = classifyRichContent(value);
    if (!value.trim() || (format !== "markdown" && format !== "plain")) return;
    const normalized = legacyRichContentToHtml(value);
    if (normalized !== value) onChangeRef.current(normalized);
  }, [value]);

  const editor = useEditor({
    immediatelyRender: false,
    shouldRerenderOnTransaction: true,
    extensions: [
      StarterKit.configure({
        codeBlock: false,
        listItem: false,
        link: false,
        underline: false,
        listKeymap: false,
        trailingNode: false,
      }),
      Underline,
      LinkExtension.configure({
        openOnClick: false,
        HTMLAttributes: { class: "text-accent-primary underline" },
      }),
      DisplayImage.configure({
        HTMLAttributes: {
          class: "my-4 h-auto w-full max-w-4xl cursor-zoom-in rounded-xl border border-border object-contain shadow-sm",
          loading: "lazy",
        },
      }),
      CodeBlockLowlight.configure({ lowlight }),
      ...createRichContentStructuralExtensions(),
      Placeholder.configure({ placeholder: placeholder || "" }),
    ],
    content: toEditorContent(value),
    parseOptions: RICH_CONTENT_PARSE_OPTIONS,
    onUpdate: ({ editor: currentEditor }) => {
      if (!applyingExternalValueRef.current) {
        onChangeRef.current(currentEditor.getHTML());
      }
    },
    editorProps: {
      attributes: {
        class: "prose prose-invert max-w-none min-h-[240px] p-4 text-sm text-text-primary focus:outline-none prose-img:my-4 prose-img:rounded-xl",
        ...(ariaLabel ? { "aria-label": ariaLabel } : {}),
      },
      handleDrop: (_view, event, _slice, moved) => {
        if (!enableImageUpload || moved) return false;
        const file = event.dataTransfer?.files?.[0];
        if (!file || !file.type.startsWith("image/")) return false;
        event.preventDefault();
        imageUploadHandlerRef.current(file);
        return true;
      },
      handlePaste: (_view, event) => {
        if (!enableImageUpload) return false;
        const imageItem = Array.from(event.clipboardData?.items || []).find((item) => item.type.startsWith("image/"));
        const file = imageItem?.getAsFile();
        if (!file) return false;
        event.preventDefault();
        imageUploadHandlerRef.current(file);
        return true;
      },
      handleClick: (_view, _position, event) => {
        const target = event.target;
        if (!(target instanceof HTMLImageElement)) return false;
        setPreviewImage({ src: target.currentSrc || target.src, alt: target.alt || "" });
        return true;
      },
    },
  });

  useEffect(() => {
    editorRef.current = editor;
    return () => {
      if (editorRef.current === editor) editorRef.current = null;
    };
  }, [editor]);

  useEffect(() => () => {
    uploadAbortControllerRef.current?.abort();
  }, []);

  useEffect(() => {
    if (!editor || editor.isDestroyed || applyingExternalValueRef.current) return;
    const nextContent = toEditorContent(value);
    if (nextContent === editor.getHTML()) return;

    const { from, to } = editor.state.selection;
    applyingExternalValueRef.current = true;
    editor.commands.setContent(nextContent, { emitUpdate: false, parseOptions: RICH_CONTENT_PARSE_OPTIONS });

    const documentSize = editor.state.doc.content.size;
    editor.commands.setTextSelection({
      from: Math.min(Math.max(from, 1), documentSize),
      to: Math.min(Math.max(to, 1), documentSize),
    });
    applyingExternalValueRef.current = false;
  }, [editor, value]);

  const handleImageUpload = useCallback(async (file: File) => {
    const activeEditor = editorRef.current;
    if (!activeEditor || activeEditor.isDestroyed || !enableImageUpload || uploadInFlightRef.current) return;

    if (!ALLOWED_IMAGE_TYPES.has(file.type)) {
      toast("error", t("uploadFailed"), t("imageOnlyError"));
      return;
    }
    if (file.size > MAX_IMAGE_SIZE) {
      toast("error", t("uploadFailed"), t("imageSizeError"));
      return;
    }

    const uploadInsertion = uploadInsertionRef.current ?? {
      position: activeEditor.state.selection.to,
      document: activeEditor.getHTML(),
    };
    uploadInsertionRef.current = null;
    const alt = buildImageAltText(imageAltText, file.name, t("image"));
    const controller = new AbortController();
    uploadAbortControllerRef.current = controller;
    uploadInFlightRef.current = true;
    setImageUploading(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const result = await apiFormFetch<{ url: string }>(imageUploadUrl, formData, {
        signal: controller.signal,
      });

      const currentEditor = editorRef.current;
      if (!currentEditor || currentEditor.isDestroyed || controller.signal.aborted) return;
      // 파일 선택 창이 열린 뒤 사용자가 본문을 수정했다면, 과거 위치에 강제로
      // 삽입하지 않는다. 현재 선택 영역으로 이동해 최신 본문을 보존한다.
      const targetPosition = currentEditor.getHTML() === uploadInsertion.document
        ? uploadInsertion.position
        : currentEditor.state.selection.to;
      const safePosition = Math.min(Math.max(targetPosition, 1), currentEditor.state.doc.content.size);
      currentEditor
        .chain()
        .focus()
        .insertContentAt(safePosition, {
          type: "image",
          attrs: {
            src: result.url,
            alt,
            title: alt,
          },
      })
        .run();
      setImageAltText("");
      toast("success", t("imageUploaded"));
    } catch {
      if (controller.signal.aborted) {
        toast("info", t("uploadCancelled"));
      } else {
        toast("error", t("uploadFailed"));
      }
    } finally {
      if (uploadAbortControllerRef.current === controller) {
        uploadAbortControllerRef.current = null;
      }
      uploadInFlightRef.current = false;
      setImageUploading(false);
    }
  }, [enableImageUpload, imageAltText, imageUploadUrl, t, toast]);

  useEffect(() => {
    imageUploadHandlerRef.current = (file: File) => {
      void handleImageUpload(file);
    };
  }, [handleImageUpload]);

  if (!editor) return null;

  const addLink = () => {
    const url = window.prompt("URL");
    if (url) editor.chain().focus().extendMarkRange("link").setLink({ href: url }).run();
  };

  const handleCancelImageUpload = () => {
    uploadAbortControllerRef.current?.abort();
  };

  const openImagePicker = () => {
    if (!editor || editor.isDestroyed || imageUploading) return;
    uploadInsertionRef.current = {
      position: editor.state.selection.to,
      document: editor.getHTML(),
    };
    fileInputRef.current?.click();
  };

  const ToolButton = ({
    onClick,
    active,
    children,
    title,
    disabled = false,
  }: {
    onClick: () => void;
    active?: boolean;
    children: ReactNode;
    title?: string;
    disabled?: boolean;
  }) => (
    <button
      type="button"
      onPointerDown={(event) => event.preventDefault()}
      onClick={onClick}
      title={title}
      aria-label={title}
      disabled={disabled}
      className={cn(
        "rounded px-2 py-1 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        active ? "bg-accent-primary/20 text-accent-primary" : "text-text-secondary hover:bg-bg-surface",
      )}
    >
      {children}
    </button>
  );

  return (
    <>
      <div
        className={cn(
          "relative overflow-hidden rounded-lg border bg-bg-primary transition-colors",
          dragOver && enableImageUpload ? "border-accent-primary ring-2 ring-accent-primary/30" : "border-border",
        )}
        onDragEnter={(event) => {
          if (enableImageUpload && event.dataTransfer.types.includes("Files")) setDragOver(true);
        }}
        onDragLeave={(event) => {
          if (event.currentTarget.contains(event.relatedTarget as Node)) return;
          setDragOver(false);
        }}
        onDrop={() => setDragOver(false)}
      >
        <div className="flex flex-wrap items-center gap-0.5 border-b border-border px-3 py-2">
          <ToolButton onClick={() => editor.chain().focus().toggleBold().run()} active={editor.isActive("bold")} title={t("bold")}>
            <strong>B</strong>
          </ToolButton>
          <ToolButton onClick={() => editor.chain().focus().toggleItalic().run()} active={editor.isActive("italic")} title={t("italic")}>
            <em>I</em>
          </ToolButton>
          <ToolButton onClick={() => editor.chain().focus().toggleUnderline().run()} active={editor.isActive("underline")} title={t("underline")}>
            <span className="underline">U</span>
          </ToolButton>
          <ToolButton onClick={() => editor.chain().focus().toggleStrike().run()} active={editor.isActive("strike")} title={t("strikethrough")}>
            <span className="line-through">S</span>
          </ToolButton>

          <div className="mx-1 h-5 w-px bg-border" />

          <ToolButton onClick={() => editor.chain().focus().toggleHeading({ level: 2 }).run()} active={editor.isActive("heading", { level: 2 })} title="H2">H2</ToolButton>
          <ToolButton onClick={() => editor.chain().focus().toggleHeading({ level: 3 }).run()} active={editor.isActive("heading", { level: 3 })} title="H3">H3</ToolButton>

          <div className="mx-1 h-5 w-px bg-border" />

          <ToolButton onClick={() => editor.chain().focus().toggleBulletList().run()} active={editor.isActive("bulletList")} title={t("bulletList")}>
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
              <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" />
              <circle cx="4" cy="6" r="1" fill="currentColor" /><circle cx="4" cy="12" r="1" fill="currentColor" /><circle cx="4" cy="18" r="1" fill="currentColor" />
            </svg>
          </ToolButton>
          <ToolButton onClick={() => editor.chain().focus().toggleOrderedList().run()} active={editor.isActive("orderedList")} title={t("orderedList")}>
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
              <line x1="10" y1="6" x2="21" y2="6" /><line x1="10" y1="12" x2="21" y2="12" /><line x1="10" y1="18" x2="21" y2="18" />
              <text x="2" y="8" fontSize="8" fill="currentColor" stroke="none">1</text>
              <text x="2" y="14" fontSize="8" fill="currentColor" stroke="none">2</text>
              <text x="2" y="20" fontSize="8" fill="currentColor" stroke="none">3</text>
            </svg>
          </ToolButton>
          <ToolButton onClick={() => editor.chain().focus().toggleBlockquote().run()} active={editor.isActive("blockquote")} title={t("blockquote")}>
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
              <path d="M3 21c3 0 7-1 7-8V5c0-1.25-.756-2.017-2-2H4c-1.25 0-2 .75-2 1.972V11c0 1.25.75 2 2 2 1 0 0 1 0 1v1c0 1-1 1-2 2s-1 .008-1 1.031V21z" />
            </svg>
          </ToolButton>

          <div className="mx-1 h-5 w-px bg-border" />

          <ToolButton onClick={() => editor.chain().focus().toggleCodeBlock().run()} active={editor.isActive("codeBlock")} title={t("codeBlock")}>{"</>"}</ToolButton>
          <ToolButton onClick={addLink} active={editor.isActive("link")} title={t("link")}>
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
              <path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71" />
              <path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71" />
            </svg>
          </ToolButton>

          <div className="mx-1 h-5 w-px bg-border" />

          {!editor.isActive("table") ? (
            <ToolButton
              onClick={() => editor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()}
              title={t("insertTable")}
            >
              <span aria-hidden="true">▦</span>
            </ToolButton>
          ) : (
            <>
              <ToolButton onClick={() => editor.chain().focus().addRowAfter().run()} disabled={!editor.can().addRowAfter()} title={t("addRow")}>
                <span aria-hidden="true">+↕</span>
              </ToolButton>
              <ToolButton onClick={() => editor.chain().focus().deleteRow().run()} disabled={!editor.can().deleteRow()} title={t("deleteRow")}>
                <span aria-hidden="true">−↕</span>
              </ToolButton>
              <ToolButton onClick={() => editor.chain().focus().addColumnAfter().run()} disabled={!editor.can().addColumnAfter()} title={t("addColumn")}>
                <span aria-hidden="true">+↔</span>
              </ToolButton>
              <ToolButton onClick={() => editor.chain().focus().deleteColumn().run()} disabled={!editor.can().deleteColumn()} title={t("deleteColumn")}>
                <span aria-hidden="true">−↔</span>
              </ToolButton>
              <ToolButton onClick={() => editor.chain().focus().deleteTable().run()} disabled={!editor.can().deleteTable()} title={t("deleteTable")}>
                <span aria-hidden="true">×▦</span>
              </ToolButton>
            </>
          )}
          <ToolButton onClick={() => editor.chain().focus().undo().run()} disabled={!editor.can().undo()} title={t("undo")}>
            <span aria-hidden="true">↶</span>
          </ToolButton>
          <ToolButton onClick={() => editor.chain().focus().redo().run()} disabled={!editor.can().redo()} title={t("redo")}>
            <span aria-hidden="true">↷</span>
          </ToolButton>

          {enableImageUpload && (
            <>
              <div className="mx-1 h-5 w-px bg-border" />
              <ToolButton
                onClick={openImagePicker}
                title={imageUploading ? t("uploadingImage") : t("uploadImage")}
                disabled={imageUploading}
              >
                {imageUploading ? (
                  <span className="block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" aria-hidden="true" />
                ) : (
                  <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                    <rect x="3" y="3" width="18" height="18" rx="2" />
                    <circle cx="8.5" cy="8.5" r="1.5" />
                    <path d="m21 15-5-5L5 21" />
                  </svg>
                )}
              </ToolButton>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/png,image/jpeg,image/gif,image/webp"
                className="hidden"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) void handleImageUpload(file);
                  event.target.value = "";
                }}
              />
              <label className="mt-1 flex min-w-0 basis-full items-center gap-1 text-xs text-text-muted sm:ml-1 sm:mt-0 sm:flex-1 sm:basis-auto">
                <span className="sr-only">{t("imageAltLabel")}</span>
                <input
                  value={imageAltText}
                  onChange={(event) => setImageAltText(event.target.value)}
                  maxLength={200}
                  placeholder={t("imageAltPlaceholder")}
                  className="min-w-0 flex-1 rounded border border-border bg-bg-primary px-2 py-1 text-xs text-text-primary placeholder:text-text-muted focus:border-accent-primary focus:outline-none"
                  aria-label={t("imageAltLabel")}
                />
              </label>
            </>
          )}
        </div>

        {requiresRichContentEditorReview(value) && (
          <p className="border-b border-accent-warning/30 bg-accent-warning/10 px-4 py-2 text-xs text-accent-warning" role="status">
            {t("richContentReviewWarning")}
          </p>
        )}

        {enableImageUpload && (
          <div className="flex items-center justify-between gap-3 border-b border-border bg-bg-surface px-4 py-2 text-xs text-text-muted" aria-live="polite">
            <p>{imageUploading ? t("uploadingImage") : t("imageDropHint")}</p>
            {imageUploading && (
              <button
                type="button"
                onClick={handleCancelImageUpload}
                className="shrink-0 rounded border border-border px-2 py-1 text-text-secondary hover:bg-bg-primary hover:text-text-primary"
              >
                {t("cancel")}
              </button>
            )}
          </div>
        )}

        <EditorContent editor={editor} />

        {dragOver && enableImageUpload && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-bg-primary/85 text-sm font-medium text-accent-primary">
            {t("imageDropHint")}
          </div>
        )}
      </div>

      {previewImage && (
        <div
          className="fixed inset-0 z-[70] flex items-center justify-center bg-black/80 p-4"
          role="dialog"
          aria-modal="true"
          aria-label={t("imagePreview")}
          onClick={() => setPreviewImage(null)}
        >
          <div className="relative max-h-full max-w-6xl" onClick={(event) => event.stopPropagation()}>
            <button
              type="button"
              onClick={() => setPreviewImage(null)}
              className="absolute right-2 top-2 z-10 rounded-full bg-black/70 px-3 py-1.5 text-sm text-white hover:bg-black"
            >
              {t("close")}
            </button>
            {/* eslint-disable-next-line @next/next/no-img-element -- 업로드 직후 실제 원본을 확인하는 편집기 미리보기 */}
            <img
              src={previewImage.src}
              alt={previewImage.alt}
              className="max-h-[85vh] max-w-full rounded-xl bg-bg-primary object-contain shadow-2xl"
            />
          </div>
        </div>
      )}
    </>
  );
}
