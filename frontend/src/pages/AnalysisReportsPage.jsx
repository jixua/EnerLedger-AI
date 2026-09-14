import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  BookOpen,
  FileChartColumn,
  FileText,
  Loader2,
  RefreshCw,
  Search,
} from "lucide-react";
import { Link } from "react-router-dom";

import { listDocumentAnalysisReports } from "../lib/api";
import { useApp } from "../state/AppContext";

function documentIdOf(document) {
  return document?.document_id ?? document?.documentId ?? document?.id;
}

function datasetIdOf(document) {
  return document?.dataset_id ?? document?.datasetId;
}

function reportTitle(filename) {
  const normalized = String(filename || "文档").trim();
  const stem = normalized.replace(/\.[^.]+$/, "");
  return `${stem || "文档"}分析报告`;
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
}

function previewReports(datasets, documents) {
  const datasetById = new Map(datasets.map((dataset) => [Number(dataset.id), dataset]));
  return Object.values(documents || {})
    .flatMap((items) => (Array.isArray(items) ? items : []))
    .filter((document) => String(document.status || "").toUpperCase() === "READY")
    .slice(0, 3)
    .map((document) => {
      const datasetId = Number(datasetIdOf(document));
      return {
        document_id: Number(documentIdOf(document)),
        dataset_id: datasetId,
        dataset_name: datasetById.get(datasetId)?.name || `知识库 #${datasetId}`,
        document_version: Number(document.version || 1),
        filename: document.filename,
        model_name: "预览模型",
        model_config_id: 0,
        analyzed_chunk_count: Number(document.chunk_count || 0),
        evidence_batch_count: 1,
        source_count: Math.min(Number(document.chunk_count || 0), 8),
        generated_at: document.updated_at || document.created_at,
        preview: true,
      };
    });
}

export function AnalysisReportsPage() {
  const { datasets = [], documents = {}, isDemo } = useApp();
  const previewStateRef = useRef({ datasets, documents });
  previewStateRef.current = { datasets, documents };
  const [reports, setReports] = useState([]);
  const [unavailableCount, setUnavailableCount] = useState(0);
  const [keyword, setKeyword] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  const loadReports = useCallback(async ({ signal, refresh = false } = {}) => {
    if (refresh) setRefreshing(true);
    else setLoading(true);
    setError("");
    try {
      if (isDemo) {
        setReports(previewReports(
          previewStateRef.current.datasets,
          previewStateRef.current.documents,
        ));
        setUnavailableCount(0);
        return;
      }
      const response = await listDocumentAnalysisReports({ signal });
      setReports(Array.isArray(response?.items) ? response.items : []);
      setUnavailableCount(Number(response?.unavailable_count || 0));
    } catch (requestError) {
      if (requestError?.name === "AbortError") return;
      setError(requestError instanceof Error ? requestError.message : "分析报告加载失败");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [isDemo]);

  useEffect(() => {
    const controller = new AbortController();
    void loadReports({ signal: controller.signal });
    return () => controller.abort();
  }, [loadReports]);

  const filteredReports = useMemo(() => {
    const normalizedKeyword = keyword.trim().toLowerCase();
    if (!normalizedKeyword) return reports;
    return reports.filter((report) => (
      `${reportTitle(report.filename)} ${report.filename || ""} ${report.dataset_name || ""} ${report.model_name || ""}`
        .toLowerCase()
        .includes(normalizedKeyword)
    ));
  }, [keyword, reports]);

  return (
    <div className="page page--analysis-reports feature-page">
      <header className="knowledge-hero">
        <div className="knowledge-hero__copy">
          <p className="eyebrow">Generated document reports</p>
          <h1>分析报告</h1>
          <p className="knowledge-hero__subtitle">{reports.length} 份已生成报告</p>
        </div>
        <button
          type="button"
          className="button button--secondary knowledge-hero__action"
          onClick={() => { void loadReports({ refresh: true }); }}
          disabled={refreshing}
        >
          <RefreshCw className={refreshing ? "spin" : ""} size={16} />
          {refreshing ? "刷新中" : "刷新报告"}
        </button>
      </header>

      {error ? <div className="notice notice--error"><AlertCircle size={16} /><p>{error}</p></div> : null}
      {unavailableCount > 0 ? (
        <div className="notice notice--warning">
          <AlertCircle size={16} />
          <p>{unavailableCount} 份报告的元数据暂时不可读取，其余报告已正常展示。</p>
        </div>
      ) : null}

      <section className="panel panel--flush">
        <div className="report-index-toolbar">
          <div className="toolbar__summary"><FileChartColumn size={15} /><span>按最近生成时间排序</span><span className="count-badge">{filteredReports.length}</span></div>
          <label className="search-field search-field--compact"><Search size={15} /><input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索报告、源文件或知识库" /></label>
        </div>

        {loading && reports.length === 0 ? (
          <div className="empty-state empty-state--loading"><Loader2 className="spin" size={21} /><p>正在汇总分析报告...</p></div>
        ) : filteredReports.length === 0 ? (
          <div className="empty-state empty-state--compact"><FileChartColumn size={22} /><h3>{reports.length ? "没有匹配的分析报告" : "暂时没有分析报告"}</h3><p>{reports.length ? "请调整搜索条件。" : "在文档详情中生成分析报告后，会集中显示在这里。"}</p></div>
        ) : (
          <div className="data-table-wrap">
            <table className="data-table analysis-report-table">
              <thead><tr><th>分析报告</th><th>所属知识库</th><th>源文件</th><th>分析范围</th><th>生成时间</th><th>操作</th></tr></thead>
              <tbody>
                {filteredReports.map((report) => (
                  <tr key={`${report.document_id}:${report.document_version}`}>
                    <td>
                      <div className="table-primary-cell report-title-cell"><span className="file-icon"><FileChartColumn size={15} /></span><span><strong>{reportTitle(report.filename)}</strong><small>{report.model_name || "未知模型"}</small></span></div>
                    </td>
                    <td><Link className="report-reference-link" to={`/datasets/${report.dataset_id}`}><BookOpen size={14} /><span>{report.dataset_name || `知识库 #${report.dataset_id}`}</span></Link></td>
                    <td><Link className="report-reference-link" to={`/datasets/${report.dataset_id}/documents/${report.document_id}`}><FileText size={14} /><span>{report.filename || `文档 #${report.document_id}`}</span></Link></td>
                    <td><div className="table-detail-stack"><span>{report.analyzed_chunk_count} 个主体片段</span><small>{report.source_count} 条引用 · {report.evidence_batch_count} 批证据</small></div></td>
                    <td>{formatTime(report.generated_at)}</td>
                    <td><Link className="button button--tiny" to={`/datasets/${report.dataset_id}/documents/${report.document_id}/analysis`}>查看报告</Link></td>
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
