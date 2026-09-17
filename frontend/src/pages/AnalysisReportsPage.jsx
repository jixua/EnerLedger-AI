import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  Download,
  FileText,
  Loader2,
  RefreshCw,
  Search,
  Sparkles,
} from "lucide-react";
import { Link } from "react-router-dom";

import { downloadReportArtifact, listReportRuns } from "../lib/api";
import { useApp } from "../state/AppContext";

const REPORT_STATE_LABELS = {
  PENDING: "等待处理",
  PROCESSING: "正在生成",
  NEEDS_INPUT: "等待补充信息",
  SUCCEEDED: "生成完成",
  FAILED: "生成失败",
  CANCELLED: "已取消",
  STALE_DOCUMENT: "文档已更新",
};

const REPORT_STATE_TONES = {
  SUCCEEDED: "is-done",
  FAILED: "is-failed",
  STALE_DOCUMENT: "is-failed",
  CANCELLED: "is-muted",
  NEEDS_INPUT: "is-waiting",
};

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
}

export function AnalysisReportsPage() {
  const { isDemo } = useApp();

  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [downloadingId, setDownloadingId] = useState(null);
  const [keyword, setKeyword] = useState("");

  const loadRuns = useCallback(async ({ signal, refresh = false } = {}) => {
    if (refresh) setRefreshing(true);
    else setLoading(true);
    setError("");
    try {
      if (isDemo) {
        setRuns([]);
        return;
      }
      const items = await listReportRuns({ limit: 100, signal });
      setRuns(Array.isArray(items) ? items : []);
    } catch (requestError) {
      if (requestError?.name === "AbortError") return;
      setError(requestError instanceof Error ? requestError.message : "报告任务加载失败");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [isDemo]);

  useEffect(() => {
    const controller = new AbortController();
    void loadRuns({ signal: controller.signal });
    return () => controller.abort();
  }, [loadRuns]);

  const filteredRuns = useMemo(() => {
    const normalizedKeyword = keyword.trim().toLowerCase();
    if (!normalizedKeyword) return runs;
    return runs.filter((run) => (
      `${run.document_filename || ""} ${run.report_type || ""} ${REPORT_STATE_LABELS[run.state] || run.state || ""}`
        .toLowerCase()
        .includes(normalizedKeyword)
    ));
  }, [keyword, runs]);

  async function handleDownload(run, artifact) {
    setDownloadingId(artifact.id);
    setError("");
    try {
      const { blob, filename } = await downloadReportArtifact(run.run_id, artifact.id);
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

  return (
    <div className="page page--analysis-reports feature-page">
      <header className="knowledge-hero">
        <div className="knowledge-hero__copy">
          <p className="eyebrow">REPORT CENTER</p>
          <h1>报告中心</h1>
          <p className="knowledge-hero__subtitle">
            {runs.length} 个报告任务 · 覆盖 R1–R7 全部报告类型
          </p>
        </div>
        <button
          type="button"
          className="button button--secondary knowledge-hero__action"
          onClick={() => { void loadRuns({ refresh: true }); }}
          disabled={refreshing}
        >
          <RefreshCw className={refreshing ? "spin" : ""} size={16} />
          {refreshing ? "刷新中" : "刷新"}
        </button>
      </header>

      {error ? <div className="notice notice--error"><AlertCircle size={16} /><p>{error}</p></div> : null}

      <section className="panel panel--flush">
        <div className="report-index-toolbar">
          <div className="toolbar__summary"><Sparkles size={15} /><span>按创建时间排序</span><span className="count-badge">{filteredRuns.length}</span></div>
          <label className="search-field search-field--compact"><Search size={15} /><input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索报告、源文件或状态" /></label>
        </div>

        {loading && runs.length === 0 ? (
          <div className="empty-state empty-state--loading"><Loader2 className="spin" size={21} /><p>正在汇总报告任务...</p></div>
        ) : filteredRuns.length === 0 ? (
          <div className="empty-state empty-state--compact">
            <Sparkles size={22} />
            <h3>{runs.length ? "没有匹配的报告任务" : "还没有生成报告"}</h3>
            <p>{runs.length ? "请调整搜索条件。" : "在对话中上传材料，或在文档详情页点击「生成报告」，任务会集中显示在这里。"}</p>
          </div>
        ) : (
          <div className="data-table-wrap">
            <table className="data-table analysis-report-table">
              <thead><tr><th>报告</th><th>源文件</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
              <tbody>
                {filteredRuns.map((run) => (
                  <tr key={run.run_id}>
                    <td>
                      <div className="table-primary-cell report-title-cell">
                        <span className="file-icon"><Sparkles size={15} /></span>
                        <span><strong>{run.report_type} 报告</strong><small>任务 {String(run.run_id).slice(0, 8)}</small></span>
                      </div>
                    </td>
                    <td>
                      <Link className="report-reference-link" to={`/datasets/${run.dataset_id}/documents/${run.document_id}`}>
                        <FileText size={14} /><span>{run.document_filename || `文档 #${run.document_id}`}</span>
                      </Link>
                    </td>
                    <td>
                      <span className={`report-state ${REPORT_STATE_TONES[run.state] || ""}`}>
                        {REPORT_STATE_LABELS[run.state] || run.state}
                      </span>
                      {run.error_message ? <small className="report-state__detail">{run.error_message}</small> : null}
                    </td>
                    <td>{formatTime(run.created_at)}</td>
                    <td>
                      <div className="report-run-actions">
                        {(run.artifacts || []).map((artifact) => (
                          <button
                            key={artifact.id}
                            type="button"
                            className="button button--tiny"
                            disabled={Boolean(downloadingId)}
                            onClick={() => { void handleDownload(run, artifact); }}
                          >
                            {downloadingId === artifact.id ? <Loader2 className="spin" size={13} /> : <Download size={13} />}
                            {artifact.artifact_type === "DOCX" ? "Word" : "Markdown"}
                          </button>
                        ))}
                        <Link className="button button--tiny" to={`/datasets/${run.dataset_id}/documents/${run.document_id}`}>查看源文档</Link>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

export default AnalysisReportsPage;
