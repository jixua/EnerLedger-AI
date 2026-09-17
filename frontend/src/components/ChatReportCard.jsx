import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, Download, FileOutput, Loader2, RefreshCw } from "lucide-react";
import {
  answerReportQuestions,
  downloadReportArtifact,
  getGeneratedReport,
  getReportRun,
  listReportQuestions,
  retryReportRun,
} from "../lib/api";
import { ReportQuestionsForm } from "./ReportQuestionsForm";

const ACTIVE_STATES = new Set(["PENDING", "PROCESSING"]);
const STATE_LABELS = {
  PENDING: "等待处理",
  PROCESSING: "正在生成",
  NEEDS_INPUT: "等待补充信息",
  SUCCEEDED: "生成完成",
  FAILED: "生成失败",
  CANCELLED: "已取消",
  STALE_DOCUMENT: "文档已更新",
};

/**
 * 对话里的报告任务卡片：显示状态、在线补充问答、完成后提供下载。
 * 报告任务由聊天上传附件时创建，用户不必再去文档详情页。
 */
export function ChatReportCard({ runId }) {
  const [run, setRun] = useState(null);
  const [report, setReport] = useState(null);
  const [questions, setQuestions] = useState([]);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [downloadingId, setDownloadingId] = useState(null);
  const [retrying, setRetrying] = useState(false);
  const mounted = useRef(true);

  const load = useCallback(async ({ signal } = {}) => {
    if (!runId) return;
    try {
      const current = await getReportRun(runId, { signal });
      if (!mounted.current) return;
      setRun(current);
      if (current.state === "SUCCEEDED") {
        setQuestions([]);
        setReport(await getGeneratedReport(runId, { signal }));
      } else if (current.state === "NEEDS_INPUT") {
        const items = await listReportQuestions(runId, { signal });
        if (mounted.current) setQuestions((items || []).filter((item) => item.status === "OPEN"));
      }
    } catch (requestError) {
      if (requestError?.name === "AbortError") return;
      if (mounted.current) setError(requestError instanceof Error ? requestError.message : "报告状态读取失败");
    }
  }, [runId]);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void load({ signal: controller.signal });
    return () => {
      mounted.current = false;
      controller.abort();
    };
  }, [load]);

  useEffect(() => {
    if (!run || !ACTIVE_STATES.has(run.state)) return undefined;
    const timer = setInterval(() => { void load(); }, 5000);
    return () => clearInterval(timer);
  }, [run, load]);

  async function handleAnswers(answers) {
    setSubmitting(true);
    setError("");
    try {
      await answerReportQuestions(runId, answers);
      setQuestions([]);
      await load();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "补充信息提交失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRetry() {
    setRetrying(true);
    setError("");
    try {
      setRun(await retryReportRun(runId));
      await load();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "重试失败");
    } finally {
      setRetrying(false);
    }
  }

  async function handleDownload(artifact) {
    setDownloadingId(artifact.id);
    setError("");
    try {
      const { blob, filename } = await downloadReportArtifact(runId, artifact.id);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename || "报告";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "报告下载失败");
    } finally {
      setDownloadingId(null);
    }
  }

  if (!run) {
    return error ? <div className="chat-report-card"><p className="form-error" role="alert">{error}</p></div> : null;
  }

  const active = ACTIVE_STATES.has(run.state);
  const documentHref = `/datasets/${run.dataset_id}/documents/${run.document_id}`;

  return (
    <div className="chat-report-card">
      <div className="chat-report-card__head">
        <span className="chat-report-card__icon">
          {run.state === "SUCCEEDED" ? <CheckCircle2 size={18} /> : active ? <Loader2 className="spin" size={18} /> : <AlertCircle size={18} />}
        </span>
        <div>
          <strong>{run.report_type} 报告任务</strong>
          <small>{STATE_LABELS[run.state] || run.state}</small>
        </div>
        <a className="chat-report-card__link" href={documentHref}>查看源文档</a>
      </div>

      {run.state === "SUCCEEDED" && report?.report_ir ? (
        <>
          <p className="chat-report-card__meta">{report.report_ir.sections?.length || 0} 个章节 · 在线预览见源文档页</p>
          {report.artifacts?.length ? (
            <div className="chat-report-card__actions">
              {report.artifacts.map((artifact) => (
                <button
                  key={artifact.id}
                  type="button"
                  className="button button--secondary"
                  disabled={Boolean(downloadingId)}
                  onClick={() => { void handleDownload(artifact); }}
                >
                  {downloadingId === artifact.id ? <Loader2 className="spin" size={15} /> : <Download size={15} />}
                  {artifact.artifact_type === "DOCX" ? "下载 Word" : "下载 Markdown"}
                </button>
              ))}
            </div>
          ) : (
            <p className="chat-report-card__meta">本次任务未包含可下载产物（旧任务可重新生成以获得 Word/Markdown）。</p>
          )}
        </>
      ) : null}

      {run.state === "NEEDS_INPUT" && questions.length ? (
        <ReportQuestionsForm questions={questions} submitting={submitting} error={error} onSubmit={handleAnswers} />
      ) : null}

      {["FAILED", "CANCELLED", "STALE_DOCUMENT"].includes(run.state) ? (
        <div className="chat-report-card__actions">
          <p className="form-error" role="alert">{run.error_message || STATE_LABELS[run.state]}</p>
          {run.state === "FAILED" ? (
            <button type="button" className="button button--secondary" disabled={retrying} onClick={() => { void handleRetry(); }}>
              {retrying ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
              {retrying ? "正在重试" : "重试生成"}
            </button>
          ) : null}
        </div>
      ) : null}

      {active ? <p className="chat-report-card__meta"><FileOutput size={13} /> 任务在后台运行，可以离开本页</p> : null}
      {error && run.state !== "NEEDS_INPUT" && !["FAILED", "CANCELLED", "STALE_DOCUMENT"].includes(run.state) ? (
        <p className="form-error" role="alert">{error}</p>
      ) : null}
    </div>
  );
}

export default ChatReportCard;
