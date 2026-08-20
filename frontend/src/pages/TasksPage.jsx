import { useMemo, useState } from 'react';
import { AlertCircle, CheckCircle2, Clock3, FileText, Loader2, RefreshCw, Search, XCircle } from 'lucide-react';
import { Link } from 'react-router-dom';
import { isDocumentRetrievalReady } from '../lib/parse-quality';
import { documentErrorMessage } from '../lib/text';
import { useApp } from '../state/AppContext';

const FILTERS = [
  { id: 'ALL', label: '全部' },
  { id: 'QUEUED', label: '排队中' },
  { id: 'PROCESSING', label: '处理中' },
  { id: 'READY', label: '处理完成' },
  { id: 'FAILED', label: '失败' },
];

function flattenDocuments(documents) {
  if (Array.isArray(documents)) return documents;
  return Object.values(documents || {}).flatMap((items) => (Array.isArray(items) ? items : []));
}

function normalizedStatus(status) {
  const value = String(status || '').toUpperCase();
  if (value === 'READY' || value === 'SUCCESS') return 'READY';
  if (value === 'FAILED') return 'FAILED';
  if (value === 'QUEUED' || value === 'PENDING' || value === 'WAITING') return 'QUEUED';
  return 'PROCESSING';
}

function canRetryDocument(document) {
  const status = normalizedStatus(document?.status);
  if (status === 'FAILED') return true;
  if (status !== 'PROCESSING') return false;
  if (!document?.lease_expires_at) return true;
  return new Date(document.lease_expires_at).getTime() <= Date.now();
}

function statusMeta(document) {
  const normalized = normalizedStatus(document?.status ?? document);
  if (normalized === 'READY') return { label: '可检索', modifier: 'ready', icon: CheckCircle2 };
  if (normalized === 'FAILED') return { label: '失败', modifier: 'failed', icon: XCircle };
  if (normalized === 'QUEUED' && Number(document?.attempt_count || 0) > 0) return { label: '待重试', modifier: 'retry', icon: Clock3 };
  if (normalized === 'QUEUED') return { label: '排队中', modifier: 'queued', icon: Clock3 };
  if (canRetryDocument(document)) return { label: '处理超时', modifier: 'retry', icon: AlertCircle };
  return { label: '处理中', modifier: 'processing', icon: Loader2 };
}

function StatusPill({ document }) {
  const meta = statusMeta(document);
  const Icon = meta.icon;
  return <span className={`status-pill status-pill--${meta.modifier}`}><Icon className={meta.modifier === 'processing' ? 'spin' : ''} size={13} />{meta.label}</span>;
}

function formatTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN');
}

function documentId(document) {
  return document?.document_id ?? document?.documentId ?? document?.id;
}

function datasetIdOf(document) {
  return document?.dataset_id ?? document?.datasetId;
}

export function TasksPage() {
  const { datasets = [], documents = {}, loading = {}, actions = {} } = useApp();
  const [activeFilter, setActiveFilter] = useState('ALL');
  const [keyword, setKeyword] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  const [actionNotice, setActionNotice] = useState('');
  const [busyDocumentId, setBusyDocumentId] = useState(null);
  const allDocuments = useMemo(() => flattenDocuments(documents), [documents]);
  const datasetById = useMemo(() => new Map(datasets.map((dataset) => [Number(dataset.id), dataset])), [datasets]);

  const counts = useMemo(() => {
    const next = { ALL: allDocuments.length, QUEUED: 0, PROCESSING: 0, READY: 0, FAILED: 0, RETRIEVAL_READY: 0 };
    allDocuments.forEach((document) => {
      next[normalizedStatus(document.status)] += 1;
      if (isDocumentRetrievalReady(document)) next.RETRIEVAL_READY += 1;
    });
    return next;
  }, [allDocuments]);

  const filteredDocuments = useMemo(() => {
    const normalizedKeyword = keyword.trim().toLowerCase();
    return allDocuments.filter((document) => {
      const status = normalizedStatus(document.status);
      if (activeFilter !== 'ALL' && status !== activeFilter) return false;
      const dataset = datasetById.get(Number(datasetIdOf(document)));
      if (!normalizedKeyword) return true;
      return `${document.filename || ''} ${dataset?.name || ''} ${documentErrorMessage(document, '')}`
        .toLowerCase()
        .includes(normalizedKeyword);
    });
  }, [activeFilter, allDocuments, datasetById, keyword]);

  async function refreshAll() {
    if ((!actions.loadAllDocuments && !actions.loadDocuments) || refreshing) return;
    setRefreshing(true);
    setRefreshError('');
    try {
      if (actions.loadAllDocuments) await actions.loadAllDocuments();
      else await Promise.all(datasets.map((dataset) => actions.loadDocuments(dataset.id)));
    } catch (error) {
      setRefreshError(error instanceof Error ? error.message : '任务状态刷新失败');
    } finally {
      setRefreshing(false);
    }
  }

  async function runDocumentAction(document, action, successMessage) {
    if (!action || busyDocumentId) return;
    const id = documentId(document);
    const datasetId = Number(datasetIdOf(document));
    setBusyDocumentId(id);
    setRefreshError('');
    try {
      await action(datasetId, id);
      await actions.loadDocuments?.(datasetId);
      setActionNotice(successMessage);
    } catch (error) {
      setRefreshError(error instanceof Error ? error.message : '文档操作失败');
    } finally {
      setBusyDocumentId(null);
    }
  }

  const isLoading = typeof loading === 'boolean' ? loading : Boolean(loading.documents || loading.initial);

  return (
    <div className="page page--tasks feature-page">
      <header className="knowledge-hero">
        <div className="knowledge-hero__copy">
          <p className="eyebrow">Document processing queue</p>
          <h1>解析队列</h1>
          <p className="knowledge-hero__subtitle">{counts.RETRIEVAL_READY} 份文档可检索</p>
        </div>
        <button type="button" className="button button--secondary knowledge-hero__action" onClick={refreshAll} disabled={refreshing || (!actions.loadAllDocuments && !actions.loadDocuments)}>
          <RefreshCw className={refreshing ? 'spin' : ''} size={16} />
          {refreshing ? '刷新中' : '刷新状态'}
        </button>
      </header>

      {refreshError ? <div className="notice notice--error"><AlertCircle size={16} /><p>{refreshError}</p></div> : null}
      {actionNotice ? <div className="notice notice--success"><CheckCircle2 size={16} /><p>{actionNotice}</p></div> : null}

      <section className="panel panel--flush">
        <div className="task-toolbar">
          <div className="filter-tabs" role="tablist" aria-label="任务状态筛选">
            {FILTERS.map((filter) => (
              <button
                key={filter.id}
                type="button"
                role="tab"
                aria-selected={activeFilter === filter.id}
                className={`filter-tab${activeFilter === filter.id ? ' filter-tab--active' : ''}`}
                onClick={() => setActiveFilter(filter.id)}
              >
                {filter.label}<span>{counts[filter.id]}</span>
              </button>
            ))}
          </div>
          <label className="search-field search-field--compact"><Search size={15} /><input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索文件或数据集" /></label>
        </div>

        {isLoading && allDocuments.length === 0 ? (
          <div className="empty-state empty-state--loading"><Loader2 className="spin" size={21} /><p>正在汇总任务状态...</p></div>
        ) : filteredDocuments.length === 0 ? (
          <div className="empty-state empty-state--compact"><Clock3 size={22} /><h3>没有匹配的文档</h3><p>上传文档后，排队与处理状态会显示在这里。</p></div>
        ) : (
          <div className="data-table-wrap">
            <table className="data-table task-table">
              <thead><tr><th>文件 / 数据集</th><th>状态</th><th>结果</th><th>提交时间</th><th>更新时间</th><th>操作</th></tr></thead>
              <tbody>
                {filteredDocuments.map((document) => {
                  const datasetId = Number(datasetIdOf(document));
                  const dataset = datasetById.get(datasetId);
                  const documentStatus = normalizedStatus(document.status);
                  const retryable = canRetryDocument(document);
                  return (
                    <tr key={`${datasetId}:${documentId(document)}`}>
                      <td>
                        <div className="table-primary-cell"><span className="file-icon"><FileText size={15} /></span><span><strong>{document.filename || `文档 #${documentId(document)}`}</strong><small>{dataset?.name || `数据集 #${datasetId}`}</small></span></div>
                      </td>
                      <td><StatusPill document={document} /></td>
                      <td>
                        <div className="task-result-cell">
                          {documentStatus === 'FAILED' ? (
                            <span className="table-error" title={documentErrorMessage(document)}>{documentErrorMessage(document)}</span>
                          ) : documentStatus === 'READY' ? (
                            <span>{document.chunk_count ?? 0} 个片段 · {document.page_count ?? '-'} 页</span>
                          ) : (
                            <span className="muted-copy">{documentStatus === 'QUEUED' ? '等待处理' : `第 ${document.attempt_count || 1} 次尝试`}</span>
                          )}
                        </div>
                      </td>
                      <td>{formatTime(document.created_at)}</td>
                      <td>{formatTime(document.updated_at)}</td>
                      <td>
                        <div className="task-row-actions">
                          <Link className="text-link" to={`/datasets/${datasetId}/documents/${documentId(document)}`}>查看详情</Link>
                          {retryable ? <button type="button" className="button button--tiny" onClick={() => runDocumentAction(document, actions.retryDocument, '文档已重新加入队列。')} disabled={busyDocumentId === documentId(document)}><RefreshCw className={busyDocumentId === documentId(document) ? 'spin' : ''} size={13} /> 重试</button> : null}
                          {documentStatus === 'READY' ? <button type="button" className="button button--tiny" onClick={() => runDocumentAction(document, actions.reparseDocument, '文档已加入重新解析队列。')} disabled={busyDocumentId === documentId(document)}><RefreshCw className={busyDocumentId === documentId(document) ? 'spin' : ''} size={13} /> 重新解析</button> : null}
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

export default TasksPage;
