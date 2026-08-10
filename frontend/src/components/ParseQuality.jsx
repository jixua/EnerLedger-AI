import {
  AlertTriangle,
  CheckCircle2,
  CircleHelp,
  FileWarning,
  ScanText,
  ShieldAlert,
} from "lucide-react";
import {
  formatParseQualityPages,
  formatParseQualityPercent,
  formatParseQualityWarning,
  normalizeParseQuality,
} from "../lib/parse-quality";

const ICONS = {
  PASSED: CheckCircle2,
  OCR_REQUIRED: ScanText,
  LOW_CONFIDENCE: AlertTriangle,
  PAGE_COUNT_MISMATCH: FileWarning,
  PAGE_PROVENANCE_INVALID: FileWarning,
  FALLBACK_FAILED: ShieldAlert,
  CONTENT_VALIDATION_FAILED: ShieldAlert,
  LEGACY_UNCHECKED: CircleHelp,
  NOT_APPLICABLE: CheckCircle2,
};

function QualityIcon({ quality, size = 13 }) {
  const Icon = ICONS[quality.status] || CircleHelp;
  return <Icon size={size} aria-hidden="true" />;
}

export function ParseQualityBadge({ document, hideNotApplicable = true }) {
  const quality = normalizeParseQuality(document);
  if (hideNotApplicable && quality.status === "NOT_APPLICABLE") return null;
  return (
    <span
      className={`parse-quality-badge parse-quality-badge--${quality.tone}`}
      title={quality.description}
      data-quality-status={quality.status}
    >
      <QualityIcon quality={quality} />
      {quality.label}
    </span>
  );
}

export function ParseQualityInline({ document }) {
  const quality = normalizeParseQuality(document);
  if (quality.status === "NOT_APPLICABLE") return null;
  const metrics = [];
  if (quality.textCoverageRatio !== null) metrics.push(`覆盖率 ${formatParseQualityPercent(quality.textCoverageRatio)}`);
  if (quality.ocrPageCount !== null) metrics.push(`OCR ${quality.ocrPageCount} 页`);
  if (quality.lowConfidencePages.length) metrics.push(`低置信 ${formatParseQualityPages(quality.lowConfidencePages)}`);
  return (
    <div className={`parse-quality-inline${quality.isBlocking ? " parse-quality-inline--blocking" : ""}`}>
      <ParseQualityBadge document={document} />
      {quality.isBlocking ? <small>质量门禁未通过，当前不可用于检索</small> : metrics.length ? <small>{metrics.join(" · ")}</small> : null}
    </div>
  );
}

function Metric({ label, value }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

function FallbackSummary({ report }) {
  if (!report) return null;
  const details = [
    `${report.processedPageCount ?? 0} 页已处理`,
    report.ocrPageCount ? `OCR ${report.ocrPageCount} 页` : null,
    report.visionPageCount ? `视觉分析 ${report.visionPageCount} 页` : null,
    report.failedPageCount ? `${report.failedPageCount} 页失败` : null,
  ].filter(Boolean);
  return (
    <article className={`parse-quality-stage${report.failed ? " parse-quality-stage--failed" : ""}`}>
      <div><ScanText size={16} /><strong>页级兜底</strong><span>{report.failed ? "未完成" : "已执行"}</span></div>
      <p>{details.join(" · ") || "已记录兜底处理摘要"}</p>
      {report.pages.length ? <small>处理页码：{formatParseQualityPages(report.pages)}</small> : null}
    </article>
  );
}

function ValidationSummary({ report }) {
  if (!report) return null;
  const result = report.structuralPassed === true && !report.failed
    ? "通过"
    : report.failed ? "未通过" : "已执行";
  const details = [
    report.evaluatedPageCount !== null ? `检查 ${report.evaluatedPageCount} 页` : null,
    report.blockingIssueCount ? `${report.blockingIssueCount} 个阻断项` : null,
    report.warningCount ? `${report.warningCount} 个提醒` : null,
    report.pageSetComplete === false ? "页集合不完整" : null,
  ].filter(Boolean);
  return (
    <article className={`parse-quality-stage${report.failed ? " parse-quality-stage--failed" : ""}`}>
      <div><ShieldAlert size={16} /><strong>表格 / 图片 / 公式专项验证</strong><span>{result}</span></div>
      <p>{details.join(" · ") || "已记录专项验证摘要"}</p>
      {report.affectedPages.length ? <small>受影响页码：{formatParseQualityPages(report.affectedPages)}</small> : null}
    </article>
  );
}

export function ParseQualitySummary({ document }) {
  const quality = normalizeParseQuality(document);
  if (quality.status === "NOT_APPLICABLE") return null;
  const warnings = [
    ...quality.warnings,
    ...(quality.fallback?.warnings || []),
    ...(quality.validation?.issues || []),
  ].filter((value, index, values) => values.indexOf(value) === index);

  return (
    <section className={`parse-quality-summary parse-quality-summary--${quality.tone}`} aria-label="PDF 解析质量">
      <header>
        <span className="parse-quality-summary__icon"><QualityIcon quality={quality} size={19} /></span>
        <div>
          <div className="parse-quality-summary__title"><h2>PDF 解析质量</h2><ParseQualityBadge document={document} /></div>
          <p>{quality.description}</p>
        </div>
      </header>

      {quality.isBlocking ? (
        <div className="parse-quality-summary__gate" role="alert">
          当前解析结果未通过质量门禁，不应作为检索与对话依据。请配置 PDF OCR/视觉模型后重新解析，或检查原文件。
        </div>
      ) : quality.status === "LEGACY_UNCHECKED" ? (
        <div className="parse-quality-summary__gate parse-quality-summary__gate--neutral">
          该历史版本未经过质量检测，不能据此确认解析完整性；建议重新解析后再用于正式检索。
        </div>
      ) : null}

      <dl className="parse-quality-metrics">
        <Metric label="文本覆盖率" value={formatParseQualityPercent(quality.textCoverageRatio)} />
        <Metric label="OCR 页数" value={quality.ocrPageCount === null ? "—" : `${quality.ocrPageCount} 页`} />
        <Metric label="触发 OCR 页码" value={formatParseQualityPages(quality.ocrRequiredPages)} />
        <Metric label="低置信页码" value={formatParseQualityPages(quality.lowConfidencePages)} />
      </dl>

      {quality.visionIncompletePages.length ? <div className="parse-quality-summary__gate" role="alert">视觉兜底未完成页码：{formatParseQualityPages(quality.visionIncompletePages)}</div> : null}

      {quality.fallback || quality.validation ? (
        <div className="parse-quality-stages">
          <FallbackSummary report={quality.fallback} />
          <ValidationSummary report={quality.validation} />
        </div>
      ) : null}

      {warnings.length ? (
        <div className="parse-quality-warnings">
          <strong><AlertTriangle size={14} />检测提示（{warnings.length}）</strong>
          <ul>{warnings.map((warning) => <li key={warning}>{formatParseQualityWarning(warning)}</li>)}</ul>
        </div>
      ) : null}
    </section>
  );
}
