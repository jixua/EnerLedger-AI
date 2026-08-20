import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, ArrowLeft, BrainCircuit, FileSearch, FileText, Loader2 } from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { DocumentAnalysisPanel } from "../components/DocumentAnalysisPanel";
import { useApp } from "../state/AppContext";

function documentIdentity(document) {
  return document?.document_id ?? document?.documentId ?? document?.id;
}

function findDocument(documents, documentId) {
  const items = Array.isArray(documents) ? documents : Object.values(documents || {}).flat();
  return items.find((item) => Number(documentIdentity(item)) === Number(documentId)) || null;
}

function normalizedStatus(document) {
  const value = String(document?.status ?? "").toUpperCase();
  if (value === "READY" || value === "SUCCESS") return "READY";
  if (value === "FAILED") return "FAILED";
  if (["QUEUED", "PENDING", "WAITING"].includes(value)) return "QUEUED";
  return "PROCESSING";
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
}

export function DocumentAnalysisPage() {
  const params = useParams();
  const datasetId = Number(params.datasetId);
  const targetDocumentId = Number(params.documentId);
  const { datasets = [], documents = {}, loading = {}, actions = {} } = useApp();
  const contextDocument = useMemo(
    () => findDocument(documents, targetDocumentId),
    [documents, targetDocumentId],
  );
  const dataset = datasets.find((item) => Number(item.id) === datasetId);
  const [document, setDocument] = useState(contextDocument);
  const [loadingDocument, setLoadingDocument] = useState(!contextDocument);
  const [documentError, setDocumentError] = useState("");

  const routeIsValid = Number.isSafeInteger(datasetId)
    && datasetId > 0
    && Number.isSafeInteger(targetDocumentId)
    && targetDocumentId > 0;
  const routeMatchesDocument = !document
    || Number(document.dataset_id ?? document.datasetId) === datasetId;
  const status = normalizedStatus(document);
  const previewPath = `/datasets/${datasetId}/documents/${targetDocumentId}`;

  useEffect(() => {
    if (contextDocument) setDocument(contextDocument);
  }, [contextDocument]);

  const loadDocument = useCallback(async () => {
    if (!routeIsValid || !actions.loadDocument) return;
    setLoadingDocument(true);
    setDocumentError("");
    try {
      const next = await actions.loadDocument(targetDocumentId);
      if (Number(next.dataset_id ?? next.datasetId) !== datasetId) {
        throw new Error("文档不属于当前数据集");
      }
      setDocument(next);
    } catch (error) {
      setDocumentError(error instanceof Error ? error.message : "文档加载失败");
    } finally {
      setLoadingDocument(false);
    }
  }, [actions.loadDocument, datasetId, routeIsValid, targetDocumentId]);

  useEffect(() => {
    void loadDocument();
  }, [loadDocument]);

  if (!routeIsValid) {
    return <div className="page"><div className="empty-state"><AlertCircle size={24} /><h1>分析报告地址无效</h1><Link className="button button--secondary" to="/datasets">返回数据集</Link></div></div>;
  }

  const loadingDatasets = typeof loading === "boolean"
    ? loading
    : Boolean(loading.datasets || loading.initial);
  if ((loadingDocument || loadingDatasets) && !document) {
    return <div className="page"><div className="empty-state empty-state--loading"><Loader2 className="spin" size={22} /><p>正在读取文档分析信息…</p></div></div>;
  }

  if (!document || !routeMatchesDocument) {
    return <div className="page"><div className="empty-state"><FileText size={24} /><h1>未找到文档</h1><p>{documentError || "文档不存在，或不属于当前数据集。"}</p><Link className="button button--secondary" to={`/datasets/${datasetId}`}>返回数据集</Link></div></div>;
  }

  return (
    <div className="page page--document-analysis">
      <header className="document-detail-header document-analysis-page__header">
        <div className="document-detail-header__main">
          <Link className="icon-button" to={previewPath} aria-label="返回文档预览"><ArrowLeft size={18} /></Link>
          <span className="document-detail-file-icon" aria-hidden="true"><BrainCircuit size={22} /></span>
          <div className="document-detail-header__identity">
            <p className="eyebrow">分析报告 · {dataset?.name || `数据集 #${datasetId}`}</p>
            <div className="document-detail-title-line"><h1>{document.filename || `文档 #${targetDocumentId}`}</h1></div>
            <p>文档版本 v{document.version ?? 1} · 更新于 {formatTime(document.updated_at)} · 报告独立保存于 MinIO</p>
          </div>
        </div>
        <div className="document-detail-header__actions">
          <Link className="button button--secondary" to={previewPath}><FileSearch size={16} />文档与分片预览</Link>
        </div>
      </header>

      {documentError ? <div className="notice notice--error" role="alert"><AlertCircle size={16} /><p>{documentError}</p></div> : null}

      {status === "READY" ? (
        <DocumentAnalysisPanel
          document={document}
          loadDocumentAnalysis={actions.loadDocumentAnalysis}
          loadDocumentAnalysisStatus={actions.loadDocumentAnalysisStatus}
          analyzeDocument={actions.analyzeDocument}
          downloadDocumentAnalysisDocx={actions.downloadDocumentAnalysisDocx}
        />
      ) : (
        <section className="panel document-analysis-page__unavailable">
          <AlertCircle size={22} />
          <div><h2>当前文档暂时不能生成分析报告</h2><p>请等待文档解析和索引完成后，再从文档预览页进入分析报告。</p></div>
        </section>
      )}
    </div>
  );
}

export default DocumentAnalysisPage;
