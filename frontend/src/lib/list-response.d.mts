export interface ListPage<Item> {
  items: Item[];
  total: number;
  page: number;
  size: number;
}

export function parseListPage<Item>(
  value: unknown,
  expectedPage?: number,
  expectedSize?: number,
): ListPage<Item>;

export interface OffsetListPage<Item> {
  items: Item[];
  total: number;
  offset: number;
  limit: number;
}

export function parseOffsetListPage<Item>(
  value: unknown,
  expectedOffset?: number,
  expectedLimit?: number,
): OffsetListPage<Item>;
