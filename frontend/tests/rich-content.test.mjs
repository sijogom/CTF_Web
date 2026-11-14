import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  classifyRichContent,
  isRichContentEmpty,
  legacyRichContentToHtml,
  richContentHtmlForDisplay,
  richContentImageSrcForDisplay,
  richContentToPlainText,
  richContentVisibleLength,
  sanitizeRichHtml,
} from "../src/lib/rich-content.mjs";

test("classifies canonical, legacy, plain, and review-required content", () => {
  assert.equal(classifyRichContent("<p>ready</p>"), "html");
  assert.equal(classifyRichContent("**ready**"), "markdown");
  assert.equal(classifyRichContent("ready"), "plain");
  assert.equal(classifyRichContent("<p>ready</p>\n\n**legacy**"), "ambiguous");
  assert.equal(classifyRichContent("<p>unclosed"), "ambiguous");
  assert.equal(classifyRichContent('<p style="color:red">lossy</p>'), "ambiguous");
  assert.equal(classifyRichContent('<p id="legacy">lossy</p>'), "ambiguous");
  assert.equal(classifyRichContent("<figure><p>unsupported wrapper</p></figure>"), "ambiguous");
  assert.equal(classifyRichContent('<p class="first" class="second">duplicate</p>'), "ambiguous");
  assert.equal(classifyRichContent('<pre><code class="language-c++">code</code></pre>'), "html");
  assert.equal(classifyRichContent('<img src="/path with space/image.png">'), "ambiguous");
  assert.equal(classifyRichContent("<table><tbody><tr><td>x</td></tr></tbody><p>outside</p></table>"), "ambiguous");
  assert.equal(classifyRichContent("<p><strong>one<div>two</div></strong></p>"), "ambiguous");
  assert.equal(classifyRichContent("<table><tr><td>x</td></tr></table>"), "html");
});

test("renders CommonMark structures and tables as canonical HTML", () => {
  const html = legacyRichContentToHtml([
    "# Heading",
    "",
    "> Quote with **bold**",
    "",
    "| name | value |",
    "| --- | ---: |",
    "| alpha | 1 |",
  ].join("\n"));

  assert.match(html, /<h1>Heading<\/h1>/);
  assert.match(html, /<blockquote>/);
  assert.match(html, /<strong>bold<\/strong>/);
  assert.match(html, /<table>/);
  assert.match(html, /<th>name<\/th>/);
  assert.match(html, /<th align="right">value<\/th>/);
  assert.match(html, /<td>alpha<\/td>/);
});

test("preserves fenced and indented code whitespace while escaping markup", () => {
  const fenced = legacyRichContentToHtml("```html\n  <tag>  \n```\n");
  assert.match(fenced, /<pre><code class="language-html">  &lt;tag&gt;  \n<\/code><\/pre>/);

  const indented = legacyRichContentToHtml("    first  \n      second\n");
  assert.match(indented, /<pre><code>first  \n  second\n<\/code><\/pre>/);

  assert.equal(
    legacyRichContentToHtml("```c++\nstd::cout << value;\n```") ,
    '<pre><code class="language-c++">std::cout &lt;&lt; value;\n</code></pre>',
  );
  assert.equal(
    legacyRichContentToHtml("```c#\nConsole.WriteLine(value);\n```") ,
    '<pre><code class="language-c#">Console.WriteLine(value);\n</code></pre>',
  );
});

test("keeps supported link and image references and strips executable URLs", () => {
  const html = sanitizeRichHtml([
    '<p><a href="#section">anchor</a>',
    '<a href="../lesson/2">relative</a>',
    '<a href="https://example.test/a" target="_blank" rel="nofollow">web</a>',
    '<a href="mailto:teacher@example.test">mail</a>',
    '<a href="javascript:alert(1)">bad</a></p>',
    '<img src="/api/learn/assets/exact-name.png?token=abc" alt="diagram">',
    '<img src="data:image/png;base64,AAAA" alt="bad image">',
  ].join(""));

  assert.match(html, /href="#section"/);
  assert.match(html, /href="\.\.\/lesson\/2"/);
  assert.match(html, /href="https:\/\/example\.test\/a" target="_blank" rel="nofollow noopener noreferrer"/);
  assert.match(html, /href="mailto:teacher@example\.test"/);
  assert.doesNotMatch(html, /javascript:/i);
  assert.match(html, /src="\/api\/learn\/assets\/exact-name\.png\?token=abc"/);
  assert.doesNotMatch(html, /data:image/i);
});

test("prefixes only app-owned image routes for display without changing stored HTML", () => {
  assert.equal(richContentImageSrcForDisplay("/api/learn/lesson-images/x.png", "/ctf"), "/ctf/api/learn/lesson-images/x.png");
  assert.equal(richContentImageSrcForDisplay("/storage/banners/x.png", "/ctf/"), "/ctf/storage/banners/x.png");
  assert.equal(richContentImageSrcForDisplay("/ctf/api/learn/lesson-images/x.png", "/ctf"), "/ctf/api/learn/lesson-images/x.png");
  assert.equal(richContentImageSrcForDisplay("https://cdn.example.test/x.png", "/ctf"), "https://cdn.example.test/x.png");
  assert.equal(richContentImageSrcForDisplay("images/x.png", "/ctf"), "images/x.png");

  const stored = '<p><img src="/api/learn/lesson-images/x.png" alt="fixture"></p>';
  assert.equal(
    richContentHtmlForDisplay(stored, "/ctf"),
    '<p><img src="/ctf/api/learn/lesson-images/x.png" alt="fixture"></p>',
  );
  assert.equal(legacyRichContentToHtml(stored), stored);
});

test("preserves bounded table spans and removes dangerous markup", () => {
  const html = sanitizeRichHtml('<table onclick="bad()"><tbody><tr><td colspan="2" rowspan="3" style="color:red">ok</td><td colspan="101">wide</td></tr></tbody></table><script>alert(1)</script>');
  assert.match(html, /<table>/);
  assert.match(html, /colspan="2" rowspan="3"/);
  assert.doesNotMatch(html, /colspan="101"/);
  assert.doesNotMatch(html, /onclick|style=|script|alert/i);
});

test("preserves bounded list and header semantics while rejecting invalid values", () => {
  const html = sanitizeRichHtml('<hr><ol start="3"><li value="5">five</li></ol><table><thead><tr><th scope="col">name</th><th scope="invalid">bad</th></tr></thead></table>');
  assert.match(html, /^<hr><ol start="3"><li value="5">five<\/li><\/ol>/);
  assert.match(html, /<th scope="col">name<\/th>/);
  assert.match(html, /<th>bad<\/th>/);
});

test("extracts visible text and treats only usable images as content", () => {
  assert.equal(richContentToPlainText("<p>Hello <strong>world</strong></p>", 7), "Hello w");
  assert.equal(isRichContentEmpty("<p><br></p>"), true);
  assert.equal(isRichContentEmpty('<img src="/images/diagram.png" alt="">'), false);
  assert.equal(classifyRichContent('<img src="data:image/png;base64,AAAA" alt="">'), "ambiguous");
  assert.equal(isRichContentEmpty('<img src="data:image/png;base64,AAAA" alt="">'), false);
});

test("visible character limits use Unicode code points rather than HTML bytes", () => {
  const text = richContentToPlainText("<p>한글😀A</p>");
  assert.equal(Array.from(text).length, 4);
  assert.equal(richContentToPlainText("<p>한글😀A</p>", 3), "한글😀");
  assert.ok(new TextEncoder().encode("<p>한글😀A</p>").length > Array.from(text).length);
});

test("plain extraction keeps structure while visible counts exclude separators", () => {
  assert.equal(richContentToPlainText("<p>a</p><p>b</p>"), "a\nb");
  assert.equal(richContentVisibleLength("<p>a</p><p>b</p>"), 2);
  assert.equal(richContentToPlainText("<p>a<br>b</p>"), "a\nb");
  assert.equal(richContentVisibleLength("<p>a<br>b</p>"), 2);
  const table = "<table><tbody><tr><td>a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr></tbody></table>";
  assert.equal(richContentToPlainText(table), "a\tb\n1\t2");
  assert.equal(richContentVisibleLength(table), 4);
  assert.equal(richContentToPlainText('<p><img src="/x" alt="그림😀"></p>'), "그림😀");
  assert.equal(richContentVisibleLength('<p><img src="/x" alt="그림😀"></p>'), 3);
  assert.equal(richContentToPlainText("<p>a   b</p>"), "a b");
  assert.equal(richContentVisibleLength("<p>a   b</p>"), 5);
});

test("canonical HTML remains stable for display/edit initialization", () => {
  const original = '<table><tbody><tr><td colspan="2"><pre><code>  exact  \n</code></pre></td></tr></tbody></table>';
  assert.equal(legacyRichContentToHtml(original), original);
});

test("ambiguous CTF text renders without deleting angle-bracket content", () => {
  const include = legacyRichContentToHtml("#include <stdio.h>");
  const flag = legacyRichContentToHtml("문자열 <FLAG> 예제");
  assert.equal(classifyRichContent("#include <stdio.h>"), "ambiguous");
  assert.equal(classifyRichContent("문자열 <FLAG> 예제"), "ambiguous");
  assert.match(include, /#include &lt;stdio\.h&gt;/);
  assert.match(flag, /문자열 &lt;FLAG&gt; 예제/);
  assert.equal(richContentToPlainText("#include <stdio.h>"), "#include <stdio.h>");
  assert.equal(richContentToPlainText("문자열 <FLAG> 예제"), "문자열 <FLAG> 예제");
});

test("matches the shared backend rich-content contract vectors", async () => {
  const contractUrl = new URL("../../services/rich_content_contract_vectors.json", import.meta.url);
  const vectors = JSON.parse(await readFile(contractUrl, "utf8"));

  for (const vector of vectors.normalize) {
    assert.equal(classifyRichContent(vector.input), vector.kind, `${vector.name}: classification`);
    if (vector.allow_empty) {
      assert.equal(isRichContentEmpty(vector.input), true, `${vector.name}: empty policy`);
    } else {
      assert.equal(legacyRichContentToHtml(vector.input), vector.expected_html, `${vector.name}: canonical HTML`);
    }
  }

  for (const vector of vectors.reject) {
    assert.equal(classifyRichContent(vector.input), "ambiguous", `${vector.name}: review gate`);
  }

  for (const vector of vectors.plain_text) {
    assert.equal(richContentToPlainText(vector.input), vector.expected, `${vector.input}: plain text`);
    assert.equal(richContentVisibleLength(vector.input), vector.visible_code_points, `${vector.input}: visible length`);
  }
});
