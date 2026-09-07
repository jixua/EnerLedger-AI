import { useEffect, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, FileOutput, Loader2, X } from "lucide-react";
import {
  answerReportQuestions,
  cancelReportRun,
  createDocumentReport,
  getGeneratedReport,
  getReportRun,
  listReportQuestions,
  listModelConfigs,
  listDocumentReportRuns,
  listReportTemplates,
  retryReportRun,
} from "../lib/api";

const ACTIVE_STATES = new Set(["PENDING", "PROCESSING"]);

const RUN_LABELS = {
  PENDING: "等待处理",
  PROCESSING: "正在生成",
  NEEDS_INPUT: "等待补充信息",
  SUCCEEDED: "生成完成",
  FAILED: "生成失败",
  STALE_DOCUMENT: "文档版本已变化",
  CANCELLED: "已取消",
};

function templateReviewMessage(template) {
  return template ? "模板可用，可创建报告。" : "";
}

function isAnswerMissing(question, value) {
  if (question.field_type === "date_range") return !value?.start || !value?.end;
  return !String(value ?? "").trim();
}

function normalizeAnswerValue(question, value) {
  if (question.field_type === "number" || question.field_type === "integer") {
    return Number(value);
  }
  if (question.field_type === "array") {
    return String(value ?? "")
      .split(/[\n,，]/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return value;
}

export function ReportGenerationDialog({ document, open, onClose }) {
  const [templates, setTemplates] = useState([]);
  const [models, setModels] = useState([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [reportType, setReportType] = useState("");
  const [modelId, setModelId] = useState("");
  const [reportingYear, setReportingYear] = useState(String(new Date().getFullYear()));
  const [instructions, setInstructions] = useState("");
  const [run, setRun] = useState(null);
  const [questions, setQuestions] = useState([]);
  const [answers, setAnswers] = useState({});
  const [report, setReport] = useState(null);
  const [runHistory, setRunHistory] = useState([]);

  const selectedTemplate = useMemo(
    () => templates.find((template) => template.report_type === reportType),
    [reportType, templates],
  );

  useEffect(() => {
    if (!open) return undefined;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const documentId = document.document_id ?? document.id;
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
        setRunHistory(nextRuns || []);
        setRun((current) => current || nextRuns?.[0] || null);
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
  }, [open]);

  useEffect(() => {
    if (!open || !run?.run_id || !ACTIVE_STATES.has(run.state)) return undefined;
    let stopped = false;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      getReportRun(run.run_id, { signal: controller.signal })
        .then((next) => {
          if (!stopped) {
            setRun(next);
            setRunHistory((current) => current.map((item) => (
              item.run_id === next.run_id ? next : item
            )));
          }
        })
        .catch((requestError) => {
          if (!stopped && requestError?.name !== "AbortError") {
            setError(requestError instanceof Error ? requestError.message : "报告状态刷新失败");
          }
        });
    }, 2000);
    return () => {
      stopped = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [open, run?.run_id, run?.state]);

  useEffect(() => {
    if (!open || !run?.run_id || !["NEEDS_INPUT", "SUCCEEDED"].includes(run.state)) return undefined;
    const controller = new AbortController();
    const request = run.state === "NEEDS_INPUT"
      ? listReportQuestions(run.run_id, { signal: controller.signal })
      : getGeneratedReport(run.run_id, { signal: controller.signal });
    request
      .then((result) => {
        if (controller.signal.aborted) return;
        if (run.state === "NEEDS_INPUT") {
          const openQuestions = (result || []).filter((question) => question.status === "OPEN");
          setQuestions(openQuestions);
          setAnswers(Object.fromEntries(openQuestions.map((question) => [question.question_id, ""])));
        } else {
          setReport(result);
        }
      })
      .catch((requestError) => {
        if (requestError?.name !== "AbortError") {
          setError(requestError instanceof Error ? requestError.message : "报告结果读取失败");
        }
      });
    return () => controller.abort();
  }, [open, run?.run_id, run?.state]);

  useEffect(() => {
    if (!open) {
      setRun(null);
      setQuestions([]);
      setAnswers({});
      setReport(null);
      setRunHistory([]);
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
      const next = await createDocumentReport(document.document_id ?? document.id, {
        report_type: reportType,
        llm_config_id: Number(modelId),
        language: "zh-CN",
        reporting_year: Number(reportingYear),
        user_instructions: instructions.trim() || null,
        output_formats: ["ONLINE"],
      });
      setRun(next);
      setRunHistory((current) => [next, ...current]);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "报告创建失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleAnswers(event) {
    event.preventDefault();
    if (submitting || !questions.length) return;
    const payload = questions
      .filter((question) => question.required || String(answers[question.question_id] ?? "").trim())
      .map((question) => ({
      question_id: question.question_id,
      value: normalizeAnswerValue(question, answers[question.question_id]),
      notes: null,
    }));
    if (questions.some(
      (question) => question.required && isAnswerMissing(question, answers[question.question_id]),
    )) {
      setError("请完成所有必填补充项。");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const next = await answerReportQuestions(run.run_id, payload);
      setQuestions([]);
      setRun(next);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "补充信息提交失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleCancel() {
    if (!run?.run_id || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      setRun(await cancelReportRun(run.run_id));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "取消报告失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRetry() {
    if (!run?.run_id || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      setRun(await retryReportRun(run.run_id));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "重试报告失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSelectRun(event) {
    const runId = event.target.value;
    if (!runId || runId === run?.run_id) return;
    setError("");
    setReport(null);
    setQuestions([]);
    try {
      setRun(await getReportRun(runId));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "读取历史报告失败");
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

        {run ? (
          <div className="report-run-workbench">
            {runHistory.length > 1 ? (
              <label className="form-field report-run-history">
                <span>当前文档的报告记录</span>
                <select value={run.run_id} onChange={handleSelectRun}>
                  {runHistory.map((item) => (
                    <option key={item.run_id} value={item.run_id}>
                      {item.report_type} · {RUN_LABELS[item.state] || item.state} · {item.run_id.slice(0, 8)}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            <span className={`report-run-workbench__icon report-run-workbench__icon--${String(run.state).toLowerCase()}`}>
              {run.state === "SUCCEEDED" ? <CheckCircle2 size={24} /> : ACTIVE_STATES.has(run.state) ? <Loader2 className="spin" size={24} /> : <AlertCircle size={24} />}
            </span>
            <div>
              <p className="eyebrow">{run.report_type} · {run.template_version}</p>
              <h3>{RUN_LABELS[run.state] || run.state}</h3>
              <p>当前阶段：{run.stage}。任务已冻结文档、模板和模型版本。</p>
              {run.error_message ? <p className="form-error">{run.error_message}</p> : null}
              <code>{run.run_id}</code>
              <div className="report-run-actions">
                {ACTIVE_STATES.has(run.state) || run.state === "NEEDS_INPUT" ? (
                  <button type="button" className="button button--ghost" onClick={handleCancel} disabled={submitting}>取消任务</button>
                ) : null}
                {run.state === "FAILED" ? (
                  <button type="button" className="button button--ghost" onClick={handleRetry} disabled={submitting}>重试任务</button>
                ) : null}
                {["SUCCEEDED", "FAILED", "CANCELLED", "STALE_DOCUMENT"].includes(run.state) ? (
                  <button type="button" className="button button--ghost" onClick={() => { setRun(null); setReport(null); setQuestions([]); }}>创建新报告</button>
                ) : null}
              </div>
            </div>
            {run.state === "NEEDS_INPUT" ? (
              <form className="report-question-form" onSubmit={handleAnswers}>
                <div className="report-question-form__heading">
                  <strong>需要补充 {questions.length} 项信息</strong>
                  <span>回答将标记为 USER_INPUT，不会改写为源文档事实。</span>
                </div>
                {questions.map((question) => (
                  <label key={question.question_id} className="form-field">
                    <span>{question.question} {question.required ? <b>*</b> : null}</span>
                    {question.options?.length ? (
                      <select value={answers[question.question_id] || ""} onChange={(event) => setAnswers((current) => ({ ...current, [question.question_id]: event.target.value }))}>
                        <option value="">请选择</option>
                        {question.options.map((option) => <option key={String(option.value)} value={option.value}>{option.label}</option>)}
                      </select>
                    ) : question.field_type === "date_range" ? (
                      <span className="report-date-range">
                        <input type="date" value={answers[question.question_id]?.start || ""} onChange={(event) => setAnswers((current) => ({ ...current, [question.question_id]: { ...(current[question.question_id] || {}), start: event.target.value } }))} />
                        <input type="date" value={answers[question.question_id]?.end || ""} onChange={(event) => setAnswers((current) => ({ ...current, [question.question_id]: { ...(current[question.question_id] || {}), end: event.target.value } }))} />
                      </span>
                    ) : question.field_type === "array" ? (
                      <textarea rows="3" value={answers[question.question_id] || ""} onChange={(event) => setAnswers((current) => ({ ...current, [question.question_id]: event.target.value }))} placeholder="每行填写一项" />
                    ) : (
                      <input type={["number", "integer"].includes(question.field_type) ? "number" : question.field_type === "date" ? "date" : "text"} value={answers[question.question_id] || ""} onChange={(event) => setAnswers((current) => ({ ...current, [question.question_id]: event.target.value }))} />
                    )}
                    <small>字段：{question.field_id}</small>
                  </label>
                ))}
                {error ? <p className="form-error" role="alert">{error}</p> : null}
                <button type="submit" className="button button--primary" disabled={submitting || !questions.length}>{submitting ? <Loader2 className="spin" size={15} /> : null}{submitting ? "正在提交" : "提交并继续生成"}</button>
              </form>
            ) : null}
            {run.state === "SUCCEEDED" && report?.report_ir ? (
              <div className="report-online-preview">
                <div><strong>在线报告已生成</strong><span>{report.report_ir.sections?.length || 0} 个章节</span></div>
                {report.report_ir.sections?.map((section) => (
                  <section key={section.section_id}>
                    <h4>{section.title}</h4>
                    {(section.blocks || []).map((block, index) => (
                      <p key={`${section.section_id}-${index}`}>
                        {block.text || (block.data ? JSON.stringify(block.data) : "")}
                      </p>
                    ))}
                  </section>
                ))}
                {report.report_ir.warnings?.map((warning) => <p key={warning}>{warning}</p>)}
              </div>
            ) : null}
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
                        {template.report_type} · {template.name}
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
                  <input value="在线报告（首期）" readOnly />
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
              {runHistory.length ? <small>已恢复当前文档最近的报告任务，可在任务完成后创建新报告。</small> : null}
              {error ? <p className="form-error" role="alert">{error}</p> : null}
            </div>
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
