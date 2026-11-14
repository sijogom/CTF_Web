import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { parseOffsetListPage } from "../src/lib/list-response.mjs";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../src/components/admin/knowledge/KnowledgeContentPicker.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
const tick = () => new Promise((resolve) => setImmediate(resolve));
const content = (name = "Private course", type = "course", id = "course-1") => ({ name, type, id, is_active: false });
const page = (items, offset = 0, total = items.length) => ({ items, offset, total, limit: 20 });
function elements(element) {
  if (Array.isArray(element)) return element.flatMap(elements);
  if (!element || typeof element !== "object" || !element.props) return [];
  return [element, ...elements(element.props.children)];
}

function harness() {
  const slots = [], requests = [], timers = new Map(), effects = [];
  let cursor = 0, dirty = true, timerId = 0, tree = [];
  const props = { label: "Source", type: "course", value: null, disabled: false,
    onTypeChange(type) { props.type = type; }, onChange(value) { props.value = value; } };
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!slots[index]) slots[index] = { value: typeof initial === "function" ? initial() : initial };
      return [slots[index].value, (value) => { slots[index].value = typeof value === "function" ? value(slots[index].value) : value; dirty = true; }];
    },
    useRef(initial) { const index = cursor++; return (slots[index] ??= { current: initial }); },
    useId() { cursor++; return "picker"; },
    useEffect(callback, dependencies) {
      const index = cursor++;
      if (!slots[index] || dependencies.some((value, i) => !Object.is(value, slots[index].dependencies[i]))) {
        effects.push(() => { slots[index]?.cleanup?.(); slots[index] = { dependencies, cleanup: callback() }; });
      }
    },
  };
  const context = { exports: {}, URLSearchParams, AbortController,
    document: { getElementById() { return { scrollIntoView() {} }; } },
    window: { setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; }, clearTimeout(id) { timers.delete(id); } },
    require(name) {
      if (name === "react") return react;
      if (name === "lucide-react") return { Search: "icon", X: "icon", ChevronLeft: "icon", ChevronRight: "icon" };
      if (name === "next-intl") return { useTranslations: () => (key) => key };
      if (name === "@/lib/list-response.mjs") return { parseOffsetListPage };
      if (name === "@/lib/api-client") return { apiFetch: (url, options) => new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })) };
      return require(name);
    },
  };
  vm.runInNewContext(compiled, context, { timeout: 1000 });
  function render() {
    let count = 0;
    do {
      dirty = false; cursor = 0;
      tree = elements(context.exports.KnowledgeContentPicker(props));
      const input = tree.find((item) => item.props.role === "combobox");
      if (input?.props.ref) input.props.ref.current = { focus() { input.props.onFocus(); } };
      while (effects.length) effects.shift()();
      assert(++count < 20, "render did not settle");
    } while (dirty);
    return tree;
  }
  render();
  return { props, requests, timers, render,
    input: () => tree.find((item) => item.props.role === "combobox"),
    find: (predicate) => tree.find(predicate),
    runTimers() { for (const [id, callback] of timers) { timers.delete(id); void callback(); } },
    unmount() { for (const slot of slots) slot?.cleanup?.(); },
  };
}

test("picker searches the administrator endpoint, including private content, and selects by keyboard", async () => {
  const h = harness();
  h.input().props.onFocus(); h.render(); h.runTimers();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].url, "/api/learn/admin/knowledge-contents?type=course&q=&offset=0&limit=20");
  h.requests[0].resolve(page([content()])); await tick(); h.render();
  const prevented = [];
  h.input().props.onKeyDown({ key: "ArrowDown", preventDefault() { prevented.push(true); } }); h.render();
  assert.equal(h.input().props["aria-activedescendant"], "picker-item-0");
  h.input().props.onKeyDown({ key: "Enter", preventDefault() { prevented.push(true); } }); h.render();
  assert.equal(h.props.value.name, "Private course");
  assert.equal(h.props.value.is_active, false);
  assert.equal(h.input().props["aria-expanded"], false);
  assert.equal(prevented.length, 2);
});

test("query changes abort the old request and ignore a reversed late response", async () => {
  const h = harness();
  h.input().props.onFocus(); h.render(); h.runTimers();
  h.input().props.onChange({ target: { value: "new" } }); h.render(); h.runTimers();
  assert.equal(h.requests[0].options.signal.aborted, true);
  h.requests[1].resolve(page([content("New match")])); await tick(); h.render();
  h.requests[0].resolve(page([content("Stale match")])); await tick();
  const tree = h.render();
  assert(tree.some((item) => item.type === "span" && item.props.children === "New match"));
  assert(!tree.some((item) => item.type === "span" && item.props.children === "Stale match"));
});

test("type changes reset the selected ID and prevent old-type results from being selected", async () => {
  const h = harness();
  h.input().props.onFocus(); h.render(); h.runTimers();
  h.props.value = content("Existing choice"); h.render();
  h.find((item) => item.type === "select").props.onChange({ target: { value: "challenge" } });
  assert.equal(h.props.value, null); h.render(); h.runTimers();
  assert.equal(h.requests[0].options.signal.aborted, true);
  h.requests[0].resolve(page([content("Old course")]));
  h.requests[1].resolve(page([content("Challenge", "challenge", "123")])); await tick(); h.render();
  h.input().props.onKeyDown({ key: "ArrowDown", preventDefault() {} }); h.render();
  h.input().props.onKeyDown({ key: "Enter", preventDefault() {} });
  assert.equal(h.props.value.type, "challenge");
  assert.equal(h.props.value.id, "123");
});

test("failure is distinct from empty results and supports explicit retry", async () => {
  const h = harness();
  h.input().props.onFocus(); h.render(); h.runTimers();
  h.requests[0].reject(new Error("network")); await tick(); h.render();
  assert(h.find((item) => item.props.role === "alert"));
  assert(!h.find((item) => item.type === "p" && item.props.children === "searchEmpty"));
  h.find((item) => item.type === "button" && item.props.children === "retry").props.onClick(); h.render(); h.runTimers();
  h.requests[1].resolve(page([])); await tick(); h.render();
  assert(!h.find((item) => item.props.role === "alert"));
  assert(h.find((item) => item.type === "p" && item.props.children === "searchEmpty"));
});

test("pagination keeps the search query and Escape and unmount cancel outstanding work", async () => {
  const h = harness();
  h.input().props.onChange({ target: { value: "SQL" } }); h.render(); h.runTimers();
  h.requests[0].resolve(page([content()], 0, 21)); await tick(); h.render();
  h.find((item) => item.props["aria-label"] === "nextResults").props.onClick(); h.render(); h.runTimers();
  assert(h.requests[1].url.includes("q=SQL&offset=20"));
  h.input().props.onKeyDown({ key: "Escape", preventDefault() {} }); h.render();
  assert.equal(h.requests[1].options.signal.aborted, true);
  assert.equal(h.input().props["aria-expanded"], false);
  h.input().props.onFocus(); h.render(); h.runTimers(); h.unmount();
  assert.equal(h.requests[2].options.signal.aborted, true);
});

test("approved relation selections stay visible without exposing an editable ID input", () => {
  const h = harness();
  h.props.disabled = true; h.props.value = content("Approved course");
  const tree = h.render();
  assert(tree.some((item) => item.type === "p" && item.props.children === "Approved course"));
  assert(!tree.some((item) => item.type === "input"));
  assert.equal(h.requests.length, 0);
});

test("duplicate titles have numeric or compact UUID identifiers in both results and selections", async () => {
  const h = harness(), uuid = "953ec08a-7958-4d3d-97ce-89345de81572";
  h.input().props.onFocus(); h.render(); h.runTimers();
  h.requests[0].resolve(page([content("Same title", "course", "123456"), content("Same title", "course", uuid)]));
  await tick(); h.render();
  const numeric = h.find((item) => item.props.title === "123456");
  const compact = h.find((item) => item.props.title === uuid);
  assert.equal(numeric.props.children, "123456");
  assert.equal(compact.props.children, "953ec08a...1572");
  assert.equal(compact.props["aria-label"], `contentId: ${uuid}`);
  h.input().props.onKeyDown({ key: "ArrowDown", preventDefault() {} }); h.render();
  h.input().props.onKeyDown({ key: "ArrowDown", preventDefault() {} }); h.render();
  h.input().props.onKeyDown({ key: "Enter", preventDefault() {} }); h.render();
  assert.equal(h.props.value.id, uuid);
  assert.equal(h.find((item) => item.props.title === uuid).props.children, "953ec08a...1572");
  assert(h.find((item) => item.type === "p" && item.props.children === "Same title"));
});

test("mouse selection stays closed when restoring input focus emits onFocus", async () => {
  const h = harness();
  h.input().props.onFocus(); h.render(); h.runTimers();
  h.requests[0].resolve(page([content("Mouse selection")])); await tick(); h.render();
  const result = h.find((item) => item.type === "button" && item.props.className?.includes("text-left"));
  result.props.onClick(); h.render(); h.runTimers();
  assert.equal(h.props.value.name, "Mouse selection");
  assert.equal(h.input().props["aria-expanded"], false);
  assert.equal(h.requests.length, 1, "focus restoration must not start another result query");
});
