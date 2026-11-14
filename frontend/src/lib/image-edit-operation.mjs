const IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/gif"]);
const IMAGE_EXTENSIONS = /\.(?:png|jpe?g|gif|webp)$/i;

export function imageFileError(file, kind) {
  const supported = kind === "banner" ? IMAGE_TYPES.has(file.type) : IMAGE_EXTENSIONS.test(file.name);
  if (!supported) return "type";
  if (file.size > (kind === "banner" ? 5 : 10) * 1024 * 1024) return "size";
  return null;
}

// One parent-owned lease serializes image upload, persistence, and step changes.
export function createImageEditOperation({ captureOwner, isCurrentOwner, onChange }) {
  let context = "", mounted = false, generation = 0, active = null;
  const isCurrent = (lease) => mounted && active === lease && lease.generation === generation
    && lease.context === context && isCurrentOwner(lease.owner);
  return {
    setContext(key) {
      if (context === key) return;
      context = key;
      generation += 1;
      active = null;
      mounted = false;
    },
    mount(key) { if (context === key) mounted = true; },
    unmount(key) {
      if (context !== key) return;
      mounted = false;
      generation += 1;
      active = null;
    },
    claim(kind) {
      if (!mounted) return null;
      if (active && isCurrentOwner(active.owner)) return null;
      const owner = captureOwner();
      active = Object.freeze({ kind, owner, context, generation });
      onChange(context, kind);
      return active;
    },
    isCurrent,
    release(lease) {
      if (!mounted || active !== lease || lease.context !== context || lease.generation !== generation) return false;
      active = null;
      onChange(context, null);
      return true;
    },
    get busy() { return mounted && active !== null && isCurrentOwner(active.owner); },
  };
}

export async function uploadEditorImage({ operation, file, kind, upload, onSuccess, onError, messages }) {
  let lease;
  try {
    lease = operation.claim("upload");
    if (!lease) return;
    const invalid = imageFileError(file, kind);
    if (invalid) {
      onError(messages[invalid]);
      return;
    }
    onError(null);
    const url = await upload(file, lease.owner);
    if (!operation.isCurrent(lease)) return;
    if (typeof url !== "string" || !url.trim()) throw new Error("Invalid upload response");
    onSuccess(url);
  } catch {
    if (!lease || operation.isCurrent(lease)) onError(messages.failed);
  } finally {
    if (lease) operation.release(lease);
  }
}
