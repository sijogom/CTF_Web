import type { ApiRequestOwner } from "./api-client";

export interface ImageEditLease {
  readonly kind: string;
  readonly owner: ApiRequestOwner;
  readonly context: string;
  readonly generation: number;
}
export interface ImageEditOperation {
  setContext(key: string): void;
  mount(key: string): void;
  unmount(key: string): void;
  claim(kind: string): ImageEditLease | null;
  isCurrent(lease: ImageEditLease): boolean;
  release(lease: ImageEditLease): boolean;
  readonly busy: boolean;
}
export function imageFileError(file: Pick<File, "name" | "type" | "size">, kind: "banner" | "thumbnail"): "type" | "size" | null;
export function createImageEditOperation(options: {
  captureOwner: () => ApiRequestOwner;
  isCurrentOwner: (owner: ApiRequestOwner) => boolean;
  onChange: (key: string, kind: string | null) => void;
}): ImageEditOperation;
export function uploadEditorImage(options: {
  operation: ImageEditOperation;
  file: File;
  kind: "banner" | "thumbnail";
  upload: (file: File, owner: ApiRequestOwner) => Promise<string>;
  onSuccess: (url: string) => void;
  onError: (message: string | null) => void;
  messages: { type: string; size: string; failed: string };
}): Promise<void>;
