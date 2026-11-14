import MarkdownIt from "markdown-it";
import DOMPurify from "isomorphic-dompurify";

export const RICH_CONTENT_TAGS = Object.freeze([
  "p", "br", "hr", "strong", "em", "u", "s", "code", "pre", "span",
  "h1", "h2", "h3", "h4", "h5", "h6",
  "ul", "ol", "li", "blockquote", "a", "img", "div",
  "table", "thead", "tbody", "tfoot", "tr", "th", "td",
]);

export const RICH_CONTENT_ATTRIBUTES = Object.freeze([
  "href", "src", "alt", "title", "class", "loading", "target", "rel",
  "colspan", "rowspan", "align", "start", "value", "scope",
]);

const markdownRenderer = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: false,
  typographer: false,
});

const contentClassifier = new MarkdownIt({
  html: true,
  breaks: false,
  linkify: false,
  typographer: false,
});

for (const tokenType of ["th_open", "td_open"]) {
  markdownRenderer.renderer.rules[tokenType] = (tokens, index, options, _environment, renderer) => {
    const token = tokens[index];
    const alignment = /^text-align:(left|center|right)$/.exec(token.attrGet("style") ?? "")?.[1];
    if (token.attrs) token.attrs = token.attrs.filter(([name]) => name !== "style");
    if (alignment) token.attrSet("align", alignment);
    return renderer.renderToken(tokens, index, options);
  };
}

const MARKDOWN_BLOCK_TOKENS = new Set([
  "blockquote_open",
  "bullet_list_open",
  "code_block",
  "fence",
  "heading_open",
  "hr",
  "ordered_list_open",
  "table_open",
]);

const MARKDOWN_INLINE_TOKENS = new Set([
  "code_inline",
  "em_open",
  "image",
  "link_open",
  "s_open",
  "strong_open",
]);

const VOID_HTML_TAGS = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"]);
const SUPPORTED_HTML_TAGS = new Set(RICH_CONTENT_TAGS);
const GENERIC_HTML_ATTRIBUTES = new Set(["class", "title"]);
const HTML_ATTRIBUTES_BY_TAG = Object.freeze({
  a: new Set(["href", "target", "rel"]),
  img: new Set(["src", "alt", "loading"]),
  ol: new Set(["start"]),
  li: new Set(["value"]),
  th: new Set(["colspan", "rowspan", "scope", "align"]),
  td: new Set(["colspan", "rowspan", "align"]),
});
const CLASS_TOKEN_PATTERN = /^[A-Za-z\d_-]+$/;
const CODE_LANGUAGE_CLASS_TOKEN_PATTERN = /^language-[A-Za-z\d_+#.-]+$/;
const REL_TOKEN_PATTERN = /^[A-Za-z][A-Za-z\d_-]*$/;
const INTEGER_PATTERN = /^-?\d+$/;
const LOADING_VALUES = new Set(["lazy", "eager", "auto"]);
const TABLE_ALIGNMENTS = new Set(["left", "center", "right"]);
const TABLE_SCOPES = new Set(["row", "col", "rowgroup", "colgroup"]);
const CONTROL_CHARACTER_PATTERN = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/;
const UNSAFE_MARKDOWN_URL_PATTERN = /!?\[[^\]]*\]\(\s*(?:javascript|data|vbscript)\s*:/i;
const BLOCK_TEXT_SEPARATORS = new Set([
  "P", "DIV", "PRE", "H1", "H2", "H3", "H4", "H5", "H6",
  "LI", "BLOCKQUOTE", "TR", "TABLE",
]);

const PURIFY_CONFIG = Object.freeze({
  ALLOWED_TAGS: RICH_CONTENT_TAGS,
  ALLOWED_ATTR: RICH_CONTENT_ATTRIBUTES,
  ALLOW_ARIA_ATTR: false,
  ALLOW_DATA_ATTR: false,
  FORBID_ATTR: ["style"],
  FORBID_TAGS: ["script", "style"],
  RETURN_DOM: true,
});

function hasMarkdownSyntax(tokens) {
  return tokens.some((token) => {
    if (MARKDOWN_BLOCK_TOKENS.has(token.type)) return true;
    if (token.type !== "inline" || !token.children) return false;
    return token.children.some((child) => MARKDOWN_INLINE_TOKENS.has(child.type));
  });
}

function hasHtmlSyntax(tokens) {
  return tokens.some((token) => {
    if (token.type === "html_block" || token.type === "html_inline") return true;
    return token.children?.some((child) => child.type === "html_inline") ?? false;
  });
}

/**
 * Markdown-it identifies HTML tokens, while this small stack check only rejects
 * incomplete/mismatched legacy markup before it reaches a lossy editor parser.
 */
function hasBalancedHtmlTags(value) {
  const stack = [];
  const tags = value.matchAll(/<!--(?:[\s\S]*?)-->|<![^>]*>|<\/?([A-Za-z][\w:-]*)\b(?:[^>"']|"[^"]*"|'[^']*')*>/g);
  for (const match of tags) {
    const raw = match[0];
    const name = match[1]?.toLowerCase();
    if (!name || raw.startsWith("<!")) continue;
    if (raw.startsWith("</")) {
      if (stack.pop() !== name) return false;
    } else if (!raw.endsWith("/>") && !VOID_HTML_TAGS.has(name)) {
      stack.push(name);
    }
  }
  return stack.length === 0;
}

function hasPreservableRawAttributes(value) {
  const tags = value.matchAll(/<\/?([A-Za-z][\w:-]*)\b(?:[^>"']|"[^"]*"|'[^']*')*>/g);
  for (const match of tags) {
    const raw = match[0];
    if (raw.startsWith("</")) continue;
    const tag = match[1].toLowerCase();
    const allowed = new Set([...GENERIC_HTML_ATTRIBUTES, ...(HTML_ATTRIBUTES_BY_TAG[tag] ?? [])]);
    const seen = new Set();
    let cursor = raw.indexOf(match[1]) + match[1].length;
    const end = raw.length - 1;

    while (cursor < end) {
      while (cursor < end && /\s/u.test(raw[cursor])) cursor += 1;
      if (cursor >= end || (raw[cursor] === "/" && cursor === end - 1)) break;

      const nameMatch = /^[^\s"'<>\/=]+/.exec(raw.slice(cursor));
      if (!nameMatch) return false;
      const name = nameMatch[0].toLowerCase();
      if (!allowed.has(name) || seen.has(name)) return false;
      seen.add(name);
      cursor += nameMatch[0].length;
      while (cursor < end && /\s/u.test(raw[cursor])) cursor += 1;
      if (raw[cursor] !== "=") return false;
      cursor += 1;
      while (cursor < end && /\s/u.test(raw[cursor])) cursor += 1;
      if (cursor >= end) return false;

      const quote = raw[cursor] === '"' || raw[cursor] === "'" ? raw[cursor] : null;
      if (quote) {
        cursor += 1;
        const closingQuote = raw.indexOf(quote, cursor);
        if (closingQuote < 0 || closingQuote >= end) return false;
        cursor = closingQuote + 1;
      } else {
        const valueMatch = /^[^\s"'=<>`]+/.exec(raw.slice(cursor));
        if (!valueMatch) return false;
        cursor += valueMatch[0].length;
      }
    }
  }
  return true;
}

function normalizeHtmlStructure(nodes, parentTag = null) {
  const normalized = [];
  for (const node of nodes) {
    if (node === "#text") {
      normalized.push(node);
      continue;
    }
    const children = normalizeHtmlStructure(node.children, node.tag);
    // HTML5 inserts tbody around a tr that is a direct table child. Both the
    // explicit and implicit forms have the same supported table semantics.
    if (node.tag === "tbody" && parentTag === "table") normalized.push(...children);
    else normalized.push({ tag: node.tag, children });
  }
  return normalized;
}

function rawHtmlStructure(value) {
  const root = [];
  const stack = [{ tag: null, children: root }];
  const tagPattern = /<\/?([A-Za-z][\w:-]*)\b(?:[^>"']|"[^"]*"|'[^']*')*>/g;
  let textStart = 0;
  for (const match of value.matchAll(tagPattern)) {
    const textBefore = value.slice(textStart, match.index);
    if (textBefore.trim()) stack.at(-1).children.push("#text");
    const raw = match[0];
    const tag = match[1].toLowerCase();
    if (raw.startsWith("</")) {
      stack.pop();
    } else {
      const node = { tag, children: [] };
      stack.at(-1).children.push(node);
      if (!raw.endsWith("/>") && !VOID_HTML_TAGS.has(tag)) stack.push(node);
    }
    textStart = match.index + raw.length;
  }
  if (value.slice(textStart).trim()) stack.at(-1).children.push("#text");
  return normalizeHtmlStructure(root);
}

function parsedHtmlStructure(fragment) {
  const visit = (parent) => Array.from(parent.childNodes).flatMap((node) => {
    if (node.nodeType === 3) return node.textContent?.trim() ? ["#text"] : [];
    if (node.nodeType !== 1) return [];
    return [{ tag: node.tagName.toLowerCase(), children: visit(node) }];
  });
  return normalizeHtmlStructure(visit(fragment));
}

function hasPreservedHtmlStructure(value, fragment) {
  return JSON.stringify(rawHtmlStructure(value)) === JSON.stringify(parsedHtmlStructure(fragment));
}

function isSafeUrl(value, { image = false } = {}) {
  const url = value.trim();
  if (
    !url
    || url !== value
    || CONTROL_CHARACTER_PATTERN.test(url)
    || /\s/u.test(url)
    || url.startsWith("//")
    || url.includes("\\")
  ) {
    return false;
  }

  const schemeMatch = /^([A-Za-z][A-Za-z\d+.-]*):/.exec(url);
  if (!schemeMatch) return true;
  const scheme = schemeMatch[1].toLowerCase();
  return image ? scheme === "http" || scheme === "https" : scheme === "http" || scheme === "https" || scheme === "mailto";
}

function isSupportedAttribute(element, attribute) {
  const tag = element.tagName.toLowerCase();
  const name = attribute.name.toLowerCase();
  const value = attribute.value;
  const tagAttributes = HTML_ATTRIBUTES_BY_TAG[tag] ?? new Set();
  if (!GENERIC_HTML_ATTRIBUTES.has(name) && !tagAttributes.has(name)) return false;
  if (CONTROL_CHARACTER_PATTERN.test(value)) return false;
  if (name === "href") return isSafeUrl(value);
  if (name === "src") return isSafeUrl(value, { image: true });
  if (name === "class") {
    const tokens = value.split(/\s+/).filter(Boolean);
    return tokens.length > 0 && tokens.every((token) => (
      CLASS_TOKEN_PATTERN.test(token)
      || (element.tagName === "CODE" && CODE_LANGUAGE_CLASS_TOKEN_PATTERN.test(token))
    ));
  }
  if (name === "target") return value === "_blank";
  if (name === "rel") {
    const tokens = value.split(/\s+/).filter(Boolean);
    return tokens.length > 0 && tokens.every((token) => REL_TOKEN_PATTERN.test(token));
  }
  if (name === "loading") return LOADING_VALUES.has(value);
  if (name === "colspan" || name === "rowspan") return boundedTableSpan(value) !== null;
  if (name === "scope") return TABLE_SCOPES.has(value);
  if (name === "align") return TABLE_ALIGNMENTS.has(value);
  if (name === "start" || name === "value") return INTEGER_PATTERN.test(value);
  return true;
}

function hasOnlySupportedHtmlSyntax(value) {
  if (/<!--[\s\S]*?-->|<![A-Za-z\[]|<\?/i.test(value) || !hasPreservableRawAttributes(value)) return false;
  const root = DOMPurify.sanitize("", { RETURN_DOM: true });
  const template = root.ownerDocument.createElement("template");
  template.innerHTML = value;
  if (!hasPreservedHtmlStructure(value, template.content)) return false;
  for (const element of template.content.querySelectorAll("*")) {
    const tag = element.tagName.toLowerCase();
    if (!SUPPORTED_HTML_TAGS.has(tag)) return false;
    for (const attribute of element.attributes) {
      if (!isSupportedAttribute(element, attribute)) return false;
    }
    if (tag === "img" && !element.hasAttribute("src")) return false;
  }
  return true;
}

function hasTopLevelText(value) {
  const root = DOMPurify.sanitize(value, {
    RETURN_DOM: true,
    ALLOW_ARIA_ATTR: false,
    ALLOW_DATA_ATTR: false,
  });
  return Array.from(root.childNodes).some(
    (node) => node.nodeType === 3 && Boolean(node.textContent?.trim()),
  );
}

/**
 * Classify persisted legacy content without using a starting-tag regex as the
 * format authority. Mixed or malformed HTML is deliberately review-required.
 *
 * @returns {"html" | "markdown" | "plain" | "ambiguous"}
 */
export function classifyRichContent(value) {
  const source = typeof value === "string" ? value : "";
  if (!source.trim()) return "plain";
  if (CONTROL_CHARACTER_PATTERN.test(source)) return "ambiguous";

  const tokens = contentClassifier.parse(source, {});
  const hasHtml = hasHtmlSyntax(tokens);
  const hasMarkdown = hasMarkdownSyntax(tokens);

  if (hasHtml) {
    if (hasMarkdown || !hasBalancedHtmlTags(source) || !hasOnlySupportedHtmlSyntax(source) || hasTopLevelText(source)) {
      return "ambiguous";
    }
    return "html";
  }
  if (UNSAFE_MARKDOWN_URL_PATTERN.test(source)) return "ambiguous";
  if (!hasMarkdown && /<[A-Za-z][^>\r\n]{0,200}>/.test(source)) return "ambiguous";
  return hasMarkdown ? "markdown" : "plain";
}

function boundedTableSpan(value) {
  if (!/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 1 && parsed <= 100 ? String(parsed) : null;
}

function normalizeSanitizedDom(root) {
  for (const element of root.querySelectorAll("*")) {
    const classTokens = element.getAttribute("class")?.split(/\s+/).filter(Boolean) ?? [];
    if (element.hasAttribute("class") && (
      classTokens.length === 0
      || classTokens.some((token) => (
        !CLASS_TOKEN_PATTERN.test(token)
        && !(element.tagName === "CODE" && CODE_LANGUAGE_CLASS_TOKEN_PATTERN.test(token))
      ))
    )) {
      element.removeAttribute("class");
    }
    if (element.tagName !== "A") {
      element.removeAttribute("href");
      element.removeAttribute("target");
      element.removeAttribute("rel");
    }
    if (element.tagName !== "IMG") {
      element.removeAttribute("src");
      element.removeAttribute("alt");
      element.removeAttribute("loading");
    }
    if (element.tagName !== "TH" && element.tagName !== "TD") {
      element.removeAttribute("colspan");
      element.removeAttribute("rowspan");
      element.removeAttribute("align");
    }
    if (element.tagName !== "TH") element.removeAttribute("scope");
    if (element.tagName !== "OL") element.removeAttribute("start");
    if (element.tagName !== "LI") element.removeAttribute("value");
  }

  for (const anchor of root.querySelectorAll("a")) {
    const href = anchor.getAttribute("href");
    if (!href || !isSafeUrl(href)) {
      anchor.removeAttribute("href");
      anchor.removeAttribute("target");
      anchor.removeAttribute("rel");
      continue;
    }

    const target = anchor.getAttribute("target");
    if (target !== "_blank") anchor.removeAttribute("target");
    const relTokens = (anchor.getAttribute("rel") ?? "")
      .split(/\s+/)
      .filter((token) => REL_TOKEN_PATTERN.test(token));
    if (anchor.getAttribute("target") === "_blank") {
      if (!relTokens.includes("noopener")) relTokens.push("noopener");
      if (!relTokens.includes("noreferrer")) relTokens.push("noreferrer");
    }
    const normalizedRel = relTokens.join(" ");
    if (normalizedRel) {
      anchor.setAttribute("rel", normalizedRel);
    } else {
      anchor.removeAttribute("rel");
    }
  }

  for (const image of root.querySelectorAll("img")) {
    const src = image.getAttribute("src");
    if (!src || !isSafeUrl(src, { image: true })) image.removeAttribute("src");
    const loading = image.getAttribute("loading");
    if (loading !== null && !LOADING_VALUES.has(loading)) image.removeAttribute("loading");
  }

  for (const cell of root.querySelectorAll("th, td")) {
    for (const attribute of ["colspan", "rowspan"]) {
      const value = cell.getAttribute(attribute);
      if (value === null) continue;
      const bounded = boundedTableSpan(value);
      if (!bounded) cell.removeAttribute(attribute);
    }
    const alignment = cell.getAttribute("align");
    if (alignment !== null && !TABLE_ALIGNMENTS.has(alignment)) cell.removeAttribute("align");
  }

  for (const list of root.querySelectorAll("ol[start]")) {
    if (!INTEGER_PATTERN.test(list.getAttribute("start") ?? "")) list.removeAttribute("start");
  }

  for (const item of root.querySelectorAll("li[value]")) {
    if (!INTEGER_PATTERN.test(item.getAttribute("value") ?? "")) item.removeAttribute("value");
  }

  for (const header of root.querySelectorAll("th[scope]")) {
    const scope = header.getAttribute("scope");
    if (scope !== null && !TABLE_SCOPES.has(scope)) header.removeAttribute("scope");
  }

  // Canonical rich HTML has element roots. Sanitization of a removed wrapper
  // can expose text nodes, so wrap those nodes instead of returning bare text.
  for (const node of Array.from(root.childNodes)) {
    if (node.nodeType !== 3 || !node.textContent?.trim()) continue;
    const paragraph = root.ownerDocument.createElement("p");
    root.insertBefore(paragraph, node);
    paragraph.appendChild(node);
  }
}

/** Sanitize canonical HTML with the client/server shared allowlist contract. */
export function sanitizeRichHtml(value) {
  const root = DOMPurify.sanitize(typeof value === "string" ? value : "", PURIFY_CONFIG);
  normalizeSanitizedDom(root);
  return root.innerHTML.trim();
}

function escapeHtmlText(value) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** Convert unambiguous legacy Markdown/plain text to canonical safe HTML. */
export function legacyRichContentToHtml(value) {
  const source = typeof value === "string" ? value : "";
  const format = classifyRichContent(source);
  if (format === "ambiguous") return `<pre><code>${escapeHtmlText(source)}</code></pre>`;
  if (format === "html") return sanitizeRichHtml(source);
  return sanitizeRichHtml(markdownRenderer.render(source));
}

function normalizeDisplayBasePath(basePath) {
  const value = typeof basePath === "string" ? basePath.trim() : "";
  if (!value || value === "/") return "";
  return `/${value.replace(/^\/+|\/+$/g, "")}`;
}

/**
 * Resolve app-owned rich-content image routes for display without changing the
 * canonical URL stored in the document. External and author-supplied relative
 * URLs remain untouched.
 */
export function richContentImageSrcForDisplay(src, basePath = "/ctf") {
  const value = typeof src === "string" ? src : "";
  if (!value.startsWith("/api/") && !value.startsWith("/storage/")) return value;
  const normalizedBasePath = normalizeDisplayBasePath(basePath);
  return normalizedBasePath ? `${normalizedBasePath}${value}` : value;
}

/** Return sanitized HTML with app-owned image URLs adjusted for this view. */
export function richContentHtmlForDisplay(value, basePath = "/ctf") {
  const html = legacyRichContentToHtml(value);
  const root = DOMPurify.sanitize(html, PURIFY_CONFIG);
  normalizeSanitizedDom(root);
  for (const image of root.querySelectorAll("img[src]")) {
    image.setAttribute(
      "src",
      richContentImageSrcForDisplay(image.getAttribute("src") ?? "", basePath),
    );
  }
  return root.innerHTML.trim();
}

function appendContentMetrics(node, summaryChunks, visibleChunks, inPre = false) {
  if (node.nodeType === 3) {
    const text = node.textContent ?? "";
    if (text.trim() || inPre) {
      summaryChunks.push(text);
      visibleChunks.push(text);
    }
    return;
  }
  if (node.nodeType !== 1) return;

  const element = node;
  if (element.tagName === "BR") summaryChunks.push("\n");
  if (element.tagName === "IMG") {
    const alt = element.getAttribute("alt");
    if (alt) {
      summaryChunks.push(alt);
      visibleChunks.push(alt);
    }
  }
  for (const child of element.childNodes) {
    appendContentMetrics(child, summaryChunks, visibleChunks, inPre || element.tagName === "PRE");
  }
  if (element.tagName === "TH" || element.tagName === "TD") summaryChunks.push("\t");
  else if (BLOCK_TEXT_SEPARATORS.has(element.tagName)) summaryChunks.push("\n");
}

function collectContentMetrics(value) {
  const root = DOMPurify.sanitize(legacyRichContentToHtml(value), PURIFY_CONFIG);
  const summaryChunks = [];
  const visibleChunks = [];
  for (const child of root.childNodes) appendContentMetrics(child, summaryChunks, visibleChunks);
  return {
    plainText: summaryChunks
      .join("")
      .replace(/\u00a0/g, " ")
      .replace(/[ \f\v]+/g, " ")
      .replace(/\t+\n/g, "\n")
      .replace(/ *\n */g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim(),
    visibleText: visibleChunks.join("").trim(),
  };
}

/** Extract user-visible text for previews and character counters. */
export function richContentToPlainText(value, maxChars) {
  const text = collectContentMetrics(value).plainText;

  if (maxChars === undefined || maxChars === null) return text;
  const limit = Math.max(0, Number.isFinite(maxChars) ? Math.floor(maxChars) : 0);
  const result = [];
  let visibleCount = 0;
  for (const character of Array.from(text)) {
    if (character === "\n" || character === "\t") {
      if (visibleCount < limit) result.push(character);
      continue;
    }
    if (visibleCount >= limit) break;
    result.push(character);
    visibleCount += 1;
  }
  return result.join("").trim();
}

/** Python `len` parity: Unicode code points, excluding structural separators. */
export function richContentVisibleLength(value) {
  return Array.from(collectContentMetrics(value).visibleText).length;
}

/** Empty paragraphs/line breaks are empty; an allowed image is meaningful. */
export function isRichContentEmpty(value) {
  const html = legacyRichContentToHtml(value);
  const root = DOMPurify.sanitize(html, PURIFY_CONFIG);
  const hasUsableImage = Array.from(root.querySelectorAll("img")).some((image) => Boolean(image.getAttribute("src")));
  return !hasUsableImage && !richContentToPlainText(html).trim();
}
