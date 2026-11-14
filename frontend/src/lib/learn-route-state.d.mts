export type LearnMapFocus = {
  type: "challenge" | "course" | "lesson" | "knowledge_tag";
  id: string;
};

export function parseMapFocus(search: string): LearnMapFocus | null;

export function getLearnHydrationSearch(
  routeSearch: string,
  pathname: string,
  locationSearch: string,
): string | null;
