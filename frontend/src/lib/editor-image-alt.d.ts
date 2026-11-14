export function buildImageAltText(
  requestedAlt: string,
  filename: string,
  fallback: string,
): string;

export function escapeMarkdownImageAlt(alt: string): string;

export function buildMarkdownImage(url: string, alt: string): string;
