import assert from "node:assert/strict";
import test from "node:test";
import { mergeAttributes } from "@tiptap/core";
import { DOMSerializer, Schema } from "@tiptap/pm/model";
import { JSDOM } from "jsdom";

test("JSON-origin own __proto__ cannot become inherited executable DOM attributes", () => {
  const untrusted = JSON.parse('{"__proto__":{"data-inherited-canary":"present","src":"x-invalid://canary","onerror":"globalThis.__tiptapSecurityCanary = true"}}');
  const attributes = mergeAttributes({ alt: "safe original text" }, untrusted);
  assert.equal(Object.getPrototypeOf(attributes), Object.prototype);
  assert.equal(Object.hasOwn(attributes, "__proto__"), true);
  assert.equal(attributes["data-inherited-canary"], undefined);
  assert.equal(attributes.src, undefined);
  assert.equal(attributes.onerror, undefined);
  assert.equal(Object.prototype["data-inherited-canary"], undefined);

  const schema = new Schema({ nodes: {
    doc: { content: "image" }, image: { toDOM: () => ["img", attributes] }, text: {},
  } });
  const window = new JSDOM("<!doctype html><html><body></body></html>").window;
  try {
    const fragment = DOMSerializer.fromSchema(schema).serializeFragment(schema.node("doc", null, [schema.node("image")]).content, { document: window.document });
    const image = fragment.firstChild;
    assert.equal(image.getAttribute("alt"), "safe original text");
    assert.equal(image.getAttribute("data-inherited-canary"), null);
    assert.equal(image.getAttribute("src"), null);
    assert.equal(image.getAttribute("onerror"), null);
    assert.equal(window.__tiptapSecurityCanary, undefined);
  } finally {
    window.close();
  }
});
