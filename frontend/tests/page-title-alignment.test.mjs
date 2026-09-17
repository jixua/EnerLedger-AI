import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const readPage = (name) => readFile(new URL(`../src/pages/${name}`, import.meta.url), "utf8");
const pagesCss = await readFile(new URL("../src/pages.css", import.meta.url), "utf8");

test("top-level pages use the knowledge-library title-first hierarchy", async () => {
  const [models, system, crawler] = await Promise.all([
    readPage("ModelsPage.jsx"),
    readPage("SystemPage.jsx"),
    readPage("CrawlerReviewPage.jsx"),
  ]);

  assert.doesNotMatch(models, /Model capability registry/);
  assert.doesNotMatch(system, /System health overview/);
  // 资料审核页曾自带一套 crawler-hero：标题只有 38px（其他页 42–64px）、多一个图标块、
  // 且 eyebrow 压在标题之上，与其余页面的 title-first 层级不一致。
  assert.doesNotMatch(crawler, /crawler-hero/);
  assert.match(crawler, /className="page page--crawler feature-page"/);
  assert.match(crawler, /<header className="knowledge-hero">/);
  assert.match(crawler, /<h1>采集资料审核<\/h1>\s*<p className="knowledge-hero__subtitle">/);
  assert.match(pagesCss, /\.page--datasets \{ max-width: 1320px; padding-top: 42px; \}/);
  assert.match(pagesCss, /\.page--crawler \{ max-width: 1320px; padding-top: 42px; \}/);
  assert.match(pagesCss, /\.page--tasks \{ max-width: 1320px; padding-top: 42px; \}/);
  assert.match(pagesCss, /\.models-page \{ padding-top: 42px; \}/);
  assert.match(pagesCss, /\.system-page \{ max-width: 1320px; padding-top: 42px; \}/);
});

test("detail pages place context below the title", async () => {
  const [dataset, document] = await Promise.all([
    readPage("DatasetDetailPage.jsx"),
    readPage("DocumentDetailPage.jsx"),
  ]);

  assert.match(dataset, /<h1>\{dataset\.name\}<\/h1><p>数据集 #/);
  assert.doesNotMatch(dataset, /<p className="eyebrow">数据集/);
  assert.doesNotMatch(document, /<p className="eyebrow">文档详情/);
});
