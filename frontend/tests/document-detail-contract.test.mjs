import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const pageSource = await readFile(
  new URL("../src/pages/DocumentDetailPage.jsx", import.meta.url),
  "utf8",
);
const pageStyles = await readFile(
  new URL("../src/pages.css", import.meta.url),
  "utf8",
);
const appContextSource = await readFile(
  new URL("../src/state/AppContext.jsx", import.meta.url),
  "utf8",
);
const previewImageSource = await readFile(
  new URL("../src/components/DocumentPreviewImage.jsx", import.meta.url),
  "utf8",
);
const htmlTableSource = await readFile(
  new URL("../src/components/DocumentHtmlTable.jsx", import.meta.url),
  "utf8",
);

test("document reader cancels stale preview requests and keeps loading errors recoverable", () => {
  assert.match(pageSource, /const controller = new AbortController\(\)/);
  assert.match(pageSource, /loadDocumentPreview[\s\S]*\{ signal \}/);
  assert.match(pageSource, /signal\?\.aborted/);
  assert.match(pageSource, /setPreviewRefreshKey/);
  assert.match(pageSource, />重试加载</);
  assert.match(pageSource, /status !== "READY"[\s\S]*setLoadingPreview\(false\)/);
  assert.doesNotMatch(pageSource, /documentError\.includes\("已"\)/);
  assert.match(pageSource, /role="alert"/);
});

test("document reader renders one continuous markdown flow with explicit chunk separators", () => {
  assert.match(pageSource, /normalizeDocumentBoundaries\(preview\?\.boundaries \|\| \[\]\)/);
  assert.match(pageSource, /createDocumentBoundaryPlugin\(readerBoundaries, preview\?\.boundary_precision\)/);
  assert.equal(pageSource.match(/<ReactMarkdown/g)?.length, 1);
  assert.match(pageSource, /remarkPlugins=\{remarkPlugins\}/);
  assert.match(pageSource, /remarkDocumentBreakTags/);
  assert.match(pageSource, /rehypePlugins=\{DOCUMENT_REHYPE_PLUGINS\}/);
  assert.match(pageSource, /content=\{renderedPreviewContent\}/);
  assert.match(pageSource, /"document-chunk-boundary"/);
  assert.match(pageSource, /<BoundaryGroup entries=\{entries\} approximate=\{approximate\}/);
  assert.match(pageSource, /role="separator"/);
  assert.match(pageSource, /readerIndex \+ 1/);
  assert.match(pageSource, /"隐藏分片线"/);
  assert.match(pageStyles, /\.document-reader__paper/);
  assert.match(pageStyles, /\.document-chunk-boundary/);
  assert.match(pageSource, /"document-page-marker"/);
  assert.match(pageSource, /第 \{pageNumber\} 页/);
  assert.match(pageStyles, /\.document-page-marker__label/);
  assert.match(pageStyles, /justify-content: flex-end/);
  assert.doesNotMatch(pageStyles, /\.document-page-marker__line/);
  assert.ok(
    pageSource.indexOf("const renderedPreviewContent = useMemo")
      < pageSource.indexOf("if (!routeIsValid)"),
    "all hooks must run before the first conditional return",
  );
  assert.match(pageSource, /mergeDocumentDetailSnapshot\(current, contextDocument\)/);
});

test("large document reader avoids repeated parsing and eagerly loading every image", () => {
  assert.match(pageSource, /const DocumentMarkdown = memo\(function DocumentMarkdown/);
  assert.match(pageSource, /const remarkPlugins = useMemo/);
  assert.match(pageSource, /document-reader__paper--boundaries-hidden/);
  assert.doesNotMatch(pageSource, /showBoundaries \? <BoundaryGroup/);
  assert.match(pageStyles, /content-visibility: auto/);
  assert.match(pageStyles, /contain-intrinsic-block-size: auto 160px/);
  assert.match(previewImageSource, /new IntersectionObserver/);
  assert.match(previewImageSource, /rootMargin: "1200px 0px"/);
  assert.match(previewImageSource, /if \(!nearViewport\)/);
  assert.match(previewImageSource, /decoding="async"/);
});

test("document reader preserves merged Word tables through a restricted renderer", () => {
  assert.match(pageSource, /"document-html-table": \(props\) => <DocumentHtmlTable/);
  assert.match(htmlTableSource, /node\.type === "html"/);
  assert.match(htmlTableSource, /node\.type = "documentHtmlTable"/);
  assert.match(htmlTableSource, /rowSpan/);
  assert.match(htmlTableSource, /colSpan/);
  assert.match(htmlTableSource, /new Set\(\[/);
  assert.doesNotMatch(htmlTableSource, /dangerouslySetInnerHTML/);
  assert.match(htmlTableSource, /<DocumentPreviewImage/);
  assert.match(pageSource, /createDocumentStructuredTablesPlugin\(tableStructures\)/);
  assert.match(pageSource, /"document-structured-table"/);
  assert.match(pageSource, /<DocumentStructuredTable structure=\{tableStructureMap\.get\(tableId\)\}/);
  assert.match(htmlTableSource, /LINKPARSE_TABLE_START/);
  assert.match(htmlTableSource, /\["rag_text", "html_fallback"\]/);
  assert.match(htmlTableSource, /rowSpan=\{safeSpan\(cell\?\.row_span\)\}/);
  assert.match(htmlTableSource, /colSpan=\{safeSpan\(cell\?\.column_span\)\}/);
});

test("document reader renders inline and block LaTeX with KaTeX", () => {
  assert.match(pageSource, /import remarkMath from "remark-math"/);
  assert.match(pageSource, /import rehypeKatex from "rehype-katex"/);
  assert.match(pageStyles, /\.document-reader-markdown \.katex-display/);
  assert.match(pageStyles, /overflow-x: auto/);
  assert.match(htmlTableSource, /remarkPlugins=\{\[remarkGfm, remarkMath, remarkDocumentBreakTags\]\}/);
  assert.match(htmlTableSource, /rehypePlugins=\{\[rehypeKatex\]\}/);
});

test("document reader keeps first-load failures actionable and honors reduced motion", () => {
  assert.match(pageSource, /!preview \? \(/);
  assert.match(pageSource, /文档内容暂时无法加载/);
  assert.match(pageSource, /preview \? <button[\s\S]*关闭加载错误/);
  assert.match(pageSource, /prefers-reduced-motion: reduce/);
  assert.match(pageStyles, /@media \(prefers-reduced-motion: reduce\)/);
});

test("document preview action supports cancellation, demo data and version consistency", () => {
  assert.match(appContextSource, /const loadDocumentPreview = useCallback/);
  assert.match(appContextSource, /getMockDocumentPreview\(id\)/);
  assert.match(appContextSource, /getDocumentPreviewContent\(id, options\)/);
  assert.match(appContextSource, /getDocumentPreviewMap\(id, options\)/);
  assert.match(appContextSource, /contentVersion !== mapVersion/);
  assert.match(appContextSource, /DOCUMENT_PREVIEW_VERSION_MISMATCH/);
  assert.match(appContextSource, /options\.signal\?\.aborted/);
  assert.match(appContextSource, /return \{ \.\.\.previewMap, content: contentResult\.content \}/);
});

test("document preview images authenticate private assets and release object URLs", () => {
  assert.match(pageSource, /img: \(props\) => <DocumentPreviewImage \{\.\.\.props\} \/>/);
  assert.match(previewImageSource, /isDocumentPreviewAssetUrl\(src\)/);
  assert.match(previewImageSource, /getDocumentPreviewAsset\(src, \{ signal: controller\.signal \}\)/);
  assert.match(previewImageSource, /URL\.createObjectURL\(blob\)/);
  assert.match(previewImageSource, /URL\.revokeObjectURL\(objectUrl\)/);
  assert.match(previewImageSource, /controller\.abort\(\)/);
  assert.match(previewImageSource, />重试</);
});
