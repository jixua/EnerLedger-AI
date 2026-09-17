import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  CircleSlash,
  FileText,
  Loader2,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { ReportArtifactButtons } from "../components/ReportArtifactButtons";
import { ReportIrView } from "../components/ReportIrView";
import { ReportQuestionsForm } from "../components/ReportQuestionsForm";
import {
  answerReportQuestions,
  cancelReportRun,
  getGeneratedReport,
  getReportRun,
  listReportQuestions,
  listReportTemplates,
  retryReportRun,
} from "../lib/api";
import {
  FAILED_REPORT_STATES,
  fieldStatusLabel,
  formatReportTime,
  isActiveReportRun,
  reportSourceDocumentPath,
  reportStateLabel,
  reportStateTone,
  reportTypeName,
  unresolvedFields,
} from "../lib/reportRun";

/**
 * 报告详情页：报告正文的唯一落脚点。
 *
 * 此前在线正文只在文档详情页的「生成报告」对话框里渲染，且表格会退化成 JSON。
 * 现在生成入口只负责创建任务，「看报告」统一走这里。
 */
export function ReportDetailPage() {
  const { runId } = useParams();
  const [run, setRun] = useState(null);
  const [detail, setDetail] = useState(null);
  const [templates, setTemplates] = useState([]);
  const [questions, setQuestions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async ({ signal } = {}) => {
    setError("");
    try {
      const current = await getReportRun(runId, { signal });
      setRun(current);
      if (current.state === "SUCCEEDED") {
        setQuestions([]);
        setDetail(await getGeneratedReport(runId, { signal }));
      } else {
        setDetail(null);
        if (current.state === "NEEDS_INPUT") {
          const items = await listReportQuestions(runId, { signal });
          setQuestions((items || []).filter((question) => question.status === "OPEN"));
        } else {
          setQuestions([]);
        }
      }
    } catch (requestError) {
      if (requestError?.name === "AbortError") return;
      setError(requestError instanceof Error ? requestError.message : "报告加载失败");
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    const controller = new AbortController();
    void load({ signal: controller.signal });
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    const controller = new AbortController();
    listReportTemplates({ signal: controller.signal })
      .then((items) => setTemplates(Array.isArray(items) ? items : []))
      .catch(() => { /* 模板名只影响标题，缺失时退回报告类型 */ });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!isActiveReportRun(run?.state)) return undefined;
    const timer = window.setInterval(() => { void load(); }, 5000);
    return () => window.clearInterval(timer);
  }, [run?.state, load]);

  const template = useMemo(
    () => templates.find((item) => item.report_type === run?.report_type) || null,
    [templates, run?.report_type],
  );

  const fieldLabels = useMemo(() => {
    const map = new Map();
    for (const field of template?.fields || []) map.set(field.field_id, field.label);
    return map;
  }, [template]);

  const reportIr = detail?.report_ir || null;
  const artifacts = detail?.artifacts || [];
  const pendingFields = useMemo(() => unresolvedFields(reportIr), [reportIr]);
  const ledger = useMemo(() => reportIr?.field_ledger || [], [reportIr]);

  async function handleAnswers(answers) {
    setBusy(true);
    setActionError("");
    try {
      await answerReportQuestions(runId, answers);
      setQuestions([]);
      await load();
    } catch (requestError) {
      setActionError(requestError instanceof Error ? requestError.message : "补充信息提交失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleRetry() {
    setBusy(true);
    setActionError("");
    try {
      setRun(await retryReportRun(runId));
      await load();
    } catch (requestError) {
      setActionError(requestError instanceof Error ? requestError.message : "重试失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleCancel() {
    setBusy(true);
    setActionError("");
    try {
      setRun(await cancelReportRun(runId));
      await load();
    } catch (requestError) {
      setActionError(requestError instanceof Error ? requestError.message : "取消失败");
    } finally {
      setBusy(false);
    }
  }

  if (loading && !run) {
    return (
      <div className="page">
        <div className="empty-state empty-state--loading"><Loader2 className="spin" size={21} /><p>正在读取报告…</p></div>
      </div>
    );
  }

  if (!run) {
    return (
      <div className="page">
        <div className="empty-state">
          <AlertCircle size={22} />
          <h1>未找到报告任务</h1>
          <p>{error || "任务不存在，或不属于当前用户。"}</p>
          <Link className="button button--secondary" to="/reports">返回报告中心</Link>
        </div>
      </div>
    );
  }

  const sourcePath = reportSourceDocumentPath(run);
  const failed = FAILED_REPORT_STATES.has(run.state);
  const title = reportTypeName(run);

  return (
    <div className="page page--report-detail">
      <header className="document-detail-header report-detail__header">
        <div className="document-detail-header__main">
          <Link className="icon-button" to="/reports" aria-label="返回报告中心"><ArrowLeft size={18} /></Link>
          <div className="document-detail-header__identity">
            <div className="document-detail-title-line">
              <h1>{title}</h1>
              <span className={`report-state ${reportStateTone(run.state)}`}>
                {reportStateLabel(run.state)}
              </span>
            </div>
          </div>
        </div>
        <div className="document-detail-header__actions">
          {/* 返回箭头已经回报告中心，这里不再放一个同目标的按钮 */}
          {sourcePath ? <Link className="button button--secondary" to={sourcePath}><FileText size={15} />源文档</Link> : null}
          {run.state === "SUCCEEDED" ? (
            <ReportArtifactButtons
              runId={run.run_id}
              artifacts={artifacts}
              variant="menu"
              emptyHint="该任务创建时未选择可下载格式，可在源文档页重新生成。"
            />
          ) : null}
          {run.state === "FAILED" ? (
            <button type="button" className="button button--primary" onClick={() => { void handleRetry(); }} disabled={busy}>
              {busy ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}重试生成
            </button>
          ) : null}
          {isActiveReportRun(run.state) || run.state === "NEEDS_INPUT" ? (
            <button type="button" className="button button--secondary" onClick={() => { void handleCancel(); }} disabled={busy}>
              <CircleSlash size={15} />取消任务
            </button>
          ) : null}
        </div>
      </header>

      <section className="document-detail-meta" aria-label="报告任务信息">
        {/* 标题本身就是模板名称，这里只留版本；空值不再用破折号占位 */}
        <span><strong>模板版本</strong>v{run.template_version}</span>
        <span><strong>文档版本</strong>v{run.document_version}</span>
        {run.reporting_year ? <span><strong>报告年度</strong>{run.reporting_year}</span> : null}
        <span><strong>创建时间</strong>{formatReportTime(run.created_at)}</span>
        {run.finished_at ? <span><strong>完成时间</strong>{formatReportTime(run.finished_at)}</span> : null}
        <span><strong>任务号</strong><code>{String(run.run_id).slice(0, 8)}</code></span>
      </section>

      {error ? <div className="notice notice--error"><AlertCircle size={16} /><p>{error}</p></div> : null}
      {actionError ? <div className="notice notice--error"><AlertCircle size={16} /><p>{actionError}</p></div> : null}
      {run.error_message ? (
        <div className="notice notice--error"><ShieldAlert size={16} /><p>{run.error_message}</p></div>
      ) : null}

      {isActiveReportRun(run.state) ? (
        <section className="panel report-detail__pending">
          <Loader2 className="spin" size={20} />
          <div>
            <h2>正在生成报告</h2>
            <p>当前阶段：{run.stage}。任务在后台运行，可以离开本页，稍后回来查看。</p>
          </div>
        </section>
      ) : null}

      {run.state === "NEEDS_INPUT" ? (
        <section className="panel report-detail__questions">
          <ReportQuestionsForm
            questions={questions}
            submitting={busy}
            error={actionError}
            onSubmit={handleAnswers}
          />
        </section>
      ) : null}

      {["CANCELLED", "STALE_DOCUMENT"].includes(run.state) ? (
        <section className="panel report-detail__pending">
          <AlertCircle size={20} />
          <div>
            <h2>{run.state === "CANCELLED" ? "任务已取消" : "源文档已更新"}</h2>
            <p>
              {run.state === "CANCELLED"
                ? "该任务已停止，不会继续生成。可以在源文档页重新发起。"
                : "报告冻结的文档版本已变化，本次结果不再适用。请到源文档页重新创建报告。"}
            </p>
            {sourcePath ? <Link className="button button--secondary" to={sourcePath}>前往源文档</Link> : null}
          </div>
        </section>
      ) : null}

      {run.state === "SUCCEEDED" ? (
        <>
          <section className="panel panel--flush report-detail__body">
            {reportIr ? <ReportIrView reportIr={reportIr} /> : (
              <div className="empty-state empty-state--loading"><Loader2 className="spin" size={20} /><p>正在渲染报告…</p></div>
            )}
          </section>

          {/* 待确认与限制是读完之后再看的附录：放在正文之前会把正文挤出首屏
              （桌面 900px 视口下正文原本从 844px 才开始）。 */}
          {pendingFields.length || reportIr?.limitations?.length || reportIr?.warnings?.length ? (
            <section className="panel report-detail__gaps">
              <h2>待确认与限制</h2>
              {pendingFields.length ? (
                <>
                  <p className="report-detail__gaps-lead">
                    以下 {pendingFields.length} 项在现有材料中未能落实，正文相应位置按资料缺口处理。
                  </p>
                  <ul className="report-gap-list">
                    {pendingFields.map((item) => (
                      <li key={item.field_id}>
                        <strong>{fieldLabels.get(item.field_id) || item.field_id}</strong>
                        <span className={`report-gap-tag is-${String(item.status).toLowerCase()}`}>
                          {fieldStatusLabel(item.status)}
                        </span>
                        {item.notes?.length ? <small>{item.notes.join("；")}</small> : null}
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}
              {reportIr?.warnings?.length ? (
                <div className="report-detail__gaps-group">
                  <h3>生成提示</h3>
                  <ul>{reportIr.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
                </div>
              ) : null}
              {reportIr?.limitations?.length ? (
                <div className="report-detail__gaps-group">
                  <h3>使用限制</h3>
                  <ul>{reportIr.limitations.map((item) => <li key={item}>{item}</li>)}</ul>
                </div>
              ) : null}
            </section>
          ) : null}

          {ledger.length ? (
            <details className="panel report-detail__ledger">
              <summary>字段台账（{ledger.length} 项）</summary>
              <div className="data-table-wrap">
                <table className="data-table report-table">
                  <thead>
                    <tr><th>字段</th><th>状态</th><th>取值</th><th>单位</th><th>证据</th></tr>
                  </thead>
                  <tbody>
                    {ledger.map((item) => (
                      <tr key={item.field_id}>
                        <td>{fieldLabels.get(item.field_id) || item.field_id}</td>
                        <td>{fieldStatusLabel(item.status)}</td>
                        <td>{item.value === null || item.value === undefined ? "—" : String(item.value)}</td>
                        <td>{item.unit || "—"}</td>
                        <td>{item.evidence_ids?.length ? item.evidence_ids.join("、") : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          ) : null}
        </>
      ) : null}

      {failed && run.state === "FAILED" ? (
        <section className="panel report-detail__pending">
          <AlertCircle size={20} />
          <div>
            <h2>报告未能生成</h2>
            <p>{run.error_code ? `原因代码：${run.error_code}。` : ""}可以直接重试；若源文档或材料有更新，请改为重新创建报告。</p>
          </div>
        </section>
      ) : null}
    </div>
  );
}

export default ReportDetailPage;
