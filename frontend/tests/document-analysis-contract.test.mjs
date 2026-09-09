import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const componentSource = await readFile(
  new URL("../src/components/DocumentAnalysisPanel.jsx", import.meta.url),
  "utf8",
);
const pageSource = await readFile(
  new URL("../src/pages/DocumentDetailPage.jsx", import.meta.url),
  "utf8",
);
const analysisPageSource = await readFile(
  new URL("../src/pages/DocumentAnalysisPage.jsx", import.meta.url),
  "utf8",
);
const analysisReportsPageSource = await readFile(
  new URL("../src/pages/AnalysisReportsPage.jsx", import.meta.url),
  "utf8",
);
const appSource = await readFile(
  new URL("../src/App.jsx", import.meta.url),
  "utf8",
);
const contextSource = await readFile(
  new URL("../src/state/AppContext.jsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(
  new URL("../src/lib/api.js", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(
  new URL("../src/pages.css", import.meta.url),
  "utf8",
);
const appShellSource = await readFile(
  new URL("../src/components/AppShell.jsx", import.meta.url),
  "utf8",
);
const authContextSource = await readFile(
  new URL("../src/state/AuthContext.jsx", import.meta.url),
  "utf8",
);

test("document analysis starts a background run and restores its status", () => {
  assert.match(apiSource, /\/api\/v1\/documents\/\$\{encodeURIComponent\(documentId\)\}\/analysis/);
  assert.match(apiSource, /method: "POST"/);
  assert.match(apiSource, /llm_config_id/);
  assert.match(apiSource, /export function getDocumentAnalysisStatus/);
  assert.match(apiSource, /analysis\/status/);
  assert.match(contextSource, /analyzeDocumentRequest\(id, payload, options\)/);
  assert.match(contextSource, /getDocumentAnalysisStatusRequest\(id, options\)/);
  assert.match(contextSource, /getMockDocumentAnalysis\(id\)/);
  assert.match(analysisPageSource, /loadDocumentAnalysisStatus=\{actions\.loadDocumentAnalysisStatus\}/);
  assert.match(componentSource, /loadDocumentAnalysisStatus\(documentId/);
  assert.match(componentSource, /window\.setTimeout\(poll/);
  assert.match(componentSource, /可以离开此页面/);
  assert.doesNotMatch(componentSource, /analyzeDocument\(documentId, \{\}, \{ signal:/);
});

test("document analysis loads the persisted MinIO report for the current version", () => {
  assert.match(apiSource, /export function getDocumentAnalysis/);
  assert.match(contextSource, /getDocumentAnalysisRequest\(id, options\)/);
  assert.match(contextSource, /DOCUMENT_ANALYSIS_NOT_FOUND/);
  assert.match(analysisPageSource, /loadDocumentAnalysis=\{actions\.loadDocumentAnalysis\}/);
  assert.match(componentSource, /正在读取已保存的分析报告/);
  assert.match(componentSource, /保存到 MinIO/);
});

test("document preview and analysis report use separate routes and pages", () => {
  assert.match(appSource, /documents\/:documentId\/analysis/);
  assert.doesNotMatch(pageSource, /import \{ DocumentAnalysisPanel \}/);
  assert.doesNotMatch(pageSource, /<DocumentAnalysisPanel/);
  assert.match(pageSource, /documents\/\$\{targetDocumentId\}\/analysis/);
  assert.match(pageSource, />分析报告<\/Link>/);
  assert.match(analysisPageSource, /import \{ DocumentAnalysisPanel \}/);
  assert.match(analysisPageSource, /<DocumentAnalysisPanel/);
  assert.match(analysisPageSource, /文档与分片预览/);
  assert.match(analysisPageSource, /loadDocumentAnalysis=\{actions\.loadDocumentAnalysis\}/);
  assert.match(analysisPageSource, /analyzeDocument=\{actions\.analyzeDocument\}/);
  assert.match(componentSource, /生成分析报告/);
  assert.match(componentSource, /正在分批提取证据并生成报告/);
  assert.match(componentSource, /Number\(result\.document_version\) !== documentVersion/);
});

test("analysis result renders Markdown and can be copied or downloaded", () => {
  assert.match(componentSource, /<ReactMarkdown remarkPlugins=\{\[remarkGfm\]\}>\{analysis\.markdown\}<\/ReactMarkdown>/);
  assert.match(componentSource, /navigator\.clipboard\.writeText\(analysis\.markdown\)/);
  assert.match(componentSource, /text\/markdown;charset=utf-8/);
  assert.match(componentSource, /分析报告\.md/);
  assert.match(componentSource, /保存到 MinIO/);
  assert.match(componentSource, /文档片段\{source\.citation_index\}/);
  assert.match(apiSource, /export async function downloadDocumentAnalysisDocx/);
  assert.match(apiSource, /analysis\/docx/);
  assert.match(apiSource, /wordprocessingml\.document/);
  assert.match(contextSource, /downloadDocumentAnalysisDocxRequest\(id, options\)/);
  assert.match(analysisPageSource, /downloadDocumentAnalysisDocx=\{actions\.downloadDocumentAnalysisDocx\}/);
  assert.match(componentSource, /下载 DOCX/);
});

test("analysis panel remains usable at medium and narrow widths", () => {
  assert.match(stylesSource, /\.document-analysis__report table/);
  assert.match(stylesSource, /@container document-detail \(max-width: 820px\)[\s\S]*\.document-analysis__header \{ flex-direction: column; \}/);
  assert.match(stylesSource, /@container document-detail \(max-width: 520px\)[\s\S]*\.document-analysis__actions \{ display: grid;/);
});

test("analysis report index replaces system status in navigation and links source records", () => {
  assert.match(appShellSource, /to: "\/analysis-reports", label: "分析报告"/);
  assert.doesNotMatch(appShellSource, /to: "\/system", label: "系统状态"/);
  assert.match(appSource, /path="analysis-reports"/);
  assert.match(apiSource, /export function listDocumentAnalysisReports/);
  assert.match(apiSource, /\/api\/v1\/analysis-reports/);
  assert.match(analysisReportsPageSource, />所属知识库</);
  assert.match(analysisReportsPageSource, />源文件</);
  assert.match(analysisReportsPageSource, /report\.dataset_name/);
  assert.match(analysisReportsPageSource, /report\.filename/);
  assert.match(analysisReportsPageSource, /documents\/\$\{report\.document_id\}\/analysis/);
});

test("demo administrator can open admin-only analysis reports", () => {
  assert.match(authContextSource, /forcedDemo[\s\S]*role:\s*"admin"/);
});
