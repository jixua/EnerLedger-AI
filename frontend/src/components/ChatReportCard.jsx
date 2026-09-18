import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, FileOutput, FileText, Loader2, RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";

import { ReportArtifactButtons } from "./ReportArtifactButtons";
import { ReportQuestionsForm } from "./ReportQuestionsForm";
import {
  answerReportQuestions,
  getGeneratedReport,
  getReportRun,
  listReportQuestions,
  retryReportRun,
} from "../lib/api";
import {
  formatReportTime,
  isActiveReportRun,
  reportRunPath,
  reportSourceDocumentPath,
  reportStateLabel,
  reportTypeName,
} from "../lib/reportRun";

/**
 * 对话里的报告任务卡片：显示状态、在线补充问答、完成后给出报告入口。
 * 正文一律进报告详情页看，卡片只做索引，不再重复渲染一份缩略版。
 */
export function ChatReportCard({ runId }) {
  const [run, setRun] = useState(null);
  const [detail, setDetail] = useState(null);
  const [questions, setQuestions] = useState([]);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
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
        setDetail(await getGeneratedReport(runId, { signal }));
      } else {
        setDetail(null);
        if (current.state === "NEEDS_INPUT") {
          const items = await listReportQuestions(runId, { signal });
          if (mounted.current) setQuestions((items || []).filter((item) => item.status === "OPEN"));
        } else if (mounted.current) {
          setQuestions([]);
        }
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
    if (!isActiveReportRun(run?.state)) return undefined;
    const timer = window.setInterval(() => { void load(); }, 5000);
    return () => window.clearInterval(timer);
  }, [run?.state, load]);

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

  if (!run) {
    return error ? <div className="chat-report-card"><p className="form-error" role="alert">{error}</p></div> : null;
  }

  const active = isActiveReportRun(run.state);
  const sourcePath = reportSourceDocumentPath(run);
  const reportPath = reportRunPath(run.run_id);

  return (
    <div className="chat-report-card">
      <div className="chat-report-card__head">
        <span className="chat-report-card__icon">
          {run.state === "SUCCEEDED" ? <CheckCircle2 size={18} /> : active ? <Loader2 className="spin" size={18} /> : <AlertCircle size={18} />}
        </span>
        <div>
          <strong>{reportTypeName(run)}</strong>
          <small>{reportStateLabel(run.state)} · {formatReportTime(run.created_at)}</small>
        </div>
        {sourcePath ? <Link className="chat-report-card__link" to={sourcePath}>查看来源文档</Link> : null}
      </div>

      {run.state === "SUCCEEDED" ? (
        // 「打开报告」和两个下载按钮并排，用同一套按钮样式——它原来是一行 10px 的小字链接，
        // 跟旁边的下载按钮差一截，看着不像同一组操作。
        <div className="chat-report-card__actions">
          <Link className="button button--secondary" to={reportPath}>
            <FileText size={15} />
            打开报告
          </Link>
          <ReportArtifactButtons
            runId={run.run_id}
            artifacts={detail?.artifacts}
            emptyHint="该任务创建时未选择可下载格式，可在报告页查看正文。"
          />
        </div>
      ) : null}

      {run.state === "NEEDS_INPUT" && questions.length ? (
        <>
          <p className="chat-report-card__meta">
            报告里有 <Link className="chat-report-card__link" to={reportPath}>{questions.length} 项信息</Link> 需要你确认后才能继续。
          </p>
          <ReportQuestionsForm
            questions={questions}
            submitting={submitting}
            error={error}
            onSubmit={handleAnswers}
          />
        </>
      ) : null}

      {["FAILED", "CANCELLED", "STALE_DOCUMENT"].includes(run.state) ? (
        <div className="chat-report-card__actions">
          <p className="form-error" role="alert">{run.error_message || reportStateLabel(run.state)}</p>
          {run.state === "FAILED" ? (
            <button type="button" className="button button--secondary" disabled={retrying} onClick={() => { void handleRetry(); }}>
              {retrying ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
              {retrying ? "正在重试" : "重试生成"}
            </button>
          ) : null}
          <Link className="button button--secondary" to={reportPath}>查看详情</Link>
        </div>
      ) : null}

      {active ? (
        <p className="chat-report-card__meta">
          <FileOutput size={13} />
          任务在后台运行，可以离开本页；<Link className="chat-report-card__link" to={reportPath}>打开报告页</Link>
        </p>
      ) : null}

      {error && run.state !== "NEEDS_INPUT" && !["FAILED", "CANCELLED", "STALE_DOCUMENT"].includes(run.state) ? (
        <p className="form-error" role="alert">{error}</p>
      ) : null}
    </div>
  );
}

export default ChatReportCard;
