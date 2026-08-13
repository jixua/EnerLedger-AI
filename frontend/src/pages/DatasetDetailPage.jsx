import { useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  Clock3,
  Database,
  Eye,
  FileText,
  Layers3,
  Loader2,
  Pencil,
  Play,
  RefreshCw,
  RotateCw,
  Search,
  Settings2,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { UploadDialog } from "../components/UploadDialog";
import { formatDuration, resolveDocumentDurationMs } from "../lib/document-duration";
import { isDocumentRetrievalReady } from "../lib/parse-quality";
import { useApp } from "../state/AppContext";

const TABS = [
  { id: "documents", label: "文档", icon: FileText },
  { id: "recall", label: "检索测试", icon: Search },
  { id: "settings", label: "设置", icon: Settings2 },
];

function documentId(document) {
  return document?.document_id ?? document?.documentId ?? document?.id;
}

function documentsForDataset(documents, datasetId) {
  if (Array.isArray(documents)) {
    return documents.filter((document) => Number(document.dataset_id ?? document.datasetId) === Number(datasetId));
  }
  return documents?.[datasetId] || documents?.[String(datasetId)] || [];
}

function modelId(model) {
  return model?.id ?? model?.config_id ?? model?.configId;
}

function modelLabel(model) {
  const label = model?.display_name || model?.displayName || model?.model_name || model?.modelName || `模型 #${modelId(model)}`;
  return model?.is_active === false ? `${label}（已停用）` : label;
}

function formatBytes(bytes) {
  if (!Number.isFinite(Number(bytes))) return "—";
  const value = Number(bytes);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
}

function normalizedStatus(document) {
  const value = String(document?.status ?? document ?? "").toUpperCase();
  if (value === "READY" || value === "SUCCESS") return "READY";
  if (value === "FAILED") return "FAILED";
  if (value === "QUEUED" || value === "PENDING" || value === "WAITING") return "QUEUED";
  return "PROCESSING";
}

function canRetryDocument(document, status) {
  if (status === "FAILED") return true;
  if (status !== "PROCESSING") return false;
  if (!document.lease_expires_at) return true;
  return new Date(document.lease_expires_at).getTime() <= Date.now();
}

function statusMeta(document) {
  const status = normalizedStatus(document);
  if (status === "READY") return { label: "可检索", modifier: "ready", icon: CheckCircle2 };
  if (status === "FAILED") return { label: "失败", modifier: "failed", icon: AlertCircle };
  if (status === "QUEUED" && Number(document?.attempt_count || 0) > 0) return { label: "待重试", modifier: "retry", icon: Clock3 };
  if (status === "QUEUED") return { label: "排队中", modifier: "queued", icon: Clock3 };
  return { label: "处理中", modifier: "processing", icon: Loader2 };
}

function StatusPill({ document }) {
  const meta = statusMeta(document);
  const Icon = meta.icon;
  return <span className={`status-pill status-pill--${meta.modifier}`}><Icon className={meta.modifier === "processing" ? "spin" : ""} size={13} />{meta.label}</span>;
}

function scoreText(value) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(4) : "—";
}

function provenanceText(hit) {
  const range = hit.page_range ?? hit.pageRange;
  const page = hit.page ?? hit.page_number ?? hit.pageNumber;
  let rangeText = null;
  if (Array.isArray(range) && range.length) rangeText = range.join("–");
  else if (range && typeof range === "object") {
    const start = range.start ?? range.start_page;
    const end = range.end ?? range.end_page;
    if (start !== undefined && start !== null && end !== undefined && end !== null) {
      rangeText = Number(start) === Number(end) ? String(start) : `${start}–${end}`;
    }
  } else if (range !== undefined && range !== null && String(range).trim()) rangeText = String(range);
  const pagePart = rangeText ? `第 ${rangeText} 页` : page ? `第 ${page} 页` : "页码未记录";
  const version = hit.document_version ?? hit.version;
  const chunk = hit.chunk_index ?? hit.chunk_id;
  return `${pagePart} · 版本 ${version ?? "—"} · 片段 ${chunk ?? "—"}`;
}

export function DatasetDetailPage() {
  const params = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const datasetId = Number(params.datasetId ?? params.id);
  const { datasets = [], models = [], documents = {}, loading = {}, actions = {} } = useApp();
  const requestedTab = searchParams.get("tab");
  const [activeTab, setActiveTab] = useState(TABS.some((tab) => tab.id === requestedTab) ? requestedTab : "documents");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [refreshingDocuments, setRefreshingDocuments] = useState(false);
  const [pageError, setPageError] = useState("");
  const [pageNotice, setPageNotice] = useState("");
  const [busyDocumentId, setBusyDocumentId] = useState(null);
  const [renameTarget, setRenameTarget] = useState(null);
  const [renameValue, setRenameValue] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [documentQuery, setDocumentQuery] = useState("");
  const [documentStatus, setDocumentStatus] = useState("ALL");
  const [query, setQuery] = useState("");
  const [recallRunning, setRecallRunning] = useState(false);
  const [recallError, setRecallError] = useState("");
  const [recallResult, setRecallResult] = useState(null);
  const [savingSettings, setSavingSettings] = useState(false);
  const [settingsForm, setSettingsForm] = useState(null);

  const dataset = datasets.find((item) => Number(item.id) === datasetId);
  const datasetDocuments = useMemo(() => documentsForDataset(documents, datasetId), [datasetId, documents]);
  const statusCounts = useMemo(() => datasetDocuments.reduce((counts, document) => {
    counts[normalizedStatus(document)] += 1;
    return counts;
  }, { QUEUED: 0, PROCESSING: 0, READY: 0, FAILED: 0 }), [datasetDocuments]);
  const retrievalReadyCount = useMemo(
    () => datasetDocuments.filter(isDocumentRetrievalReady).length,
    [datasetDocuments],
  );
  const filteredDocuments = useMemo(() => {
    const normalizedQuery = documentQuery.trim().toLocaleLowerCase("zh-CN");
    return datasetDocuments.filter((document) => {
      const matchesStatus = documentStatus === "ALL" || normalizedStatus(document) === documentStatus;
      const matchesQuery = !normalizedQuery || String(document.filename || documentId(document) || "").toLocaleLowerCase("zh-CN").includes(normalizedQuery);
      return matchesStatus && matchesQuery;
    });
  }, [datasetDocuments, documentQuery, documentStatus]);
  const loadDocuments = actions.loadDocuments;
  const denseModels = models.filter((model) => model.capability === "EMBEDDING" && (model.is_active !== false || Number(modelId(model)) === Number(dataset?.dense_embedding_config_id)));
  const sparseModels = models.filter((model) => model.capability === "SPARSE_EMBEDDING" && (model.is_active !== false || Number(modelId(model)) === Number(dataset?.sparse_embedding_config_id)));
  const chatModels = models.filter((model) => model.capability === "CHAT" && (model.is_active !== false || Number(modelId(model)) === Number(dataset?.chat_config_id)));
  const visionModels = models.filter((model) => model.capability === "VISION" && (model.is_active !== false || Number(modelId(model)) === Number(dataset?.vision_config_id)));
  const hasActiveDocuments = statusCounts.QUEUED + statusCounts.PROCESSING > 0;
  const parseBindingChanged = Boolean(settingsForm && dataset && (
    Number(settingsForm.dense_embedding_config_id) !== Number(dataset.dense_embedding_config_id)
    || Number(settingsForm.sparse_embedding_config_id) !== Number(dataset.sparse_embedding_config_id)
    || String(settingsForm.vision_config_id || "") !== String(dataset.vision_config_id || "")
  ));
  const settingsDirty = Boolean(settingsForm && dataset && (
    settingsForm.name.trim() !== String(dataset.name || "")
    || settingsForm.description.trim() !== String(dataset.description || "")
    || Number(settingsForm.dense_embedding_config_id) !== Number(dataset.dense_embedding_config_id)
    || Number(settingsForm.sparse_embedding_config_id) !== Number(dataset.sparse_embedding_config_id)
    || String(settingsForm.chat_config_id || "") !== String(dataset.chat_config_id || "")
    || String(settingsForm.vision_config_id || "") !== String(dataset.vision_config_id || "")
  ));

  useEffect(() => {
    if (!Number.isFinite(datasetId) || !dataset?.id || !loadDocuments) return;
    Promise.resolve(loadDocuments(datasetId)).catch((error) => setPageError(error instanceof Error ? error.message : "文档列表加载失败"));
    // 只在路由数据集变化时读取，避免状态回写导致重复请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasetId, dataset?.id]);

  useEffect(() => {
    if (!dataset) return;
    setSettingsForm({
      name: dataset.name || "",
      description: dataset.description || "",
      dense_embedding_config_id: String(dataset.dense_embedding_config_id || ""),
      sparse_embedding_config_id: String(dataset.sparse_embedding_config_id || ""),
      chat_config_id: String(dataset.chat_config_id || ""),
      vision_config_id: String(dataset.vision_config_id || ""),
    });
  }, [dataset]);

  useEffect(() => {
    if (TABS.some((tab) => tab.id === requestedTab)) setActiveTab(requestedTab);
  }, [requestedTab]);

  function selectTab(tabId) {
    setActiveTab(tabId);
    setSearchParams(tabId === "documents" ? {} : { tab: tabId }, { replace: true });
    window.requestAnimationFrame(() => document.getElementById("dataset-detail-tabs")?.scrollIntoView({ block: "start" }));
  }

  async function handleUpload(files) {
    if (!actions.uploadDocuments) throw new Error("上传操作尚未就绪");
    setUploading(true);
    setPageError("");
    try {
      const results = await actions.uploadDocuments(datasetId, files);
      await actions.loadDocuments?.(datasetId);
      const failed = Array.isArray(results) ? results.filter((item) => item?.error) : [];
      if (failed.length === files.length) throw new Error(`${failed.length} 个文件全部提交失败`);
      setUploadOpen(false);
      setPageNotice(`${files.length - failed.length} 个文件已加入解析队列${failed.length ? `，${failed.length} 个提交失败` : ""}。`);
    } finally {
      setUploading(false);
    }
  }

  async function refreshDocuments() {
    if (!actions.loadDocuments || refreshingDocuments) return;
    setRefreshingDocuments(true);
    setPageError("");
    try {
      await actions.loadDocuments(datasetId);
    } catch (error) {
      setPageNotice("");
      setPageError(error instanceof Error ? error.message : "文档列表加载失败");
    } finally {
      setRefreshingDocuments(false);
    }
  }

  async function runDocumentAction(document, action, successMessage) {
    const id = documentId(document);
    if (!action || busyDocumentId) return;
    setBusyDocumentId(id);
    setPageError("");
    setPageNotice("");
    try {
      await action(datasetId, id);
      await actions.loadDocuments?.(datasetId);
      setPageNotice(successMessage);
    } catch (error) {
      setPageError(error instanceof Error ? error.message : "文档操作失败");
    } finally {
      setBusyDocumentId(null);
    }
  }

  async function handleDeleteDocument(document) {
    if (!window.confirm(`确认删除文档“${document.filename || documentId(document)}”及其分块与索引吗？`)) return;
    await runDocumentAction(document, actions.deleteDocument, "文档及其索引已删除。");
  }

  function openRenameDocument(document) {
    setRenameTarget(document);
    setRenameValue(document.filename || "");
    setPageError("");
  }

  async function handleRenameDocument(event) {
    event.preventDefault();
    const filename = renameValue.trim();
    if (!renameTarget || !filename || !actions.updateDocument || renaming) return;
    setRenaming(true);
    setPageError("");
    try {
      await actions.updateDocument(datasetId, documentId(renameTarget), { filename });
      setRenameTarget(null);
      setPageNotice("文档展示名称已更新。");
    } catch (error) {
      setPageError(error instanceof Error ? error.message : "文档名称更新失败");
    } finally {
      setRenaming(false);
    }
  }

  async function handleRecall(event) {
    event.preventDefault();
    const normalized = query.trim();
    if (!normalized || recallRunning || !actions.recall) return;
    setRecallRunning(true);
    setRecallError("");
    setRecallResult(null);
    try {
      setRecallResult(await actions.recall({ query: normalized, dataset_ids: [datasetId], include_content: true }));
    } catch (error) {
      setRecallError(error instanceof Error ? error.message : "检索测试失败，请稍后重试");
    } finally {
      setRecallRunning(false);
    }
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (!settingsForm || !dataset || !actions.updateDataset || savingSettings || !settingsDirty) return;
    setSavingSettings(true);
    setPageError("");
    setPageNotice("");
    const shouldRebuildIndex = parseBindingChanged;
    const payload = {};
    if (settingsForm.name.trim() !== String(dataset.name || "")) payload.name = settingsForm.name.trim();
    if (settingsForm.description.trim() !== String(dataset.description || "")) payload.description = settingsForm.description.trim() || null;
    if (Number(settingsForm.dense_embedding_config_id) !== Number(dataset.dense_embedding_config_id)) payload.dense_embedding_config_id = Number(settingsForm.dense_embedding_config_id);
    if (Number(settingsForm.sparse_embedding_config_id) !== Number(dataset.sparse_embedding_config_id)) payload.sparse_embedding_config_id = Number(settingsForm.sparse_embedding_config_id);
    if (String(settingsForm.chat_config_id || "") !== String(dataset.chat_config_id || "")) payload.chat_config_id = settingsForm.chat_config_id ? Number(settingsForm.chat_config_id) : null;
    if (String(settingsForm.vision_config_id || "") !== String(dataset.vision_config_id || "")) payload.vision_config_id = settingsForm.vision_config_id ? Number(settingsForm.vision_config_id) : null;
    try {
      await actions.updateDataset(datasetId, payload);
      if (shouldRebuildIndex) await actions.loadDocuments?.(datasetId);
      setPageNotice(shouldRebuildIndex ? "模型绑定已更新，现有文档已加入索引重建队列。" : "数据集设置已更新。");
    } catch (error) {
      setPageError(error instanceof Error ? error.message : "数据集更新失败");
    } finally {
      setSavingSettings(false);
    }
  }

  async function handleDeleteDataset() {
    if (!actions.deleteDataset || datasetDocuments.length) return;
    if (!window.confirm(`确认删除数据集“${dataset.name}”吗？`)) return;
    setSavingSettings(true);
    try {
      await actions.deleteDataset(datasetId);
      navigate("/datasets", { replace: true });
    } catch (error) {
      setPageError(error instanceof Error ? error.message : "数据集删除失败");
      setSavingSettings(false);
    }
  }

  if (!Number.isFinite(datasetId)) {
    return <div className="page"><div className="empty-state"><AlertCircle size={24} /><h1>数据集地址无效</h1><Link className="button button--secondary" to="/datasets">返回数据集</Link></div></div>;
  }

  const loadingDatasets = typeof loading === "boolean" ? loading : Boolean(loading.datasets || loading.initial);
  const loadingDocuments = typeof loading === "boolean" ? false : Boolean(loading.documents);
  if (!dataset && loadingDatasets) return <div className="page"><div className="empty-state empty-state--loading"><Loader2 className="spin" size={22} /><p>正在加载数据集…</p></div></div>;
  if (!dataset) return <div className="page"><div className="empty-state"><Database size={24} /><h1>未找到数据集</h1><p>数据集不存在，或列表尚未完成加载。</p><Link className="button button--secondary" to="/datasets">返回数据集</Link></div></div>;

  const hits = recallResult?.hits || [];

  return (
    <div className="page page--dataset-detail">
      <header className="dataset-detail-header">
        <div className="dataset-detail-header__main">
          <Link className="icon-button" to="/datasets" aria-label="返回数据集列表"><ArrowLeft size={18} /></Link>
          <div><p className="eyebrow">数据集 #{dataset.id}</p><h1>{dataset.name}</h1><p>{dataset.description || "暂无描述"}</p></div>
        </div>
        <div className="dataset-detail-header__actions">
          <Link className="button button--secondary" to="/tasks"><Clock3 size={16} />解析队列</Link>
          <button type="button" className="button button--primary" onClick={() => setUploadOpen(true)}><Upload size={16} />上传文档</button>
        </div>
      </header>

      <section className="dataset-summary" aria-label="数据集摘要">
        <article><span>全部文档</span><strong>{datasetDocuments.length}</strong></article>
        <article><span>排队 / 处理</span><strong>{statusCounts.QUEUED + statusCounts.PROCESSING}</strong></article>
        <article><span>可检索</span><strong>{retrievalReadyCount}</strong></article>
        <article className={statusCounts.FAILED ? "has-error" : ""}><span>失败</span><strong>{statusCounts.FAILED}</strong></article>
      </section>

      {pageNotice ? <div className="notice notice--success" role="status" aria-live="polite"><CheckCircle2 size={16} /><p>{pageNotice}</p><button type="button" onClick={() => setPageNotice("")} aria-label="关闭提示">×</button></div> : null}
      {pageError ? <div className="notice notice--error" role="alert"><AlertCircle size={16} /><p>{pageError}</p><button type="button" onClick={() => setPageError("")} aria-label="关闭错误">×</button></div> : null}

      <nav className="tabs dataset-tabs" id="dataset-detail-tabs" role="tablist" aria-label="数据集详情功能">
        {TABS.map((tab) => {
          const Icon = tab.icon;
          return <button key={tab.id} id={`dataset-tab-${tab.id}`} type="button" role="tab" className={`tab${activeTab === tab.id ? " tab--active" : ""}`} onClick={() => selectTab(tab.id)} aria-selected={activeTab === tab.id} aria-controls={`dataset-panel-${tab.id}`} tabIndex={activeTab === tab.id ? 0 : -1}><Icon size={15} /><span>{tab.label}</span></button>;
        })}
      </nav>

      {activeTab === "documents" ? (
        <section className="panel document-panel" id="dataset-panel-documents" role="tabpanel" aria-labelledby="dataset-tab-documents" tabIndex={0}>
          <div className="section-heading document-panel__heading">
            <div><h2>文档</h2><p>上传后将自动进入解析队列并建立检索索引。</p></div>
          </div>

          {loadingDocuments && datasetDocuments.length === 0 ? (
            <div className="empty-state empty-state--loading"><Loader2 className="spin" size={20} /><p>正在读取文档…</p></div>
          ) : datasetDocuments.length === 0 ? (
            <div className="empty-state"><FileText size={24} /><h3>还没有文档</h3><p>支持 PDF、DOCX、HTML 和 HTM；PDF 使用 OpenDataLoader 解析。</p><button type="button" className="button button--primary" onClick={() => setUploadOpen(true)}><Upload size={16} />上传文档</button></div>
          ) : (
            <>
              <div className="document-toolbar">
                <label className="document-search"><Search size={15} aria-hidden="true" /><input aria-label="搜索文档" value={documentQuery} onChange={(event) => setDocumentQuery(event.target.value)} placeholder="搜索文件名" /></label>
                <select className="document-status-filter" aria-label="按状态筛选文档" value={documentStatus} onChange={(event) => setDocumentStatus(event.target.value)}>
                  <option value="ALL">全部状态</option>
                  <option value="QUEUED">排队中</option>
                  <option value="PROCESSING">处理中</option>
                  <option value="READY">处理完成</option>
                  <option value="FAILED">失败</option>
                </select>
                <span className="document-toolbar__count">显示 {filteredDocuments.length} / {datasetDocuments.length}</span>
                <button type="button" className="button button--secondary" onClick={refreshDocuments} disabled={loadingDocuments || refreshingDocuments}><RefreshCw className={loadingDocuments || refreshingDocuments ? "spin" : ""} size={15} />刷新</button>
              </div>

              {filteredDocuments.length ? (
                <div className="document-list" role="table" aria-label="文档列表">
                  <div className="document-list__header" role="row"><span role="columnheader">文档</span><span role="columnheader">状态</span><span role="columnheader">解析结果</span><span role="columnheader">更新时间</span><span role="columnheader">操作</span></div>
                  {filteredDocuments.map((document) => {
                    const id = documentId(document);
                    const status = normalizedStatus(document);
                    const busy = busyDocumentId === id;
                    const retryable = canRetryDocument(document, status);
                    return (
                      <article className="document-row" role="row" key={id}>
                        <div className="document-row__identity" role="cell"><span className="file-icon"><FileText size={16} /></span><span><Link className="document-name-link" to={`/datasets/${datasetId}/documents/${id}`}>{document.filename || `文档 #${id}`}</Link><small>{String(document.file_type || "").toUpperCase()} · {formatBytes(document.file_size)} · {document.parser_backend || "—"}</small></span></div>
                        <div className="document-row__status" role="cell" data-label="状态"><StatusPill document={document} />{Number(document.attempt_count) > 0 ? <small>尝试 {document.attempt_count} 次</small> : null}</div>
                        <div className="document-row__result" role="cell" data-label="解析结果">
                          {status === "FAILED" ? <p className="document-error">{document.error_message || "解析或索引失败"}</p> : <><strong>{document.chunk_count ?? 0} 个分片 · {document.page_count ?? "—"} 页</strong><small>{status === "READY" ? `耗时 ${formatDuration(resolveDocumentDurationMs(document))}` : status === "QUEUED" ? `可用时间 ${formatTime(document.available_at || document.queued_at)}` : `开始于 ${formatTime(document.processing_started_at)}`}</small></>}
                        </div>
                        <time className="document-row__time" role="cell" data-label="更新时间">{formatTime(document.updated_at)}</time>
                        <div className="document-row__actions" role="cell" data-label="操作">
                          <Link className="button button--tiny document-view-link" to={`/datasets/${datasetId}/documents/${id}`} aria-label={`查看 ${document.filename || id} 的分片详情`}><Eye size={13} />详情</Link>
                          <button type="button" className="icon-button icon-button--quiet" onClick={() => openRenameDocument(document)} disabled={busy || renaming} aria-label={`重命名 ${document.filename || id}`} title="修改展示名称"><Pencil size={14} /></button>
                          {retryable ? <button type="button" className="button button--tiny" onClick={() => runDocumentAction(document, actions.retryDocument, "文档已重新加入队列。")} disabled={busy}><RefreshCw className={busy ? "spin" : ""} size={13} />重试</button> : null}
                          {status === "READY" ? <button type="button" className="button button--tiny" onClick={() => runDocumentAction(document, actions.reparseDocument, "文档已加入重新解析队列。")} disabled={busy}><RotateCw className={busy ? "spin" : ""} size={13} />重新解析</button> : null}
                          {["READY", "FAILED"].includes(status) ? <button type="button" className="icon-button icon-button--quiet icon-button--danger" onClick={() => handleDeleteDocument(document)} disabled={busy} aria-label={`删除 ${document.filename || id}`} title="删除文档"><Trash2 size={14} /></button> : null}
                        </div>
                      </article>
                    );
                  })}
                </div>
              ) : <div className="empty-state empty-state--compact"><Search size={20} /><h3>没有匹配的文档</h3><p>请调整文件名或状态筛选条件。</p></div>}
            </>
          )}
        </section>
      ) : null}

      {activeTab === "recall" ? (
        <section className="recall-lab" id="dataset-panel-recall" role="tabpanel" aria-labelledby="dataset-tab-recall" tabIndex={0}>
          <article className="panel recall-lab__query">
            <div className="section-heading"><div><h2>混合检索测试</h2><p>测试关键词、稀疏向量和稠密向量检索，不生成回答。</p></div></div>
            <form className="recall-form" onSubmit={handleRecall}>
              <label className="form-field"><span>检索问题</span><textarea rows={5} maxLength={8000} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="例如：企业碳排放核算中，外购电力应如何计算？" /></label>
              <div className="recall-form__footer"><span>{query.length} / 8000</span><button type="submit" className="button button--primary" disabled={!query.trim() || recallRunning || !actions.recall}>{recallRunning ? <Loader2 className="spin" size={16} /> : <Play size={16} />}{recallRunning ? "检索中" : "执行检索"}</button></div>
            </form>
            {recallError ? <p className="form-error" role="alert">{recallError}</p> : null}
          </article>
          <article className="panel recall-lab__results">
            <div className="section-heading"><div><h2>检索结果</h2></div>{recallResult ? <span className="count-badge">{hits.length}</span> : null}</div>
            {!recallResult && !recallRunning ? <div className="empty-state empty-state--compact"><Sparkles size={22} /><p>执行检索后，可在这里查看来源、页码与各路分数。</p></div> : null}
            {recallRunning ? <div className="empty-state empty-state--loading"><Loader2 className="spin" size={20} /><p>正在检索相关内容…</p></div> : null}
            {recallResult?.failed_sources?.length ? <div className="notice notice--warning"><AlertCircle size={15} /><p>部分检索服务暂时不可用。</p></div> : null}
            {recallResult && !hits.length ? <div className="empty-state empty-state--compact"><Search size={20} /><p>未检索到相关内容。</p></div> : null}
            {hits.length ? <div className="recall-hit-list">{hits.map((hit, index) => (
              <article className="recall-hit" key={hit.chunk_id || index}>
                <header><span className="recall-hit__index">{hit.result_rank ?? index + 1}</span><div><strong>{hit.filename || `文档 #${hit.doc_id}`}</strong><small>{provenanceText(hit)}</small></div><em>综合相关度 {scoreText(hit.fused_score)}</em></header>
                <div className="score-strip"><span>关键词（BM25）<b>{scoreText(hit.scores?.bm25)}</b></span><span>稀疏向量 <b>{scoreText(hit.scores?.sparse)}</b></span><span>稠密向量 <b>{scoreText(hit.scores?.dense)}</b></span></div>
                <p>{hit.content || "该检索结果暂无可展示内容。"}</p>
              </article>
            ))}</div> : null}
          </article>
        </section>
      ) : null}

      {activeTab === "settings" && settingsForm ? (
        <section className="settings-layout" id="dataset-panel-settings" role="tabpanel" aria-labelledby="dataset-tab-settings" tabIndex={0}>
          <form className="panel dataset-settings-form" onSubmit={saveSettings}>
            <div className="section-heading"><div><h2>基本信息与模型配置</h2><p>更新后将用于后续文档解析和对话。</p></div></div>
            <div className="form-grid form-grid--two">
              <label className="form-field"><span>数据集名称</span><input required value={settingsForm.name} onChange={(event) => setSettingsForm((current) => ({ ...current, name: event.target.value }))} /></label>
              <label className="form-field"><span>对话模型 <small>可选</small></span><select value={settingsForm.chat_config_id} onChange={(event) => setSettingsForm((current) => ({ ...current, chat_config_id: event.target.value }))}><option value="">暂不绑定</option>{chatModels.map((model) => <option key={modelId(model)} value={modelId(model)}>{modelLabel(model)}</option>)}</select></label>
            </div>
            <label className="form-field"><span>描述</span><textarea rows={3} maxLength={512} value={settingsForm.description} onChange={(event) => setSettingsForm((current) => ({ ...current, description: event.target.value }))} /></label>
            {hasActiveDocuments ? <div className="notice notice--warning settings-model-notice"><Clock3 size={15} /><p>当前仍有文档在排队或处理，需等待完成后才能更换向量模型。</p></div> : null}
            {parseBindingChanged && !hasActiveDocuments ? <div className="notice notice--warning settings-model-notice"><AlertCircle size={15} /><p>保存解析模型绑定后，现有文档将生成新版本、重新解析并重建检索索引。</p></div> : null}
            <div className="form-grid form-grid--two">
              <label className="form-field"><span>稠密向量模型</span><select required disabled={hasActiveDocuments} value={settingsForm.dense_embedding_config_id} onChange={(event) => setSettingsForm((current) => ({ ...current, dense_embedding_config_id: event.target.value }))}>{denseModels.map((model) => <option key={modelId(model)} value={modelId(model)}>{modelLabel(model)}</option>)}</select></label>
              <label className="form-field"><span>稀疏向量模型</span><select required disabled={hasActiveDocuments} value={settingsForm.sparse_embedding_config_id} onChange={(event) => setSettingsForm((current) => ({ ...current, sparse_embedding_config_id: event.target.value }))}>{sparseModels.map((model) => <option key={modelId(model)} value={modelId(model)}>{modelLabel(model)}</option>)}</select></label>
            </div>
            <label className="form-field"><span>PDF OCR / 视觉模型 <small>可选</small></span><select disabled={hasActiveDocuments} value={settingsForm.vision_config_id} onChange={(event) => setSettingsForm((current) => ({ ...current, vision_config_id: event.target.value }))}><option value="">暂不绑定</option>{visionModels.map((model) => <option key={modelId(model)} value={modelId(model)}>{modelLabel(model)}</option>)}</select><small>仅在 PDF 页面缺少有效正文或图表需要解释时调用；未绑定不会阻止文档完成解析和检索。</small></label>
            {!denseModels.length || !sparseModels.length ? <p className="settings-model-empty"><AlertCircle size={14} />缺少可用的向量模型，请先前往 <Link to="/models">模型配置</Link>。</p> : null}
            <footer className="dataset-settings-form__actions"><span>{settingsDirty ? "有尚未保存的更改" : "当前设置已保存"}</span><button type="submit" className="button button--primary" disabled={savingSettings || !settingsDirty || !denseModels.length || !sparseModels.length}>{savingSettings ? <Loader2 className="spin" size={15} /> : <Settings2 size={15} />}{savingSettings ? "正在保存" : "保存更改"}</button></footer>
          </form>

          <aside className="settings-aside">
            <article className="panel execution-card"><h2>处理方式</h2><dl><div><dt><FileText size={14} />支持格式</dt><dd>PDF、DOCX、HTML、HTM</dd></div><div><dt><Layers3 size={14} />PDF 解析</dt><dd>OpenDataLoader</dd></div><div><dt><Database size={14} />检索索引</dt><dd>关键词 + 稀疏向量 + 稠密向量</dd></div><div><dt><Clock3 size={14} />任务处理</dt><dd>后台解析队列</dd></div></dl></article>
            <article className="panel danger-zone"><p className="eyebrow">谨慎操作</p><h2>删除数据集</h2><p>只有不包含文档的数据集才能删除。</p><button type="button" className="button button--danger" onClick={handleDeleteDataset} disabled={savingSettings || datasetDocuments.length > 0}><Trash2 size={15} />删除数据集</button>{datasetDocuments.length ? <small>请先删除当前 {datasetDocuments.length} 个文档。</small> : null}</article>
          </aside>
        </section>
      ) : null}

      {renameTarget ? (
        <div className="dialog-backdrop" role="presentation" onMouseDown={renaming ? undefined : () => setRenameTarget(null)}>
          <section className="dialog document-rename-dialog" role="dialog" aria-modal="true" aria-labelledby="document-rename-title" onMouseDown={(event) => event.stopPropagation()}>
            <header className="dialog__header">
              <div><h2 id="document-rename-title">修改文档名称</h2><p className="dialog__subtitle">只修改界面展示名称，不改变原文件格式和存储对象。</p></div>
              <button type="button" className="icon-button" onClick={() => setRenameTarget(null)} disabled={renaming} aria-label="关闭"><X size={18} /></button>
            </header>
            <form className="form-stack" onSubmit={handleRenameDocument}>
              <label className="form-field"><span>文档名称</span><input autoFocus required maxLength={255} value={renameValue} onChange={(event) => setRenameValue(event.target.value)} /></label>
              <footer className="dialog__footer">
                <button type="button" className="button button--ghost" onClick={() => setRenameTarget(null)} disabled={renaming}>取消</button>
                <button type="submit" className="button button--primary" disabled={renaming || !renameValue.trim()}>{renaming ? <Loader2 className="spin" size={15} /> : <Pencil size={15} />}{renaming ? "正在保存" : "保存名称"}</button>
              </footer>
            </form>
          </section>
        </div>
      ) : null}

      <UploadDialog open={uploadOpen} datasetName={dataset.name} busy={uploading} onClose={() => !uploading && setUploadOpen(false)} onSubmit={handleUpload} />
    </div>
  );
}

export default DatasetDetailPage;
