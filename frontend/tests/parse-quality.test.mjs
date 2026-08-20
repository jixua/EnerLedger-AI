import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  formatParseQualityPages,
  formatParseQualityPercent,
  formatParseQualityWarning,
  isDocumentQualityUsable,
  isDocumentRetrievalReady,
  normalizeParseQuality,
} from "../src/lib/parse-quality.js";

test("PDF quality contract exposes passed metrics without inventing missing values", () => {
  const document = {
    file_type: "pdf",
    parse_quality_status: "PASSED",
    parse_quality: {
      text_coverage_ratio: 0.875,
      ocr_page_count: 2,
      ocr_required_pages: [],
      low_confidence_pages: [],
      warnings: [],
    },
  };

  const quality = normalizeParseQuality(document);

  assert.equal(quality.status, "PASSED");
  assert.equal(quality.label, "通过");
  assert.equal(quality.isBlocking, false);
  assert.equal(quality.isUsable, true);
  assert.equal(formatParseQualityPercent(quality.textCoverageRatio), "87.5%");
  assert.equal(quality.ocrPageCount, 2);
});

test("missing PDF quality is historical and never silently considered usable", () => {
  const quality = normalizeParseQuality({ file_type: "pdf", status: "READY" });

  assert.equal(quality.status, "LEGACY_UNCHECKED");
  assert.equal(quality.label, "历史未检测");
  assert.equal(quality.isUsable, false);
  assert.equal(isDocumentQualityUsable({ filename: "legacy.pdf" }), false);
  assert.equal(isDocumentQualityUsable({ file_type: "docx" }), true);
});

test("retrieval readiness follows the non-blocking READY business status", () => {
  assert.equal(isDocumentRetrievalReady({ status: "PROCESSING", retrieval_ready: true }), false);
  assert.equal(isDocumentRetrievalReady({ status: "READY", retrieval_ready: true }), true);
  assert.equal(isDocumentRetrievalReady({ status: "READY", retrieval_ready: false, file_type: "docx" }), true);
  assert.equal(isDocumentRetrievalReady({ status: "READY", file_type: "pdf", parse_quality_status: "PASSED" }), true);
  assert.equal(isDocumentRetrievalReady({ status: "READY", file_type: "pdf", parse_quality_status: "LEGACY_UNCHECKED" }), true);
});

test("OCR and low-confidence pages support explicit lists and per-page aliases", () => {
  const quality = normalizeParseQuality({
    file_type: "pdf",
    parse_quality_status: "LOW_CONFIDENCE",
    parse_quality: {
      pages: [
        { page_number: 3, ocr_required: true, low_confidence: true },
        { page_number: 5, ocr_required: false, low_confidence: true },
      ],
      ocr_page_count: 2,
    },
  });

  assert.equal(quality.status, "LOW_CONFIDENCE");
  assert.equal(quality.isBlocking, true);
  assert.deepEqual(quality.ocrRequiredPages, [3]);
  assert.deepEqual(quality.lowConfidencePages, [3, 5]);
  assert.equal(formatParseQualityPages(quality.lowConfidencePages), "第 3、5 页");
});

test("page provenance failures remain an explicit blocking status", () => {
  const quality = normalizeParseQuality({
    file_type: "pdf",
    status: "FAILED",
    parse_quality_status: "PAGE_PROVENANCE_INVALID",
    parse_quality: {
      status: "PAGE_PROVENANCE_INVALID",
      warnings: ["TEXT_OUTSIDE_PAGE_MARKERS"],
    },
  });

  assert.equal(quality.status, "PAGE_PROVENANCE_INVALID");
  assert.equal(quality.label, "页码归属异常");
  assert.equal(quality.isBlocking, true);
  assert.equal(quality.isUsable, false);
});

test("a blocking JSON quality report overrides a stale passed scalar", () => {
  const quality = normalizeParseQuality({
    file_type: "pdf",
    status: "READY",
    parse_quality_status: "PASSED",
    parse_quality: { status: "PAGE_PROVENANCE_INVALID" },
  });

  assert.equal(quality.status, "PAGE_PROVENANCE_INVALID");
  assert.equal(quality.isUsable, false);
});

test("fallback and special-validation failures override a misleading passed status", () => {
  const fallback = normalizeParseQuality({
    file_type: "pdf",
    parse_quality_status: "PASSED",
    parse_quality: {
      fallback_report: {
        status: "FAILED",
        processed_page_count: 2,
        failed_page_count: 1,
        results: [{ page_number: 4, method: "OCR" }],
      },
    },
  });
  assert.equal(fallback.status, "FALLBACK_FAILED");
  assert.equal(fallback.fallback.processedPageCount, 2);
  assert.equal(fallback.fallback.ocrPageCount, 1);
  assert.equal(fallback.isBlocking, true);

  const incomplete = normalizeParseQuality({
    file_type: "pdf",
    parse_quality_status: "FALLBACK_INCOMPLETE",
    parse_quality: {
      vision_incomplete_pages: [4],
      fallback: { processed_page_count: 1, results: [{ page_number: 4, method: "VISION" }] },
    },
  });
  assert.equal(incomplete.status, "FALLBACK_FAILED");
  assert.equal(incomplete.fallback.failed, true);
  assert.deepEqual(incomplete.visionIncompletePages, [4]);

  const validation = normalizeParseQuality({
    file_type: "pdf",
    parse_quality_status: "PASSED",
    parse_quality: {
      content_validation: {
        structural_passed: false,
        evaluated_page_count: 8,
        blocking_issues: [{ code: "TABLE_MISSING", message: "第 6 页表格未完整输出" }],
        affected_pages: { table: [6], image: [], formula: [] },
      },
    },
  });
  assert.equal(validation.status, "CONTENT_VALIDATION_FAILED");
  assert.equal(validation.validation.blockingIssueCount, 1);
  assert.deepEqual(validation.validation.affectedPages, [6]);
  assert.equal(validation.isUsable, false);

  const listSummary = normalizeParseQuality({
    file_type: "pdf",
    parse_quality_status: "PASSED",
    parse_quality: {
      status: "PASSED",
      fallback: {
        processed_page_count: 3,
        ocr_page_count: 2,
        vision_page_count: 1,
        pages: [2, 4, 5],
      },
    },
  });
  assert.equal(listSummary.fallback.ocrPageCount, 2);
  assert.equal(listSummary.fallback.visionPageCount, 1);
  assert.deepEqual(listSummary.fallback.pages, [2, 4, 5]);
});

test("quality warnings are translated when known and preserved when unknown", () => {
  assert.equal(formatParseQualityWarning("PAGE_7_OCR_LOW_CONFIDENCE"), "第 7 页：OCR 置信度偏低");
  assert.equal(
    formatParseQualityWarning("WORD_OLE_PREVIEW_MISSING:source=2,preview=1"),
    "部分 Word 内嵌对象没有可用预览，其他内容已正常入库",
  );
  assert.equal(formatParseQualityWarning("CUSTOM_GATE_WARNING"), "CUSTOM_GATE_WARNING");
});

const datasetListSource = await readFile(new URL("../src/pages/DatasetsPage.jsx", import.meta.url), "utf8");
const datasetDetailSource = await readFile(new URL("../src/pages/DatasetDetailPage.jsx", import.meta.url), "utf8");
const documentDetailSource = await readFile(new URL("../src/pages/DocumentDetailPage.jsx", import.meta.url), "utf8");
const tasksSource = await readFile(new URL("../src/pages/TasksPage.jsx", import.meta.url), "utf8");
const playgroundSource = await readFile(new URL("../src/pages/PlaygroundPage.jsx", import.meta.url), "utf8");

test("dataset create and settings send the optional VISION binding with reparse guidance", () => {
  assert.match(datasetListSource, /modelCapability\(model\) === 'VISION'/);
  assert.match(datasetListSource, /vision_config_id: form\.vision_config_id \? Number\(form\.vision_config_id\) : null/);
  assert.match(datasetListSource, /PDF OCR \/ 视觉模型/);
  assert.match(datasetDetailSource, /model\.capability === "VISION"/);
  assert.match(datasetDetailSource, /payload\.vision_config_id/);
  assert.match(datasetDetailSource, /现有文档将生成新版本、重新解析并重建检索索引/);
});

test("quality diagnostics never block READY documents or add a detail-page banner", () => {
  assert.doesNotMatch(datasetDetailSource, /ParseQualityInline/);
  assert.doesNotMatch(documentDetailSource, /文档已入库，但有.*项解析提醒/);
  assert.doesNotMatch(documentDetailSource, /formatParseQualityWarning/);
  assert.doesNotMatch(documentDetailSource, /当前正文未通过质量门禁/);
  assert.doesNotMatch(tasksSource, /ParseQualityInline/);
  assert.match(datasetListSource, /items\.filter\(isDocumentRetrievalReady\)/);
  assert.match(datasetDetailSource, /datasetDocuments\.filter\(isDocumentRetrievalReady\)/);
  assert.match(tasksSource, /counts\.RETRIEVAL_READY/);
  assert.match(tasksSource, /canRetryDocument\(document\)/);
  assert.match(playgroundSource, /isDocumentRetrievalReady\(document\)/);
});
