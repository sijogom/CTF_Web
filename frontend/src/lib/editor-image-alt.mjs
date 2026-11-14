/**
 * Builds a short, readable image alternative text without introducing raw
 * Markdown delimiters into the document.
 */

/**
 * @param {string} requestedAlt
 * @param {string} filename
 * @param {string} fallback
 * @returns {string}
 */
export function buildImageAltText(requestedAlt, filename, fallback) {
  const normalizedRequested = requestedAlt
    .replace(/[\r\n]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (normalizedRequested) return normalizedRequested.slice(0, 200);

  const normalizedFilename = filename
    .replace(/\.[^.]+$/, "")
    .replace(/[-_]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return (normalizedFilename || fallback).slice(0, 200);
}

/**
 * @param {string} alt
 * @returns {string}
 */
export function escapeMarkdownImageAlt(alt) {
  return alt
    .replace(/\\/g, "\\\\")
    .replace(/\[/g, "\\[")
    .replace(/\]/g, "\\]")
    .replace(/\(/g, "\\(")
    .replace(/\)/g, "\\)")
    .replace(/[\r\n]+/g, " ");
}

/**
 * @param {string} url
 * @param {string} alt
 * @returns {string}
 */
export function buildMarkdownImage(url, alt) {
  return `![${escapeMarkdownImageAlt(alt)}](${url})`;
}
