import { useEffect, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, FileOutput, Loader2, X } from "lucide-react";
import { Link } from "react-router-dom";

import {
  createDocumentReport,
  getReportRun,
  listDocumentReportRuns,
  listModelConfigs,
  listReportTemplates,
} from "../lib/api";
import {
  formatReportTime,
  isActiveReportRun,
  reportRunPath,
  reportStateLabel,
  reportStateTone,
  reportTypeName,
} from "../lib/reportRun";

function documentIdOf(document) {
  return document?.document_id ?? document?.documentId ?? document?.id;
}

function templateReviewMessage(template) {
  return template ? "模板可用，可创建报告。" : "";
}

/**
 * 生成报告对话框：只负责「创建任务」。
 *
 * 报告的正文、产物下载、补充问答都归报告详情页，对话框不再内嵌在线预览，
 * 否则「看报告」这件事会被藏在「生成报告」按钮后面。
 */
export function ReportGenerationDialog({ document, open, onClose }) {
  const [templates, setTemplates] = useState([]);
  const [models, setModels] = useState([]);
  const [history, setHistory] = useState([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [reportType, setReportType] = useState("");
  const [modelId, setModelId] = useState("");
  const [reportingYear, setReportingYear] = useState(String(new Date().getFullYear()));
  const [instructions, setInstructions] = useState("");
  const [created, setCreated] = useState(null);

  const documentId = documentIdOf(document);

  const selectedTemplate = useMemo(
    () => templates.find((template) => template.report_type === reportType),
    [reportType, templates],
  );

  useEffect(() => {
    if (!open || !documentId) return undefined;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    Promise.all([
      listReportTemplates({ signal: controller.signal }),
      listModelConfigs({ capability: "CHAT" }, { signal: controller.signal }),
      listDocumentReportRuns(documentId, { limit: 20, signal: controller.signal }),
    ])
      .then(([nextTemplates, nextModels, nextRuns]) => {
        setTemplates(nextTemplates || []);
        const toolModels = (nextModels || []).filter((model) => model.supports_tool_calling);
        setModels(toolModels);
        setReportType((current) => current || nextTemplates?.[0]?.report_type || "");
        setModelId((current) => current || String(toolModels?.[0]?.id || ""));
        setHistory(nextRuns || []);
      })
      .catch((requestError) => {
        if (requestError?.name !== "AbortError") {
          setError(requestError instanceof Error ? requestError.message : "无法读取报告配置");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [open, documentId]);

  // 刚创建的任务只在这里显示状态；详情页负责完整进度与结果。
  useEffect(() => {
    if (!open || !created?.run_id) return undefined;
    if (!["PENDING", "PROCESSING"].includes(created.state)) return undefined;
    const timer = window.setInterval(() => {
      getReportRun(created.run_id)
        .then((next) => setCreated(next))
        .catch(() => { /* 状态刷新失败不打断创建流程 */ });
    }, 3000);
    return () => window.clearInterval(timer);
  }, [open, created?.run_id, created?.state]);

  useEffect(() => {
    if (!open) {
      setCreated(null);
      setInstructions("");
      setError("");
    }
  }, [open]);

  if (!open) return null;

  async function handleSubmit(event) {
    event.preventDefault();
    if (!selectedTemplate || !modelId || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      const next = await createDocumentReport(documentId, {
        report_type: reportType,
        llm_config_id: Number(modelId),
        language: "zh-CN",
        reporting_year: Number(reportingYear),
        user_instructions: instructions.trim() || null,
      });
      setCreated(next);
      setHistory((current) => [next, ...current]);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "报告创建失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={submitting ? undefined : onClose}>
      <section
        className="dialog report-generation-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="report-generation-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="dialog__header">
          <div>
            <p className="eyebrow">文档报告</p>
            <h2 id="report-generation-title">生成报告</h2>
            <p className="dialog__subtitle">源文件：{document?.filename || "当前文档"} · v{document?.version || 1}</p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} disabled={submitting} aria-label="关闭报告窗口"><X size={18} /></button>
        </header>

        {created ? (
          <div className="report-run-workbench">
            <span className={`report-run-workbench__icon report-run-workbench__icon--${String(created.state).toLowerCase()}`}>
              {created.state === "SUCCEEDED" ? <CheckCircle2 size={24} /> : isActiveReportRun(created.state) ? <Loader2 className="spin" size={24} /> : <AlertCircle size={24} />}
            </span>
            <div>
              <p className="eyebrow">{reportTypeName(created)} · 模板 v{created.template_version}</p>
              <h3>{reportStateLabel(created.state)}</h3>
              <p>任务已冻结文档、模板和模型版本，生成过程与结果都在报告页。</p>
              <code>{created.run_id}</code>
              <div className="report-run-actions">
                <Link className="button button--primary" to={reportRunPath(created.run_id)}>打开报告页</Link>
                <button
                  type="button"
                  className="button button--ghost"
                  onClick={() => { setCreated(null); setInstructions(""); }}
                >
                  继续创建
                </button>
              </div>
            </div>
          </div>
        ) : (
          <form onSubmit={handleSubmit}>
            <div className="report-generation-dialog__body">
              {loading ? <div className="report-dialog-loading"><Loader2 className="spin" size={18} />正在读取模板和模型…</div> : null}
              <div className="form-grid form-grid--two">
                <label className="form-field">
                  <span>报告类型 <b>*</b></span>
                  <select value={reportType} onChange={(event) => setReportType(event.target.value)} disabled={loading}>
                    {templates.map((template) => (
                      <option key={template.report_type} value={template.report_type}>
                        {template.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="form-field">
                  <span>分析模型 <b>*</b></span>
                  <select value={modelId} onChange={(event) => setModelId(event.target.value)} disabled={loading}>
                    <option value="">请选择 CHAT 模型</option>
                    {models.map((model) => <option key={model.id} value={model.id}>{model.display_name || model.model_name}</option>)}
                  </select>
                </label>
                <label className="form-field">
                  <span>报告年度</span>
                  <input type="number" min="1900" max="2200" value={reportingYear} onChange={(event) => setReportingYear(event.target.value)} />
                </label>
                <label className="form-field">
                  <span>输出格式</span>
                  <input value="在线报告 + Word" readOnly />
                </label>
              </div>
              <label className="form-field">
                <span>补充要求</span>
                <textarea rows="4" maxLength="2000" value={instructions} onChange={(event) => setInstructions(event.target.value)} placeholder="可选，例如：重点展示 Scope 3。" />
              </label>
              {selectedTemplate ? (
                <div className="report-template-summary">
                  <div><strong>{selectedTemplate.name}</strong><span>{selectedTemplate.required_field_count} 个必填字段 · {selectedTemplate.blocking_field_count} 个阻塞字段</span></div>
                  <p>{templateReviewMessage(selectedTemplate)}</p>
                  <small>章节：{selectedTemplate.sections?.map((section) => section.title).join("、") || "待配置"}</small>
                </div>
              ) : null}
              {error ? <p className="form-error" role="alert">{error}</p> : null}
            </div>

            {history.length ? (
              <div className="report-generation-dialog__history">
                <h3>本文件已有的报告（{history.length}）</h3>
                <ul>
                  {history.slice(0, 5).map((item) => (
                    <li key={item.run_id}>
                      <Link to={reportRunPath(item.run_id)}>
                        <strong>{reportTypeName(item)}</strong>
                        <span className={`report-state ${reportStateTone(item.state)}`}>{reportStateLabel(item.state)}</span>
                        <small>{formatReportTime(item.created_at)}</small>
                      </Link>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            <footer className="dialog__footer">
              <button type="button" className="button button--ghost" onClick={onClose} disabled={submitting}>取消</button>
              <button type="submit" className="button button--primary" disabled={submitting || loading || !selectedTemplate || !modelId}>
                {submitting ? <Loader2 className="spin" size={16} /> : <FileOutput size={16} />}
                {submitting ? "正在创建" : "创建报告任务"}
              </button>
            </footer>
          </form>
        )}
      </section>
    </div>
  );
}

export default ReportGenerationDialog;
