const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const fail = (code) => { throw Object.assign(new Error(code), { attachmentCode: code }); };
const requireValue = (condition, code = "failed") => { if (!condition) fail(code); };
export function attachmentFileError(file) {
  if (!file || typeof file.name !== "string" || !file.name || /\.\.|[/\\\0]/.test(file.name)) return "name";
  if (!Number.isSafeInteger(file.size) || file.size < 0 || file.size > 100 * 1024 * 1024) return "size";
  return null;
}
export function attachmentIds(attachments) { return attachments.filter(item => item.id !== null).map(item => item.id); }
function descriptors(value) {
  requireValue(Array.isArray(value));
  for (const item of value) requireValue(item && (item.id === null || UUID.test(item.id)) && typeof item.name === "string" && ["ready", "unavailable"].includes(item.state));
  const ids = attachmentIds(value); requireValue(new Set(ids).size === ids.length);
  return value;
}
function uploadResult(value, item, draftId) {
  requireValue(value && value.id === item.id && value.draft_id === draftId && value.original_name === item.name && value.byte_size === item.byte_size
    && /^[a-f0-9]{64}$/.test(value.sha256) && ["pending", "ready", "missing"].includes(value.state)
    && ["ready_for_attach", "reconcile_required", "retry_allowed"].every(key => typeof value[key] === "boolean")
    && (!value.ready_for_attach || value.state === "ready" && !value.reconcile_required)
    && (!value.retry_allowed || value.state === "missing"));
  return { ...item, ...value, name: value.original_name, error: null };
}

export function createChallengeAttachments({ operation, initial, files, request, onChange, onCanonical = () => {}, makeId = () => crypto.randomUUID() }) {
  let target = initial.challengeId ?? null, revision = initial.revision, baseline = attachmentIds(descriptors(initial.attachments));
  let items = initial.attachments.filter(item => item.id !== null).map(item => ({ ...item, existing: true, ready_for_attach: true, retry_allowed: false, reconcile_required: false, attempted: true, error: null }));
  let draftId = null, draftConfirmed = false, error = null, conflict = false, restoreFailed = false;
  const base = () => `/api/learn/challenge-attachment-drafts/${draftId}`;
  const current = lease => { if (!operation.isCurrent(lease)) fail("stale"); };
  const dirty = () => JSON.stringify(items.map(item => item.id)) !== JSON.stringify(baseline);
  const snapshot = () => ({ items: items.map(item => ({ ...item, canRetry: files.has(item.id) && (!item.attempted || item.state === "missing" && item.retry_allowed) })),
    dirty: dirty(), blocked: conflict || restoreFailed || items.some(item => !item.existing && !item.ready_for_attach), error, conflict,
    attachments: items.map(item => ({ id: item.id, legacy_index: null, name: item.name, byte_size: item.byte_size, sha256: item.sha256 ?? null,
      state: item.ready_for_attach && item.state === "ready" ? "ready" : "unavailable", download_path: item.download_path ?? null })) });
  const publish = () => onChange(snapshot());
  const call = async (lease, method, path, body) => { current(lease); const result = await request(method, path, body, lease.owner); current(lease); return result; };
  function checkDraft(value) {
    requireValue(value?.draft_id === draftId && value.challenge_id === target && value.state === "open"
      && Number.isSafeInteger(value.base_attachment_revision) && Number.isSafeInteger(value.current_attachment_revision));
    if (value.base_attachment_revision !== revision || value.current_attachment_revision !== revision) { conflict = true; error = "conflict"; publish(); fail("conflict"); }
    descriptors(value.attachments); requireValue(Array.isArray(value.uploads)); return value;
  }
  async function ensureDraft(lease) {
    if (draftConfirmed) return;
    if (draftId) {
      try { const value = await call(lease, "GET", base()); current(lease); checkDraft(value); draftConfirmed = true; return; }
      catch (failure) { current(lease); if (failure.status !== 404) throw failure; }
    } else draftId = makeId();
    const value = await call(lease, "POST", "/api/learn/challenge-attachment-drafts", { draft_id: draftId, ...(target === null ? {} : { challenge_id: target }) });
    current(lease); checkDraft(value); draftConfirmed = true;
  }
  async function run(kind, work) {
    let lease;
    try { lease = operation.claim(kind); if (!lease) return; error = null; return await work(lease); }
    catch (failure) { if (lease && operation.isCurrent(lease)) { error = failure.attachmentCode ?? "failed"; publish(); } }
    finally { if (lease) operation.release(lease); }
  }
  async function put(lease, item) {
    const file = files.get(item.id); requireValue(file, "fileUnavailable"); await ensureDraft(lease); current(lease);
    item.state = "uploading"; item.error = null; publish(); item.attempted = true;
    try { const value = await call(lease, "PUT", `${base()}/uploads/${item.id}`, file); current(lease); Object.assign(item, uploadResult(value, item, draftId)); }
    catch (failure) { current(lease); item.state = "pending"; item.ready_for_attach = false; item.retry_allowed = false; item.error = "failed"; throw failure; }
    finally { if (operation.isCurrent(lease)) publish(); }
  }
  return {
    snapshot,
    add(selected) { return run("attachment-upload", async lease => {
      const queued = [];
      for (const file of selected) { current(lease); const invalid = attachmentFileError(file); if (invalid) { error = invalid; publish(); continue; }
        const item = { id: makeId(), name: file.name, byte_size: file.size, sha256: null, state: "queued", existing: false, attempted: false, ready_for_attach: false, retry_allowed: false, reconcile_required: false, error: null };
        files.set(item.id, file); items.push(item); queued.push(item);
      }
      publish(); for (const item of queued) { current(lease); await put(lease, item); }
    }); },
    check(id, reconcile = false) { return run("attachment-status", async lease => {
      const item = items.find(value => value.id === id); requireValue(item && !item.existing && draftId);
      try { const value = await call(lease, reconcile ? "POST" : "GET", `${base()}/uploads/${id}${reconcile ? "/reconcile" : ""}`, reconcile ? {} : undefined); current(lease); Object.assign(item, uploadResult(value, item, draftId)); }
      catch (failure) {
        current(lease);
        if (!reconcile && failure.status === 404) { const value = await call(lease, "GET", base()); current(lease); checkDraft(value); draftConfirmed = true; item.state = "missing"; item.ready_for_attach = false; item.retry_allowed = true; item.reconcile_required = false; item.error = null; }
        else throw failure;
      }
      publish();
    }); },
    retry(id) { return run("attachment-upload", async lease => { const item = items.find(value => value.id === id); requireValue(item && files.has(id), "fileUnavailable"); requireValue(!item.attempted || item.state === "missing" && item.retry_allowed, "pending"); await put(lease, item); }); },
    remove(id) { return run("attachment-remove", async () => { items = items.filter(item => item.id !== id); files.delete(id); publish(); }); },
    move(id, delta) { return run("attachment-reorder", async () => { const index = items.findIndex(item => item.id === id), next = index + delta; if (index < 0 || next < 0 || next >= items.length || ![-1, 1].includes(delta)) return; [items[index], items[next]] = [items[next], items[index]]; publish(); }); },
    async prepareSave(lease, legacyChanged = false) {
      current(lease); if (!dirty() && !legacyChanged) return {};
      requireValue(!snapshot().blocked, conflict ? "conflict" : "pending"); await ensureDraft(lease); current(lease);
      return { attachment_draft_id: draftId, attachment_ids: items.map(item => item.id) };
    },
    acceptSaved(value, lease) {
      current(lease); requireValue(Number.isSafeInteger(value?.id) && value.id > 0 && Number.isSafeInteger(value.attachment_revision) && value.attachment_revision >= 0);
      descriptors(value.attachments); target = value.id; revision = value.attachment_revision; baseline = attachmentIds(value.attachments);
      items = value.attachments.filter(item => item.id !== null).map(item => ({ ...item, existing: true, ready_for_attach: true, retry_allowed: false, reconcile_required: false, attempted: true, error: null }));
      draftId = null; draftConfirmed = false; error = null; conflict = false; restoreFailed = false; files.clear(); publish(); onCanonical(value);
    },
    saveFailed(failure) { if (failure?.status === 409 && failure.code === "attachment_revision_conflict") { conflict = true; error = "conflict"; publish(); } },
    reload() { return run("attachment-status", async lease => {
      requireValue(target !== null); const value = await call(lease, "GET", `/api/learn/challenges/${target}`);
      current(lease); requireValue(value.id === target && Array.isArray(value.files)); this.acceptSaved(value, lease); return value;
    }); },
    metadata() { return draftId ? { draftId, challengeId: target, revision, items: items.map(item => ({ id: item.id, name: item.name, byte_size: item.byte_size, existing: item.existing })) } : null; },
    restore(metadata) { return run("attachment-status", async lease => {
      requireValue(metadata && UUID.test(metadata.draftId) && metadata.challengeId === target && metadata.revision === revision && Array.isArray(metadata.items), "restore");
      requireValue(metadata.items.every(item => item && UUID.test(item.id) && typeof item.name === "string" && typeof item.existing === "boolean" && (item.byte_size === null || Number.isSafeInteger(item.byte_size) && item.byte_size >= 0)), "restore");
      requireValue(new Set(metadata.items.map(item => item.id)).size === metadata.items.length, "restore");
      draftId = metadata.draftId; restoreFailed = true;
      items = metadata.items.map(item => ({ ...item, state: "pending", ready_for_attach: false, retry_allowed: false, reconcile_required: false, attempted: true, error: null })); publish();
      try {
        const response = await call(lease, "GET", base()); current(lease); const value = checkDraft(response);
        items = metadata.items.map(item => {
          if (item.existing) { const existing = value.attachments.find(entry => entry.id === item.id); requireValue(existing, "restore"); return { ...existing, existing: true, ready_for_attach: true, retry_allowed: false, reconcile_required: false, attempted: true, error: null }; }
          const upload = value.uploads.find(entry => entry.id === item.id); requireValue(upload, "restore"); return uploadResult({ ...upload }, { ...item, attempted: true, existing: false }, draftId);
        });
        draftConfirmed = true; restoreFailed = false; publish(); return true;
      } catch (failure) { current(lease); error = "restore"; publish(); throw failure; }
    }); },
  };
}
