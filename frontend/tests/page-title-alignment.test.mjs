import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const readPage = (name) => readFile(new URL(`../src/pages/${name}`, import.meta.url), "utf8");
const pagesCss = await readFile(new URL("../src/pages.css", import.meta.url), "utf8");

test("top-level pages use the knowledge-library title-first hierarchy", async () => {
  const [models, system] = await Promise.all([
    readPage("ModelsPage.jsx"),
    readPage("SystemPage.jsx"),
  ]);

  assert.doesNotMatch(models, /Model capability registry/);
  assert.doesNotMatch(system, /System health overview/);
  assert.match(pagesCss, /\.page--datasets \{ max-width: 1320px; padding-top: 42px; \}/);
  assert.match(pagesCss, /\.crawler-page \{ max-width: 1320px; padding-top: 42px; \}/);
  assert.match(pagesCss, /\.page--tasks \{ max-width: 1320px; padding-top: 42px; \}/);
  assert.match(pagesCss, /\.crawler-review-page \{ max-width: 1320px; padding-top: 42px; \}/);
  assert.match(pagesCss, /\.models-page \{ padding-top: 42px; \}/);
  assert.match(pagesCss, /\.system-page \{ max-width: 1320px; padding-top: 42px; \}/);
});

test("detail pages place context below the title", async () => {
  const [dataset, document, analysis] = await Promise.all([
    readPage("DatasetDetailPage.jsx"),
    readPage("DocumentDetailPage.jsx"),
    readPage("DocumentAnalysisPage.jsx"),
  ]);

  assert.match(dataset, /<h1>\{dataset\.name\}<\/h1><p>数据集 #/);
  assert.doesNotMatch(dataset, /<p className="eyebrow">数据集/);
  assert.doesNotMatch(document, /<p className="eyebrow">文档详情/);
  assert.doesNotMatch(analysis, /<p className="eyebrow">分析报告/);
});
