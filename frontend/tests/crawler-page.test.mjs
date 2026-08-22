import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const pageSource = await readFile(
  new URL("../src/pages/CrawlerPage.jsx", import.meta.url),
  "utf8",
);

test("crawler requires a dataset before search and places that selector first", () => {
  const datasetSelector = pageSource.indexOf('className="crawler-search__dataset"');
  const queryInput = pageSource.indexOf('className="crawler-search__input"');

  assert.ok(datasetSelector >= 0);
  assert.ok(queryInput > datasetSelector);
  assert.match(pageSource, /disabled=\{!datasetId \|\| loading \|\| query\.trim\(\)\.length < 2\}/);
  assert.match(pageSource, /if \(!datasetId \|\| normalized\.length < 2 \|\| loading\) return/);
});

test("changing dataset invalidates old search results and fixes the import target", () => {
  assert.match(pageSource, /function handleDatasetChange/);
  assert.match(pageSource, /setResult\(null\)/);
  assert.match(pageSource, /setSelectedIds\(\[\]\)/);
  assert.match(pageSource, /crawler-import__target/);
  assert.doesNotMatch(pageSource, /aria-label="目标数据集"/);
});
