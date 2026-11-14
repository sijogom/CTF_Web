import assert from "node:assert/strict";
import test from "node:test";

import { classifyRichContent, legacyRichContentToHtml } from "../src/lib/rich-content.mjs";
import {
  createRichContentStructuralExtensions,
  requiresRichContentEditorReview,
  RICH_CONTENT_PARSE_OPTIONS,
} from "../src/lib/rich-content-editor.mjs";

test("editor review gate catches structure and attribute loss before conversion", () => {
  assert.equal(requiresRichContentEditorReview('<p style="color:red">body</p>'), true);
  assert.equal(requiresRichContentEditorReview('<div><p>body</p></div>'), true);
  assert.equal(requiresRichContentEditorReview('<p class="legacy">body</p>'), true);
  assert.equal(requiresRichContentEditorReview('<p>body</p>'), false);
  assert.equal(requiresRichContentEditorReview('<pre><code class="language-c++">code</code></pre>'), false);
  assert.equal(requiresRichContentEditorReview('<pre><code class="language-c#">code</code></pre>'), false);
  assert.equal(requiresRichContentEditorReview('<ol start="3"><li value="5">five</li></ol>'), false);
  assert.equal(requiresRichContentEditorReview('<table><tbody><tr><th align="right" scope="col">value</th></tr></tbody></table>'), false);
  assert.equal(requiresRichContentEditorReview('<table><tbody><tr><td rowspan="2">broken</td></tr></tbody></table>'), true);
  assert.equal(requiresRichContentEditorReview('<table><tbody><tr><td rowspan="2">valid</td><td>one</td></tr><tr><td>two</td></tr></tbody></table>'), false);
  assert.equal(requiresRichContentEditorReview('<table><tbody><tr><td>default span</td></tr></tbody></table>'), false);
  assert.equal(requiresRichContentEditorReview('<table><tbody><tr><td rowspan="0">invalid zero</td></tr></tbody></table>'), true);
  assert.equal(requiresRichContentEditorReview('<table><thead><tr><th>heading</th></tr></thead><tbody><tr><td>body</td></tr></tbody></table>'), true);
  assert.equal(requiresRichContentEditorReview('<table><tbody><tr><td>wide</td><td>row</td></tr><tr><td>ragged</td></tr></tbody></table>'), true);
  assert.equal(requiresRichContentEditorReview('<table><tbody><tr><td rowspan="2">span</td><td>row</td></tr><tr><td colspan="2">overlap</td></tr></tbody></table>'), true);
  assert.equal(requiresRichContentEditorReview('<table><tbody><tr><td>A</td><td rowspan="2">B</td><td>C</td><td>D</td></tr><tr><td colspan="2">E</td></tr></tbody></table>'), true);
  assert.equal(requiresRichContentEditorReview('<table><tr><td>implicit tbody</td></tr></table>'), false);
});

async function installEditorDom() {
  // isomorphic-dompurify already supplies this runtime dependency in the
  // application lock; use a fresh document to exercise ProseMirror parsing.
  const { JSDOM } = await import("jsdom");
  const browserWindow = new JSDOM("<!doctype html><html><body></body></html>").window;
  globalThis.window = browserWindow;
  globalThis.document = browserWindow.document;
  globalThis.Node = browserWindow.Node;
  globalThis.HTMLElement = browserWindow.HTMLElement;
  globalThis.getComputedStyle = browserWindow.getComputedStyle.bind(browserWindow);
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: browserWindow.navigator,
  });
}

test("TipTap round-trip keeps plain spacing, code whitespace, table spans, and alignment", async () => {
  await installEditorDom();
  const [{ Editor }, { default: StarterKit }, { default: CodeBlockLowlight }, { common, createLowlight }] = await Promise.all([
    import("@tiptap/core"),
    import("@tiptap/starter-kit"),
    import("@tiptap/extension-code-block-lowlight"),
    import("lowlight"),
  ]);
  const lowlight = createLowlight(common);

  const original = '<p>a   b<br>c  d</p><hr><ol start="3"><li value="5"><p>five</p></li></ol><pre><code>  x  \n y</code></pre><pre><code class="language-c++">  int x;  \n</code></pre><pre><code class="language-c#">  var x = 1;  \n</code></pre><table><tbody><tr><th align="right" colspan="2" rowspan="1" scope="col"><p>value</p></th><td rowspan="2"><p>span</p></td></tr><tr><th><p>next</p></th><th><p>row</p></th></tr></tbody></table>';
  let updateCount = 0;
  const editor = new Editor({
    extensions: [
      StarterKit.configure({ codeBlock: false, listItem: false, link: false, underline: false, listKeymap: false, trailingNode: false }),
      CodeBlockLowlight.configure({ lowlight }),
      ...createRichContentStructuralExtensions(),
    ],
    content: legacyRichContentToHtml(original),
    parseOptions: RICH_CONTENT_PARSE_OPTIONS,
    onUpdate: () => { updateCount += 1; },
  });

  const serialized = editor.getHTML();
  assert.equal(classifyRichContent(serialized), "html");
  assert.equal(requiresRichContentEditorReview(serialized), false);
  assert.doesNotMatch(serialized, /<colgroup|\sstyle=/);
  assert.match(serialized, /<p>a   b<br>c  d<\/p>/);
  assert.match(serialized, /<hr><ol start="3"><li value="5"><p>five<\/p><\/li><\/ol>/);
  assert.match(serialized, /<pre><code>  x  \n y<\/code><\/pre>/);
  assert.match(serialized, /<pre><code class="language-c\+\+">  int x;  \n<\/code><\/pre>/);
  assert.match(serialized, /<pre><code class="language-c#">  var x = 1;  \n<\/code><\/pre>/);
  assert.match(serialized, /<th colspan="2" rowspan="1" align="right" scope="col"><p>value<\/p><\/th>/);
  assert.match(serialized, /<td colspan="1" rowspan="2"><p>span<\/p><\/td>/);
  assert.match(serialized, /<\/table>$/, "opening a document must not append a new trailing paragraph");
  assert.equal(updateCount, 0);

  editor.commands.setContent(legacyRichContentToHtml(original), { emitUpdate: false, parseOptions: RICH_CONTENT_PARSE_OPTIONS });
  assert.equal(updateCount, 0, "opening and saving another field must not synthesize a body edit");

  editor.commands.insertContentAt(1, "first ");
  const firstControlledUpdate = editor.getHTML();
  assert.equal(classifyRichContent(firstControlledUpdate), "html");
  assert.equal(requiresRichContentEditorReview(firstControlledUpdate), false);
  assert.doesNotMatch(firstControlledUpdate, /<colgroup|\sstyle=/);
  assert.match(firstControlledUpdate, /<td colspan="1" rowspan="2"><p>span<\/p><\/td>/);

  editor.commands.setContent(firstControlledUpdate, { emitUpdate: false, parseOptions: RICH_CONTENT_PARSE_OPTIONS });
  editor.commands.insertContentAt(1, "second ");
  const secondControlledUpdate = editor.getHTML();
  assert.equal(classifyRichContent(secondControlledUpdate), "html");
  assert.equal(requiresRichContentEditorReview(secondControlledUpdate), false);
  assert.doesNotMatch(secondControlledUpdate, /<colgroup|\sstyle=/);
  assert.match(secondControlledUpdate, /<pre><code class="language-c\+\+">  int x;  \n<\/code><\/pre>/);
  assert.match(secondControlledUpdate, /<td colspan="1" rowspan="2"><p>span<\/p><\/td>/);
  const beforeJsonRoundTrip = updateCount;
  editor.commands.setContent(editor.getJSON(), { emitUpdate: false, parseOptions: RICH_CONTENT_PARSE_OPTIONS });
  assert.equal(editor.getHTML(), secondControlledUpdate, "the internal JSON schema round-trip must preserve the stored HTML contract");
  assert.equal(updateCount, beforeJsonRoundTrip, "JSON input must also obey explicit update suppression");
  editor.destroy();
});

test("TipTap keeps configured link underline and original image source through controlled HTML updates", async () => {
  await installEditorDom();
  const [{ Editor }, { default: StarterKit }, { default: Link }, { default: Underline }, { default: Image }] = await Promise.all([
    import("@tiptap/core"), import("@tiptap/starter-kit"), import("@tiptap/extension-link"), import("@tiptap/extension-underline"), import("@tiptap/extension-image"),
  ]);
  let updates = 0;
  const editor = new Editor({
    extensions: [
      StarterKit.configure({ codeBlock: false, listItem: false, link: false, underline: false, listKeymap: false, trailingNode: false }),
      Link.configure({ openOnClick: false, HTMLAttributes: { class: "text-accent-primary underline" } }), Underline, Image,
      ...createRichContentStructuralExtensions(),
    ],
    content: '<p><a href="/learn/owned-course">owned link</a> <u>underlined text</u></p><img src="/api/learn/files/original.png" alt="original alt">',
    parseOptions: RICH_CONTENT_PARSE_OPTIONS,
    onUpdate: () => { updates += 1; },
  });
  const html = editor.getHTML();
  assert.match(html, /href="\/learn\/owned-course"/);
  assert.match(html, /<u>underlined text<\/u>/);
  assert.match(html, /src="\/api\/learn\/files\/original\.png"/);
  assert.match(html, /alt="original alt"/);
  assert.doesNotMatch(html, /src="\/ctf\//, "display-only base path rewriting must not alter the stored source");
  assert.match(html, /<img[^>]*>$/, "the image must not acquire a synthetic trailing paragraph");
  editor.commands.setContent(html, { emitUpdate: false, parseOptions: RICH_CONTENT_PARSE_OPTIONS });
  assert.equal(editor.getHTML(), html);
  assert.equal(updates, 0);
  editor.destroy();
});
