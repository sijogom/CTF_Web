import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import postcss from "postcss";

const css = postcss.parse(readFileSync(new URL("../src/app/globals.css", import.meta.url), "utf8"));

test("normal text preserves Korean word boundaries and wraps oversized tokens without negative spacing", () => {
  const bodies = [];
  css.walkRules("body", (rule) => bodies.push(rule));
  assert.equal(bodies.length, 1);
  const declarations = Object.fromEntries(bodies[0].nodes.filter((node) => node.type === "decl").map((node) => [node.prop, node.value]));
  assert.equal(declarations["word-break"], "keep-all");
  assert.equal(declarations["overflow-wrap"], "anywhere");
  assert.equal(declarations["letter-spacing"], "0");
  assert.equal(declarations["white-space"], undefined);
});

test("code whitespace is not replaced by a global wrapping or clipping rule", () => {
  css.walkRules((rule) => {
    if (!/(?:^|[\s,])(?:pre|code)(?:$|[\s,.:[#])/u.test(rule.selector)) return;
    for (const declaration of rule.nodes.filter((node) => node.type === "decl")) {
      if (declaration.prop === "white-space") assert.notEqual(declaration.value, "normal");
      if (declaration.prop === "text-overflow") assert.notEqual(declaration.value, "ellipsis");
    }
  });
});
