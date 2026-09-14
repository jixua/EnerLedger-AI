import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const systemPageSource = await readFile(new URL("../src/pages/SystemPage.jsx", import.meta.url), "utf8");

test("system status uses the selected flat dependency directory", () => {
  assert.match(systemPageSource, /className="dependency-list__header"/);
  assert.match(systemPageSource, /<span>服务<\/span>[\s\S]*<span>用途<\/span>[\s\S]*<span>当前状态<\/span>[\s\S]*<span>验证状态<\/span>/);
  assert.match(systemPageSource, /className="dependency-row__role"/);
  assert.doesNotMatch(systemPageSource, /paper-card/);
});

test("system status preserves the real health-check action and dependencies", () => {
  assert.match(systemPageSource, /onClick=\{\(\) => refreshHealth\?\.\(\)\.catch\(\(\) => \{\}\)\}/);
  assert.match(systemPageSource, /<small>\/health\/live<\/small>/);
  for (const service of ["MySQL", "MinIO", "Qdrant", "Manticore", "解析队列", "解析服务"]) {
    assert.match(systemPageSource, new RegExp(`name: "${service}"`));
  }
});
