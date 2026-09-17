import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

/**
 * 报告正文的排版：这是一份文档，不是后台列表。
 *
 * 正文的块渲染复用了列表页的 .data-table 与若干界面字号，界面上看不出错，
 * 一页读下来就会变成「字太小、色块太多」：表头 8px 大写、单元格 10px、
 * 正文 13px，指标卡 / 提示 / 证据编号各自填一块底色。所以这里钉三条线——
 * 正文字号、报告页表格字号、正文里的填充块——都是靠字号与底色判断的，
 * 改动时不会报错，只能靠测试拦。
 */

const pagesCss = await readFile(new URL("../src/pages.css", import.meta.url), "utf8");
const reportIrView = await readFile(new URL("../src/components/ReportIrView.jsx", import.meta.url), "utf8");

/** 取一条规则的声明体，用来断言它写了什么、没写什么 */
function declarations(selector) {
  const start = pagesCss.indexOf(`${selector} {`);
  assert.notEqual(start, -1, `pages.css 里找不到规则：${selector}`);
  return pagesCss.slice(start, pagesCss.indexOf("}", start));
}

function px(block, property) {
  const match = block.match(new RegExp(`${property}:\\s*([\\d.]+)px`));
  assert.ok(match, `规则里读不到 ${property}：${block}`);
  return Number.parseFloat(match[1]);
}

test("报告正文按阅读字号排，不跟随界面字号", () => {
  // 界面字号是 12–13px，正文低于 15px 就退回了列表的密度
  assert.ok(px(declarations(".report-block"), "font-size") >= 15, "正文段落小于 15px");
  assert.ok(px(declarations(".report-block--list"), "font-size") >= 15, "列表项小于 15px");
  assert.ok(px(declarations(".report-callout span"), "font-size") >= 15, "提示正文小于 15px");
  assert.ok(px(declarations(".report-section > h2"), "font-size") >= 20, "章节标题没有拉开层级");
});

test("报告页的表格用两级选择器压过列表页的紧凑表格", () => {
  const th = declarations(".page--report-detail .report-table th");
  const td = declarations(".page--report-detail .report-table td");

  // .data-table 是列表页的紧凑档：8px 大写表头、10px 单元格，且写在文件更后面，
  // 所以报告页的表头与单元格必须带页面前缀，否则字号会被它赢走
  assert.ok(px(th, "font-size") >= 12.5, "表头小于 12.5px");
  assert.ok(px(td, "font-size") >= 13, "单元格小于 13px");
  assert.match(th, /text-transform: none/, "表头又变成大写了");
  assert.match(th, /background: none/, "表头又填上底色了");

  // 窄屏下首列会被压成一字一行，所以给了下限，宁可让表格横向滚动
  assert.match(declarations(".page--report-detail .report-table td:first-child"), /min-width:/);
});

test("正文里不再靠底色分块", () => {
  for (const selector of [".report-callout", ".report-metric", ".report-evidence code"]) {
    // 不声明底色、或显式写成 none（清掉继承来的那一层）都可以，填色不行
    const fill = declarations(selector).match(/background:\s*[^;]+/);
    assert.ok(!fill || /^background:\s*none$/.test(fill[0].trim()), `${selector} 又填上底色了：${fill?.[0]}`);
  }
  // 提示改用左侧竖线，指标卡改用上边线：仍然是块，但不再是一块颜色
  assert.match(declarations(".report-callout"), /border-left:/);
  assert.match(declarations(".report-metric"), /border-top:/);
});

test("指标卡的值按内容分档：数值用指标字号，中文值退回文本字号", () => {
  assert.match(reportIrView, /const CJK = \/\[\\u4e00-\\u9fff\]\//);
  assert.match(reportIrView, /is-text/);
  assert.ok(px(declarations(".report-metric__value"), "font-size") >= 22, "指标数值不够醒目");
  assert.ok(px(declarations(".report-metric__value.is-text"), "font-size") < 20, "中文值还是占满整行");
});
