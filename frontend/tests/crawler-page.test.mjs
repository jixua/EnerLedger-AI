import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const pageSource = await readFile(
  new URL("../src/pages/CrawlerPage.jsx", import.meta.url),
  "utf8",
);

test("crawler requires a dataset before search and places that selector first", () => {
  const datasetSelector = pageSource.indexOf('className="crawler-search__dataset"');
  const resultLimit = pageSource.indexOf('className="crawler-search__limit"');
  const queryInput = pageSource.indexOf('className="crawler-search__input"');

  assert.ok(datasetSelector >= 0);
  assert.ok(resultLimit > datasetSelector);
  assert.ok(queryInput > resultLimit);
  assert.match(pageSource, /disabled=\{!datasetId \|\| loading \|\| query\.trim\(\)\.length < 2\}/);
  assert.match(pageSource, /if \(!datasetId \|\| normalized\.length < 2 \|\| loading\) return/);
});

test("crawler always enables AI optimization without showing a toggle or success card", () => {
  assert.match(pageSource, /aiOptimize: true/);
  assert.doesNotMatch(pageSource, /setAiOptimize|crawler-search__ai|crawler-search__note/);
  assert.doesNotMatch(pageSource, /AI 优化检索|规则优化检索/);
  assert.match(pageSource, /result\?\.optimization_warning/);
});

test("changing dataset invalidates old search results and fixes the import target", () => {
  assert.match(pageSource, /function handleDatasetChange/);
  assert.match(pageSource, /setResult\(null\)/);
  assert.match(pageSource, /setSelectedIds\(\[\]\)/);
  assert.match(pageSource, /crawler-import__target/);
  assert.doesNotMatch(pageSource, /aria-label="目标数据集"/);
});
