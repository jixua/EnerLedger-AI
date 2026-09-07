import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const crawlerPageSource = await readFile(
  new URL("../src/pages/CrawlerPage.jsx", import.meta.url),
  "utf8",
);
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

test("crawler requires a dataset and places the query before compact selectors", () => {
  const datasetSelector = crawlerPageSource.indexOf('className="crawler-search__dataset"');
  const resultLimit = crawlerPageSource.indexOf('className="crawler-search__limit"');
  const queryInput = crawlerPageSource.indexOf('className="crawler-search__input"');

  assert.ok(queryInput >= 0);
  assert.ok(datasetSelector > queryInput);
  assert.ok(resultLimit > datasetSelector);
  assert.match(
    crawlerPageSource,
    /disabled=\{!datasetId \|\| loading \|\| query\.trim\(\)\.length < 2\}/,
  );
  assert.match(
    crawlerPageSource,
    /if \(!datasetId \|\| normalized\.length < 2 \|\| loading\) return/,
  );
});

test("crawler always enables AI optimization without showing a toggle or success card", () => {
  assert.match(crawlerPageSource, /aiOptimize: true/);
  assert.doesNotMatch(
    crawlerPageSource,
    /setAiOptimize|crawler-search__ai|crawler-search__note/,
  );
  assert.doesNotMatch(crawlerPageSource, /AI 优化检索|规则优化检索/);
  assert.match(crawlerPageSource, /result\?\.optimization_warning/);
});

test("search results remain bound to the selected dataset", () => {
  assert.match(crawlerPageSource, /function handleDatasetChange/);
  assert.match(crawlerPageSource, /setResult\(null\)/);
  assert.match(crawlerPageSource, /setSelectedIds\(\[\]\)/);
  assert.match(crawlerPageSource, /crawler-import__target/);
  assert.doesNotMatch(crawlerPageSource, /setDatasetId\(""\)/);
  assert.doesNotMatch(crawlerPageSource, /aria-label="目标数据集"/);
});

test("crawler unlocks import before background document refresh", () => {
  const unlockIndex = crawlerPageSource.indexOf("setImporting(false);");
  const refreshIndex = crawlerPageSource.indexOf(
    "Promise.resolve(actions.loadDocuments?.(targetDatasetId))",
  );

  assert.ok(unlockIndex >= 0);
  assert.ok(refreshIndex > unlockIndex);
});

test("crawler review gates parsing behind an explicit approval action", () => {
  assert.match(crawlerReviewPageSource, /listCrawlerSubmissions/);
  assert.match(crawlerReviewPageSource, /reviewCrawlerSubmission/);
  assert.match(crawlerReviewPageSource, /decision === "APPROVED"/);
  assert.match(crawlerReviewPageSource, /通过并解析/);
  assert.match(crawlerReviewPageSource, /查看原文件/);
  assert.match(crawlerReviewPageSource, />目标数据集</);
  assert.match(crawlerReviewPageSource, /datasetId: targetDatasetId/);
});

test("arXiv collection and crawler review have independent routes and navigation", () => {
  assert.doesNotMatch(crawlerPageSource, /listCrawlerSubmissions/);
  assert.doesNotMatch(crawlerReviewPageSource, /searchArxivPapers/);
  assert.match(appShellSource, /to: "\/crawler\/review", label: "资料审核"/);
  assert.match(appShellSource, /to: "\/crawler", label: "arXiv 采集"[^\n]*end: true/);
  assert.match(appSource, /path="crawler\/review"/);
});

test("crawler review filter and refresh action stay in one row", () => {
  assert.match(crawlerReviewPageSource, /crawler-results__actions crawler-review__actions/);
  assert.match(pageStyles, /\.crawler-review__actions \{[^}]*flex-wrap: nowrap;/);
  assert.match(pageStyles, /\.crawler-review__actions \.button \{[^}]*white-space: nowrap;/);
});

test("crawler review actions sit to the right of the submission title on wide screens", () => {
  assert.match(
    pageStyles,
    /\.crawler-review-card__headline \{[^}]*grid-template-columns: minmax\(0, 1fr\) auto;[^}]*align-items: start;/,
  );
  assert.match(
    pageStyles,
    /\.crawler-review-card__actions \{[^}]*justify-content: flex-end;[^}]*flex-wrap: nowrap;/,
  );
  assert.match(
    pageStyles,
    /@media \(max-width: 760px\) \{[\s\S]*?\.crawler-review-card__headline \{[^}]*grid-template-columns: 1fr;/,
  );
});
