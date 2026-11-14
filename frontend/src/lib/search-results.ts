export type SearchSource = "challenge" | "course" | "post";

export interface SearchSourceTask<Result> {
  source: SearchSource;
  request: Promise<SearchResultBatch<Result>>;
}

export interface SettledSearch<Result> {
  results: Result[];
  failedSources: SearchSource[];
  totals: Partial<Record<SearchSource, number>>;
}

export interface SearchResultBatch<Result> {
  items: Result[];
  total: number;
}

/** 배열 및 기존/opt-in envelope 응답을 같은 방식으로 읽는다. */
export function listItems<Item>(response: Item[] | { items?: Item[] }): Item[] {
  if (Array.isArray(response)) return response;
  if (response && Array.isArray(response.items)) return response.items;
  throw new Error("Invalid list response: items array is missing");
}

/** 검색 API의 items/total 계약을 검증해 shape 오류를 정상 0건과 구분한다. */
export function searchBatch<Item>(response: { items?: Item[]; total?: number }): SearchResultBatch<Item> {
  if (!response || !Array.isArray(response.items)) {
    throw new Error("Invalid search response: items array is missing");
  }
  if (!Number.isSafeInteger(response.total) || (response.total ?? -1) < 0) {
    throw new Error("Invalid search response: total is missing");
  }
  return { items: response.items, total: response.total as number };
}

/** 소스 순서를 보존하면서 부분 실패와 전체 실패를 호출자가 구분할 수 있게 한다. */
export async function settleSearchSources<Result>(
  tasks: Array<SearchSourceTask<Result>>,
): Promise<SettledSearch<Result>> {
  const settled = await Promise.allSettled(tasks.map((task) => task.request));
  const results: Result[] = [];
  const failedSources: SearchSource[] = [];
  const totals: Partial<Record<SearchSource, number>> = {};

  settled.forEach((result, index) => {
    if (result.status === "fulfilled") {
      results.push(...result.value.items);
      totals[tasks[index].source] = result.value.total;
    } else {
      failedSources.push(tasks[index].source);
    }
  });

  return { results, failedSources, totals };
}
