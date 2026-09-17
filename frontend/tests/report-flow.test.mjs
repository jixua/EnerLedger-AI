import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const read = (path) => readFile(new URL(`../src/${path}`, import.meta.url), "utf8");

const [reportRun, reportsPage, detailPage, chatCard, dialog, appShell, app] = await Promise.all([
  read("lib/reportRun.js"),
  read("pages/ReportsPage.jsx"),
  read("pages/ReportDetailPage.jsx"),
  read("components/ChatReportCard.jsx"),
  read("components/ReportGenerationDialog.jsx"),
  read("components/AppShell.jsx"),
  read("App.jsx"),
]);

test("报告状态文案只有一份来源", async () => {
  const consumers = [reportsPage, detailPage, chatCard, dialog];
  for (const source of consumers) {
    assert.doesNotMatch(source, /const (STATE_LABELS|RUN_LABELS|REPORT_STATE_LABELS)\s*=/, "状态文案不应在组件里另起一份");
    assert.match(source, /from "\.\.\/lib\/reportRun"/);
  }
  assert.match(reportRun, /export const REPORT_STATE_LABELS/);
  assert.match(reportRun, /export const REPORT_STATE_TONES/);
});

test("报告正文有独立路由，生成入口不再内嵌预览", async () => {
  assert.match(app, /path="reports"/);
  assert.match(app, /path="reports\/:runId"/);
  assert.match(app, /path="analysis-reports" element=\{<Navigate to="\/reports" replace \/>\}/);
  assert.match(appShell, /to: "\/reports"/);

  // 详情页是唯一渲染报告正文的地方
  assert.match(detailPage, /<ReportIrView/);
  assert.doesNotMatch(dialog, /ReportIrView|report-online-preview/);
});

test("列表与对话卡片都指向报告详情页", async () => {
  assert.match(reportsPage, /reportRunPath\(/);
  assert.match(chatCard, /reportRunPath\(/);
  assert.match(reportRun, /return `\/reports\/\$\{encodeURIComponent\(runId\)\}`/);
});

test("产物下载收敛到共享组件，不再各自复制 blob 下载", async () => {
  for (const source of [reportsPage, detailPage, chatCard]) {
    assert.match(source, /from "(\.\/|\.\.\/components\/)ReportArtifactButtons"/);
    assert.doesNotMatch(source, /URL\.createObjectURL/, "下载实现只应存在于 ReportArtifactButtons");
  }
});

test("离线图表不落成 JSON：块类型逐一渲染", async () => {
  const irView = await read("components/ReportIrView.jsx");
  for (const type of ["paragraph", "heading", "callout", "list", "metric_cards", "table"]) {
    assert.match(irView, new RegExp(`case "${type}"`), `缺少 ${type} 的分支`);
  }
  // 含负值时不能画成占比，也不能画成零宽的普通条
  assert.match(irView, /hasNegative/);
  assert.match(irView, /report-diverge/);
});
