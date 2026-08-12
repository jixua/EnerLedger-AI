/**
 * @typedef {"PASSED"|"OCR_REQUIRED"|"LOW_CONFIDENCE"|"PAGE_COUNT_MISMATCH"|"PAGE_PROVENANCE_INVALID"|"FALLBACK_FAILED"|"CONTENT_VALIDATION_FAILED"|"LEGACY_UNCHECKED"|"NOT_APPLICABLE"} ParseQualityStatus
 */

const QUALITY_META = {
  PASSED: {
    label: "通过",
    tone: "passed",
    description: "页面完整性与正文覆盖检查通过。",
    isBlocking: false,
    isUsable: true,
  },
  OCR_REQUIRED: {
    label: "待 OCR",
    tone: "warning",
    description: "部分页面缺少有效正文，需要 OCR 或视觉模型补全。",
    isBlocking: true,
    isUsable: false,
  },
  LOW_CONFIDENCE: {
    label: "低置信度",
    tone: "warning",
    description: "部分 OCR 页面识别置信度偏低，需要复核或重新解析。",
    isBlocking: true,
    isUsable: false,
  },
  PAGE_COUNT_MISMATCH: {
    label: "页数不一致",
    tone: "danger",
    description: "原 PDF 页数与解析页标记不一致，文档内容可能不完整。",
    isBlocking: true,
    isUsable: false,
  },
  PAGE_PROVENANCE_INVALID: {
    label: "页码归属异常",
    tone: "danger",
    description: "解析结果存在无法归属到原 PDF 页的正文，页码溯源不可信。",
    isBlocking: true,
    isUsable: false,
  },
  FALLBACK_FAILED: {
    label: "页级兜底失败",
    tone: "danger",
    description: "需要补全的页面未完成 OCR 或视觉兜底。",
    isBlocking: true,
    isUsable: false,
  },
  CONTENT_VALIDATION_FAILED: {
    label: "专项验证失败",
    tone: "danger",
    description: "表格、图片或公式等结构化内容未通过完整性验证。",
    isBlocking: true,
    isUsable: false,
  },
  LEGACY_UNCHECKED: {
    label: "历史未检测",
    tone: "neutral",
    description: "该历史 PDF 生成时尚未执行解析质量检测，建议重新解析。",
    isBlocking: false,
    isUsable: false,
  },
  NOT_APPLICABLE: {
    label: "无需检测",
    tone: "neutral",
    description: "当前文件类型不适用 PDF 解析质量检测。",
    isBlocking: false,
    isUsable: true,
  },
};

function isObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function firstObject(...values) {
  return values.find(isObject) || null;
}

function firstDefined(sources, keys) {
  for (const source of sources) {
    if (!isObject(source)) continue;
    for (const key of keys) {
      if (source[key] !== undefined && source[key] !== null) return source[key];
    }
  }
  return null;
}

function finiteNumber(value) {
  if (value === "" || value === null || value === undefined || typeof value === "boolean") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function booleanValue(value) {
  if (typeof value === "boolean") return value;
  if (String(value).toLowerCase() === "true") return true;
  if (String(value).toLowerCase() === "false") return false;
  return null;
}

function uniquePages(value) {
  const values = Array.isArray(value) ? value : value === null || value === undefined ? [] : [value];
  return [...new Set(values.map(finiteNumber).filter((page) => Number.isInteger(page) && page > 0))].sort((a, b) => a - b);
}

function warningKey(value) {
  if (typeof value === "string") return value.trim();
  if (!isObject(value)) return "";
  return String(value.message || value.code || value.warning || "").trim();
}

function normalizeWarnings(...values) {
  const result = [];
  const seen = new Set();
  values.flatMap((value) => Array.isArray(value) ? value : value ? [value] : []).forEach((value) => {
    const text = warningKey(value);
    if (!text || seen.has(text)) return;
    seen.add(text);
    result.push(text);
  });
  return result;
}

function normalizeStatus(rawStatus) {
  const value = String(rawStatus || "").trim().toUpperCase().replace(/[\s-]+/g, "_");
  if (Object.prototype.hasOwnProperty.call(QUALITY_META, value)) return value;
  if (value === "FALLBACK_INCOMPLETE") return "FALLBACK_FAILED";
  if (value.includes("FALLBACK") && (value.includes("FAIL") || value.includes("ERROR"))) return "FALLBACK_FAILED";
  if ((value.includes("VALIDATION") || value.includes("STRUCTURAL")) && (value.includes("FAIL") || value.includes("ERROR"))) {
    return "CONTENT_VALIDATION_FAILED";
  }
  return null;
}

function normalizeFallback(report) {
  if (!isObject(report)) return null;
  const results = Array.isArray(report.results) ? report.results.filter(isObject) : [];
  const status = String(report.status || "").toUpperCase();
  const failedPageCount = finiteNumber(report.failed_page_count ?? report.failure_count) || 0;
  const failed = report.failed === true || failedPageCount > 0 || status.includes("FAIL") || status.includes("ERROR");
  const processedPageCount = finiteNumber(report.processed_page_count) ?? results.length;
  const ocrPageCount = finiteNumber(report.ocr_page_count)
    ?? results.filter((item) => String(item.method || "").toUpperCase() === "OCR").length;
  const visionPageCount = finiteNumber(report.vision_page_count)
    ?? results.filter((item) => String(item.method || "").toUpperCase() === "VISION").length;
  return {
    failed,
    status: status || null,
    processedPageCount,
    failedPageCount,
    ocrPageCount,
    visionPageCount,
    pages: uniquePages(report.pages ?? results.map((item) => item.page_number ?? item.page)),
    warnings: normalizeWarnings(report.warnings),
  };
}

function normalizeValidation(report) {
  if (!isObject(report)) return null;
  const status = String(report.status || "").toUpperCase();
  const structuralPassed = booleanValue(report.structural_passed ?? report.passed);
  const pageSetComplete = booleanValue(report.page_set_complete);
  const blockingIssues = Array.isArray(report.blocking_issues) ? report.blocking_issues : [];
  const warnings = Array.isArray(report.warnings) ? report.warnings : [];
  const blockingIssueCount = finiteNumber(report.blocking_issue_count) ?? blockingIssues.length;
  const warningCount = finiteNumber(report.warning_count) ?? warnings.length;
  const failed = structuralPassed === false || blockingIssueCount > 0 || status.includes("FAIL") || status.includes("ERROR");
  return {
    failed,
    status: status || null,
    structuralPassed,
    pageSetComplete,
    evaluatedPageCount: finiteNumber(report.evaluated_page_count),
    blockingIssueCount,
    warningCount,
    missingPages: uniquePages(report.missing_markdown_pages),
    unexpectedPages: uniquePages(report.unexpected_markdown_pages),
    affectedPages: uniquePages(Object.values(isObject(report.affected_pages) ? report.affected_pages : {}).flat()),
    issues: normalizeWarnings(blockingIssues, warnings),
  };
}

function isPdfDocument(document) {
  const fileType = String(document?.file_type ?? document?.fileType ?? "").toLowerCase();
  if (fileType) return fileType === "pdf" || fileType === "application/pdf";
  return String(document?.filename || "").toLowerCase().endsWith(".pdf");
}

/**
 * Normalize both the current snake_case API contract and compatible nested reports.
 * Missing PDF quality is intentionally treated as legacy/unverified, never as passed.
 */
export function normalizeParseQuality(document) {
  const quality = firstObject(document?.parse_quality, document?.parseQuality) || {};
  const pageQuality = firstObject(quality.page_quality, quality.pageQuality);
  const finalQuality = firstObject(
    quality.final_quality,
    quality.finalQuality,
    quality.quality_report,
    quality.qualityReport,
    quality.report,
    quality.quality,
    pageQuality,
  ) || quality;
  const sources = [quality, finalQuality, pageQuality];
  const fallbackRaw = firstObject(
    quality.fallback,
    quality.fallback_report,
    quality.fallbackReport,
    quality.page_fallback,
    quality.page_fallback_report,
    finalQuality.fallback,
  );
  const validationRaw = firstObject(
    quality.validation,
    quality.validation_report,
    quality.validationReport,
    quality.content_validation,
    quality.content_validation_report,
    quality.special_validation,
    finalQuality.validation,
  );
  const fallback = normalizeFallback(fallbackRaw);
  const validation = normalizeValidation(validationRaw);

  const scalarStatus = normalizeStatus(
    document?.parse_quality_status ?? document?.parseQualityStatus,
  );
  const reportStatus = normalizeStatus(quality.status ?? finalQuality.status);
  let status = scalarStatus || reportStatus;
  if (reportStatus && reportStatus !== "PASSED") status = reportStatus;
  if (fallback?.failed) status = "FALLBACK_FAILED";
  if (validation?.failed) status = "CONTENT_VALIDATION_FAILED";
  if (!status) status = isPdfDocument(document) ? "LEGACY_UNCHECKED" : "NOT_APPLICABLE";
  if (status === "FALLBACK_FAILED" && fallback) fallback.failed = true;

  const pages = firstDefined(sources, ["pages", "per_page", "perPage"]);
  const normalizedPages = Array.isArray(pages) ? pages.filter(isObject) : [];
  const ocrRequiredPages = uniquePages(
    firstDefined(sources, ["ocr_required_pages", "ocrRequiredPages"])
      ?? normalizedPages.filter((page) => page.ocr_required === true).map((page) => page.page_number ?? page.page),
  );
  const lowConfidencePages = uniquePages(
    firstDefined(sources, ["low_confidence_pages", "lowConfidencePages"])
      ?? normalizedPages.filter((page) => page.low_confidence === true).map((page) => page.page_number ?? page.page),
  );
  const meta = QUALITY_META[status] || QUALITY_META.LEGACY_UNCHECKED;
  const coverage = finiteNumber(firstDefined(sources, ["text_coverage_ratio", "textCoverageRatio"]));

  return {
    status,
    ...meta,
    isPdf: isPdfDocument(document),
    textCoverageRatio: coverage === null ? null : Math.max(0, Math.min(coverage > 1 && coverage <= 100 ? coverage / 100 : coverage, 1)),
    ocrPageCount: finiteNumber(firstDefined(sources, ["ocr_page_count", "ocrPageCount"])),
    ocrRequiredPages,
    lowConfidencePages,
    visionIncompletePages: uniquePages(firstDefined(sources, ["vision_incomplete_pages", "visionIncompletePages"])),
    pdfPageCount: finiteNumber(firstDefined(sources, ["pdf_page_count", "pdfPageCount"])),
    markdownPageCount: finiteNumber(firstDefined(sources, ["markdown_page_count", "markdownPageCount"])),
    warnings: normalizeWarnings(quality.warnings, finalQuality.warnings),
    fallback,
    validation,
    raw: quality,
  };
}

export function isDocumentQualityUsable(document) {
  return normalizeParseQuality(document).isUsable;
}

/**
 * The single frontend definition of a document that may participate in recall.
 * 质量诊断不再具备业务阻塞能力；文档完成三路索引并进入 READY 即可参与召回。
 */
export function isDocumentRetrievalReady(document) {
  const status = String(document?.status || "").trim().toUpperCase();
  return status === "READY" || status === "SUCCESS";
}

export function formatParseQualityPercent(value) {
  const number = finiteNumber(value);
  if (number === null) return "—";
  const ratio = number > 1 && number <= 100 ? number / 100 : number;
  return new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 1 }).format(Math.max(0, Math.min(ratio, 1)));
}

export function formatParseQualityPages(pages) {
  const normalized = uniquePages(pages);
  return normalized.length ? `第 ${normalized.join("、")} 页` : "—";
}

export function formatParseQualityWarning(value) {
  const warning = warningKey(value);
  const pageMatch = warning.match(/^PAGE_(\d+)_(.+)$/);
  const page = pageMatch?.[1];
  const code = pageMatch?.[2] || warning.split(":", 1)[0];
  const labels = {
    PAGE_MARKER_COUNT_MISMATCH: "PDF 页数与解析页标记数量不一致",
    PAGE_MARKER_SEQUENCE_MISMATCH: "解析页标记顺序异常",
    MARKDOWN_SECTION_MISSING: "缺少解析正文",
    IMAGE_ONLY: "页面仅包含图片或有效文本过少",
    OCR_REQUIRED: "需要 OCR 补全文本",
    OCR_LOW_CONFIDENCE: "OCR 置信度偏低",
    OCR_TEXT_INSUFFICIENT: "OCR 提取文本不足",
    OCR_CONFIDENCE_MISSING: "缺少 OCR 置信度",
    OCR_RESULT_PAGE_OUT_OF_RANGE: "OCR 结果页码超出 PDF 范围",
    CONFIDENCE_UNAVAILABLE: "识别服务未返回置信度",
    WORD_IMAGE_TRANSCODE_FAILED: "特殊格式图片转换失败，其他内容已正常入库",
    WORD_IMAGE_FORMAT_NOT_VISION_SUPPORTED: "特殊格式图片已保留，但暂不支持视觉识别",
    WORD_OLE_PREVIEW_MISSING: "部分 Word 内嵌对象没有可用预览，其他内容已正常入库",
    WORD_CHART_DATA_EXTRACTION_INCOMPLETE: "部分 Word 图表数据未能完整提取，其他内容已正常入库",
    WORD_DIAGRAM_EXTRACTION_UNSUPPORTED: "部分 Word 关系图暂不能结构化提取，其他内容已正常入库",
    WORD_UNSUPPORTED_OBJECT: "部分 Word 特殊对象无法结构化解析，其他内容已正常入库",
  };
  const label = labels[code];
  if (label) return page ? `第 ${page} 页：${label}` : label;
  return warning;
}
