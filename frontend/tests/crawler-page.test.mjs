import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const crawlerReviewPageSource = await readFile(
  new URL("../src/pages/CrawlerReviewPage.jsx", import.meta.url),
  "utf8",
);
const appShellSource = await readFile(
  new URL("../src/components/AppShell.jsx", import.meta.url),
  "utf8",
);
const appSource = await readFile(
  new URL("../src/App.jsx", import.meta.url),
  "utf8",
);
const pageStyles = await readFile(
  new URL("../src/pages.css", import.meta.url),
  "utf8",
);
const uploadDialogSource = await readFile(
  new URL("../src/components/UploadDialog.jsx", import.meta.url),
  "utf8",
);

test("crawler review gates parsing behind an explicit approval action", () => {
  assert.match(crawlerReviewPageSource, /listCrawlerSubmissions/);
  assert.match(crawlerReviewPageSource, /reviewCrawlerSubmission/);
  assert.match(crawlerReviewPageSource, /decision === "APPROVED"/);
  assert.match(crawlerReviewPageSource, /通过并解析/);
  assert.match(crawlerReviewPageSource, /审核预览/);
});

test("review preview renders PDF and Markdown without parsing Word before approval", () => {
  assert.match(crawlerReviewPageSource, /submissionPreviewKind/);
  assert.match(crawlerReviewPageSource, /preview\.kind === "pdf"/);
  assert.match(crawlerReviewPageSource, /<ReactMarkdown/);
  assert.match(crawlerReviewPageSource, /Word 原文件需下载后审核/);
  assert.match(crawlerReviewPageSource, /系统不会提前把 Word 转换为 HTML/);
  assert.match(pageStyles, /\.crawler-preview-dialog__pdf/);
  assert.match(pageStyles, /\.crawler-preview-dialog__markdown/);
});

test("manual upload selector includes Markdown files", () => {
  assert.match(uploadDialogSource, /'md', 'markdown'/);
  assert.match(uploadDialogSource, /PDF、Word、Markdown、HTML/);
});

test("document review remains the only crawler navigation entry", () => {
  assert.match(appShellSource, /to: "\/crawler\/review", label: "资料审核"/);
  assert.doesNotMatch(appShellSource, /to: "\/crawler"[^/]/);
  assert.match(appSource, /path="crawler\/review"/);
  assert.doesNotMatch(appSource, /path="crawler"/);
});

test("crawler review filter and refresh action stay in one row", () => {
  assert.match(crawlerReviewPageSource, /crawler-results__actions crawler-review__actions/);
  assert.match(pageStyles, /\.crawler-review__actions \{[^}]*flex-wrap: nowrap;/);
  assert.match(pageStyles, /\.crawler-review__actions \.button \{[^}]*white-space: nowrap;/);
});
