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

test("产物下载收敛到共享组件，且只在报告语境里出现", async () => {
  // 列表只做索引；下载归详情页，对话卡片保留一处就近入口
  assert.doesNotMatch(reportsPage, /ReportArtifactButtons|URL\.createObjectURL/);
  for (const source of [detailPage, chatCard]) {
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

test("界面只讲报告类型名称，不暴露 R1–R7 内部编号", async () => {
  const playground = await read("pages/PlaygroundPage.jsx");
  const leakPatterns = [
    /\{run\.report_type\}/,
    /\{item\.report_type\}/,
    /\{created\.report_type\}/,
    /\$\{run\.report_type\}/,
    /report_type\} ·/,
    /"R[1-7] · /,
    /R1–R7|R1-R7/,
  ];
  for (const [name, source] of Object.entries({
    reportsPage,
    detailPage,
    chatCard,
    dialog,
    playground,
  })) {
    for (const pattern of leakPatterns) {
      assert.doesNotMatch(source, pattern, `${name} 把内部编号渲染给了用户`);
    }
  }
  // 展示名称统一走 helper，取值来自接口的 report_type_name
  for (const source of [reportsPage, detailPage, chatCard, dialog]) {
    assert.match(source, /reportTypeName\(/);
  }
  assert.match(reportRun, /export function reportTypeName/);
  assert.match(reportRun, /run\?\.report_type_name/);
});

test("对话上传只问文件，不问用途", async () => {
  const playground = await read("pages/PlaygroundPage.jsx");
  const sse = await read("lib/sse.js");
  // 一个入口按钮，不再有「来源文档 / 报告模板」的角色菜单
  assert.match(playground, /aria-label="上传文件"/);
  assert.doesNotMatch(playground, /pickUploadFile|composer-selector__panel--upload/);
  // 份数上限仍在；用途交给服务端判断
  assert.match(playground, /attachments\.length >= 2/);
  assert.doesNotMatch(sse, /role: attachment\.role,/);
});
