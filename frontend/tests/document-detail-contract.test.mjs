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
  assert.match(pageSource, /<ReactMarkdown remarkPlugins=\{\[remarkGfm, boundaryPlugin\]\}[\s\S]*\{preview\.content\}<\/ReactMarkdown>/);
  assert.match(pageSource, /"document-chunk-boundary"/);
  assert.match(pageSource, /<BoundaryGroup entries=\{entries\} approximate=\{approximate\}/);
  assert.match(pageSource, /role="separator"/);
  assert.match(pageSource, /readerIndex \+ 1/);
  assert.match(pageSource, /"隐藏分片线"/);
  assert.match(pageStyles, /\.document-reader__paper/);
  assert.match(pageStyles, /\.document-chunk-boundary/);
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
