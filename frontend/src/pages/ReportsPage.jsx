import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  FileText,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Sparkles,
} from "lucide-react";
import { Link } from "react-router-dom";

import { ReportArtifactButtons } from "../components/ReportArtifactButtons";
import { listReportRuns, listReportTemplates } from "../lib/api";
import { useApp } from "../state/AppContext";
import {
  formatReportTime,
  isActiveReportRun,
  reportRunPath,
  reportSourceDocumentPath,
  reportStateLabel,
  reportStateTone,
} from "../lib/reportRun";

const FILTERS = [
  { key: "ALL", label: "全部" },
  { key: "ACTIVE", label: "进行中" },
  { key: "DONE", label: "已完成" },
  { key: "ATTENTION", label: "需要处理" },
];

function matchesFilter(run, filter) {
  if (filter === "ACTIVE") return isActiveReportRun(run.state);
  if (filter === "DONE") return run.state === "SUCCEEDED";
  if (filter === "ATTENTION") return ["NEEDS_INPUT", "FAILED", "CANCELLED", "STALE_DOCUMENT"].includes(run.state);
  return true;
}

/**
 * 报告中心：跨文档汇总报告任务。
 *
 * 这里只做索引与下载入口，正文一律进报告详情页看——生成、存放、查看三件事
 * 各有明确归属，不再互相嵌套。
 */
export function ReportsPage() {
  const { isDemo } = useApp();
  const [runs, setRuns] = useState([]);
  const [templates, setTemplates] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [keyword, setKeyword] = useState("");
  const [filter, setFilter] = useState("ALL");

  const load = useCallback(async ({ signal, refresh = false } = {}) => {
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
    void load({ signal: controller.signal });
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    const controller = new AbortController();
    listReportTemplates({ signal: controller.signal })
      .then((items) => setTemplates(Array.isArray(items) ? items : []))
      .catch(() => { /* 模板名缺失时退回报告类型 */ });
    return () => controller.abort();
  }, []);

  const templateNames = useMemo(() => {
    const map = new Map();
    for (const template of templates) map.set(template.report_type, template.name);
    return map;
  }, [templates]);

  const visibleRuns = useMemo(() => {
    const normalized = keyword.trim().toLowerCase();
    return runs.filter((run) => {
      if (!matchesFilter(run, filter)) return false;
      if (!normalized) return true;
      return [
        run.report_type,
        run.document_filename,
        templateNames.get(run.report_type),
        reportStateLabel(run.state),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(normalized);
    });
  }, [filter, keyword, runs, templateNames]);

  const counts = useMemo(
    () => Object.fromEntries(
      FILTERS.map(({ key }) => [key, runs.filter((run) => matchesFilter(run, key)).length]),
    ),
    [runs],
  );

  return (
    <div className="page page--reports feature-page">
      <header className="knowledge-hero">
        <div className="knowledge-hero__copy">
          <p className="eyebrow">REPORT CENTER</p>
          <h1>报告中心</h1>
          <p className="knowledge-hero__subtitle">
            {runs.length} 个报告任务 · 覆盖 R1–R7 全部报告类型
          </p>
        </div>
        <div className="knowledge-hero__actions">
          <button
            type="button"
            className="button button--secondary knowledge-hero__action"
            onClick={() => { void load({ refresh: true }); }}
            disabled={refreshing}
          >
            <RefreshCw className={refreshing ? "spin" : ""} size={16} />
            {refreshing ? "刷新中" : "刷新"}
          </button>
          <Link className="button button--primary knowledge-hero__action" to="/datasets">
            <Plus size={16} />新建报告
          </Link>
        </div>
      </header>

      {error ? <div className="notice notice--error"><AlertCircle size={16} /><p>{error}</p></div> : null}

      <section className="panel panel--flush">
        <div className="report-index-toolbar">
          <div className="filter-tabs" role="tablist" aria-label="报告状态筛选">
            {FILTERS.map(({ key, label }) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={filter === key}
                className={`filter-tab${filter === key ? " filter-tab--active" : ""}`}
                onClick={() => setFilter(key)}
              >
                {label}<span>{counts[key]}</span>
              </button>
            ))}
          </div>
          <label className="search-field search-field--compact">
            <Search size={15} />
            <input
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder="搜索报告、源文件或状态"
            />
          </label>
        </div>

        {loading && runs.length === 0 ? (
          <div className="empty-state empty-state--loading"><Loader2 className="spin" size={21} /><p>正在汇总报告任务...</p></div>
        ) : visibleRuns.length === 0 ? (
          <div className="empty-state empty-state--compact">
            <Sparkles size={22} />
            <h3>{runs.length ? "没有匹配的报告任务" : "还没有生成报告"}</h3>
            <p>
              {runs.length
                ? "请调整筛选条件或搜索关键词。"
                : "在对话中上传材料会自动创建报告任务；也可以在「碳知识库」里打开文档，点击「生成报告」。"}
            </p>
          </div>
        ) : (
          <div className="data-table-wrap">
            <table className="data-table report-index-table">
              <thead>
                <tr><th>报告</th><th>源文件</th><th>状态</th><th>创建时间</th><th>操作</th></tr>
              </thead>
              <tbody>
                {visibleRuns.map((run) => {
                  const sourcePath = reportSourceDocumentPath(run);
                  return (
                    <tr key={run.run_id}>
                      <td>
                        <div className="table-primary-cell report-title-cell">
                          <span className="file-icon"><Sparkles size={15} /></span>
                          <span>
                            <Link to={reportRunPath(run.run_id)}>
                              {templateNames.get(run.report_type) || `${run.report_type} 报告`}
                            </Link>
                            <small>{run.report_type} · 任务 {String(run.run_id).slice(0, 8)}</small>
                          </span>
                        </div>
                      </td>
                      <td>
                        {sourcePath ? (
                          <Link className="report-reference-link" to={sourcePath}>
                            <FileText size={14} /><span>{run.document_filename || `文档 #${run.document_id}`}</span>
                          </Link>
                        ) : <span className="muted-copy">—</span>}
                      </td>
                      <td>
                        <span className={`report-state ${reportStateTone(run.state)}`}>
                          {reportStateLabel(run.state)}
                        </span>
                        {run.error_message ? <small className="report-state__detail">{run.error_message}</small> : null}
                      </td>
                      <td>{formatReportTime(run.created_at)}</td>
                      <td>
                        <div className="report-run-actions">
                          <Link className="button button--tiny button--primary" to={reportRunPath(run.run_id)}>
                            查看报告
                          </Link>
                          <ReportArtifactButtons
                            runId={run.run_id}
                            artifacts={run.artifacts}
                            className="report-artifacts--inline"
                          />
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

export default ReportsPage;
