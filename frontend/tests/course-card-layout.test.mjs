import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import { isValidElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const source = readFileSync(new URL("../src/components/learn/CourseCard.tsx", import.meta.url), "utf8");
const require = createRequire(import.meta.url);
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const context = { exports: {}, require: (name) => {
  if (name === "@/i18n/navigation") return { Link: "a" };
  if (name === "@/components/ui/CategoryIcon") return { CategoryIcon: () => null };
  if (name === "next-intl") return { useTranslations: () => (key, options) => `${key}${options ? `:${options.count ?? options.n}` : ""}` };
  return require(name);
} };
vm.runInNewContext(compiled, context, { timeout: 1000 });

function elements(element) {
  if (Array.isArray(element)) return element.flatMap(elements);
  if (!isValidElement(element)) return [];
  return [element, ...elements(element.props.children)];
}

function card(title) {
  return context.exports.CourseCard({ course: { id: "owned-course", title, category: "web", difficulty: "introductory", estimated_hours: 4, lesson_count: 1, progress_percent: 0 } });
}

for (const [kind, title] of [
  ["unbroken ASCII", `legacy_education_management_${"0123456789_".repeat(12)}`],
  ["unbroken Korean", "긴한글무공백강의제목원문을생략하지않고줄바꿈해서읽습니다".repeat(6)],
]) {
  test(`${kind} course title retains every original character with container-safe wrapping`, () => {
    const rendered = card(title);
    const heading = elements(rendered).find((element) => element.type === "h3");
    assert.equal(heading.props.children, title);
    const classes = heading.props.className.split(" ");
    assert(classes.includes("min-w-0"));
    assert(classes.includes("[overflow-wrap:anywhere]"));
    assert(!classes.some((token) => token === "truncate" || token.startsWith("line-clamp-") || token === "overflow-hidden"));
    assert(renderToStaticMarkup(rendered).includes(title));
  });
}

test("the actual card can shrink in its grid while its course route and metadata stay intact", () => {
  const rendered = card("owned title");
  assert.equal(rendered.type, "a");
  assert.equal(rendered.props.href, "/learn/owned-course");
  assert(rendered.props.className.split(" ").includes("min-w-0"));
  const html = renderToStaticMarkup(rendered);
  assert(html.includes("lessonCount:1"));
  assert(html.includes("estimatedHours:4"));
});
