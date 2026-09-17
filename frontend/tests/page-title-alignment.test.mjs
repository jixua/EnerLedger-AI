import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const readPage = (name) => readFile(new URL(`../src/pages/${name}`, import.meta.url), "utf8");
const pagesCss = await readFile(new URL("../src/pages.css", import.meta.url), "utf8");
const stylesCss = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

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
});

test("内容列宽只有一个来源，不再逐页重复声明", async () => {
  // 此前 .page 写 1440、六个页面规则写 1320，而 pages.css 经 styles.css 顶部的
  // @import 先注入，同权重下 .page 后写获胜 —— 那些 1320 从未生效，页面被分成两拨。
  assert.match(stylesCss, /:root \{ --page-column: 1320px; \}/);
  assert.match(stylesCss, /\.page \{ width: 100%; max-width: var\(--page-column\)/);
  assert.match(pagesCss, /max-width: var\(--page-column\)/);
  assert.doesNotMatch(pagesCss, /max-width: 1320px/);
  assert.doesNotMatch(stylesCss, /max-width: 1440px/);
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
