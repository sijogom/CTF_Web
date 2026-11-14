import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const manifest = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
const lock = JSON.parse(readFileSync(new URL("../package-lock.json", import.meta.url), "utf8"));

test("locked TipTap extensions and their exact peers share the configured editor version", () => {
  const version = manifest.dependencies["@tiptap/core"];
  const packages = Object.entries(lock.packages).filter(([name]) => name.includes("node_modules/@tiptap/"));
  assert.ok(packages.length > 11);
  for (const [name, entry] of packages) {
    assert.equal(entry.version, version, `${name} must use the supported editor family`);
    for (const peer of ["@tiptap/core", "@tiptap/pm"]) {
      if (entry.peerDependencies?.[peer]) assert.equal(entry.peerDependencies[peer], version, `${name} ${peer}`);
    }
  }
});

test("optional React menus cannot float to a different exact core or pm peer", () => {
  for (const name of ["@tiptap/extension-bubble-menu", "@tiptap/extension-floating-menu"]) {
    assert.equal(manifest.overrides["@tiptap/react"][name], manifest.dependencies["@tiptap/core"]);
    assert.equal(lock.packages[`node_modules/${name}`].version, manifest.dependencies["@tiptap/core"]);
  }
});
