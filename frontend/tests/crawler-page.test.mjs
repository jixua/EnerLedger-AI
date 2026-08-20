import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const crawlerPageSource = await readFile(
  new URL("../src/pages/CrawlerPage.jsx", import.meta.url),
  "utf8",
);

test("crawler resets the target dataset between collection rounds", () => {
  const resetCalls = crawlerPageSource.match(/setDatasetId\(""\)/g) || [];

  assert.ok(resetCalls.length >= 2);
  assert.match(crawlerPageSource, /onChange=\{\(event\) => setDatasetId\(event\.target\.value\)\}/);
});

test("crawler unlocks dataset selection before background document refresh", () => {
  const unlockIndex = crawlerPageSource.indexOf('setImporting(false);');
  const refreshIndex = crawlerPageSource.indexOf('Promise.resolve(actions.loadDocuments?.(targetDatasetId))');

  assert.ok(unlockIndex >= 0);
  assert.ok(refreshIndex > unlockIndex);
});
