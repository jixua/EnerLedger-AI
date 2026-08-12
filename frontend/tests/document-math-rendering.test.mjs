import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";
import { normalizeDocumentMath } from "../src/lib/document-math.js";

test("Word inline and block LaTeX produce KaTeX markup", () => {
  const html = renderToStaticMarkup(createElement(
    ReactMarkdown,
    { remarkPlugins: [remarkMath], rehypePlugins: [rehypeKatex] },
    normalizeDocumentMath("行内公式 $E=mc^2$\n\n$$\\sum_{i=1}^{n} i$$"),
  ));

  assert.match(html, /class="katex"/);
  assert.match(html, /class="katex-display"/);
  assert.doesNotMatch(html, /\$E=mc\^2\$/);
});
