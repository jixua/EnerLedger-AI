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

const STATUS_ORDER = { FAILED: 0, PROCESSING: 1, QUEUED: 2, READY: 3 };
const DOCUMENT_POLL_INTERVAL_MS = Math.max(1000, Number(import.meta.env.VITE_DOCUMENT_POLL_INTERVAL_MS || 3000));

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
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

function formatDuration(milliseconds) {
  const safeMilliseconds = Number(milliseconds);
  if (!Number.isFinite(safeMilliseconds) || safeMilliseconds < 0) return '—';
  const totalSeconds = Math.floor(safeMilliseconds / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分 ${seconds} 秒`;
  return `${seconds} 秒`;
}

function elapsedSince(value) {
  if (!value) return null;
  const timestamp = new Date(value).getTime();
  return Number.isNaN(timestamp) ? null : Math.max(0, Date.now() - timestamp);
}

function taskInformation(document) {
  const status = normalizedStatus(document.status);
  if (status === 'FAILED') {
    return {
      primary: documentErrorMessage(document, '解析失败'),
      secondary: document.error_code ? `错误代码：${document.error_code}` : '可以重新加入解析队列',
    };
  }
  if (status === 'PROCESSING') {
    const duration = elapsedSince(document.processing_started_at);
    return {
      primary: `第 ${document.attempt_count || 1} 次尝试${duration === null ? '' : ` · 已处理 ${formatDuration(duration)}`}`,
      secondary: '解析任务正在运行',
    };
  }
  if (status === 'QUEUED') {
    const duration = elapsedSince(document.queued_at);
    return {
      primary: duration === null ? '等待处理' : `已等待 ${formatDuration(duration)}`,
      secondary: Number(document.attempt_count || 0) > 0 ? `等待第 ${Number(document.attempt_count) + 1} 次尝试` : '已加入解析队列',
    };
  }
  const result = `${document.chunk_count ?? 0} 个片段 · ${document.page_count ?? '—'} 页`;
  return {
    primary: document.parse_time_ms == null ? result : `${result} · 用时 ${formatDuration(document.parse_time_ms)}`,
    secondary: '解析完成，可用于知识检索',
  };
}

function taskTime(document) {
  const status = normalizedStatus(document.status);
  if (status === 'PROCESSING') return { label: '处理开始于', value: document.processing_started_at };
  if (status === 'QUEUED') return { label: '排队于', value: document.queued_at };
  if (status === 'READY') return { label: '完成于', value: document.finished_at || document.updated_at };
  return { label: '更新于', value: document.updated_at || document.finished_at };
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
    return allDocuments
      .filter((document) => {
        const status = normalizedStatus(document.status);
        if (activeFilter !== 'ALL' && status !== activeFilter) return false;
        const dataset = datasetById.get(Number(datasetIdOf(document)));
        if (!normalizedKeyword) return true;
        return `${document.filename || ''} ${dataset?.name || ''} ${documentErrorMessage(document, '')}`
          .toLowerCase()
          .includes(normalizedKeyword);
      })
      .sort((left, right) => {
        const statusDifference = STATUS_ORDER[normalizedStatus(left.status)] - STATUS_ORDER[normalizedStatus(right.status)];
        if (statusDifference !== 0) return statusDifference;
        return new Date(right.updated_at || right.created_at || 0).getTime() - new Date(left.updated_at || left.created_at || 0).getTime();
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
  const hasActiveDocuments = counts.QUEUED + counts.PROCESSING > 0;

  return (
    <div className="page page--tasks feature-page">
      <header className="task-hero">
        <div className="task-hero__copy">
          <h1>解析队列</h1>
          <p>{counts.ALL} 份文档 · {counts.RETRIEVAL_READY} 份可检索</p>
        </div>
        <div className="task-hero__sync">
          <button type="button" className="icon-button" onClick={refreshAll} disabled={refreshing || (!actions.loadAllDocuments && !actions.loadDocuments)} aria-label="刷新解析队列" title="刷新解析队列">
            <RefreshCw className={refreshing ? 'spin' : ''} size={17} />
          </button>
          <span>{hasActiveDocuments ? `每 ${DOCUMENT_POLL_INTERVAL_MS / 1000} 秒自动更新` : '状态已同步'}</span>
        </div>
      </header>

      {refreshError ? <div className="notice notice--error"><AlertCircle size={16} /><p>{refreshError}</p></div> : null}
      {actionNotice ? <div className="notice notice--success"><CheckCircle2 size={16} /><p>{actionNotice}</p></div> : null}

      <section className="task-queue" aria-label="文档解析任务">
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
          <label className="search-field search-field--compact"><Search size={15} /><input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索文件名或数据集" /></label>
        </div>

        {isLoading && allDocuments.length === 0 ? (
          <div className="empty-state empty-state--loading"><Loader2 className="spin" size={21} /><p>正在汇总任务状态...</p></div>
        ) : filteredDocuments.length === 0 ? (
          <div className="empty-state empty-state--compact"><Clock3 size={22} /><h3>没有匹配的文档</h3><p>上传文档后，排队与处理状态会显示在这里。</p></div>
        ) : (
          <div className="data-table-wrap task-table-wrap">
            <table className="task-table">
              <thead><tr><th>文档</th><th>状态</th><th>处理信息</th><th>时间</th><th>操作</th></tr></thead>
              <tbody>
                {filteredDocuments.map((document) => {
                  const datasetId = Number(datasetIdOf(document));
                  const dataset = datasetById.get(datasetId);
                  const documentStatus = normalizedStatus(document.status);
                  const retryable = canRetryDocument(document);
                  const information = taskInformation(document);
                  const time = taskTime(document);
                  return (
                    <tr className={`task-row task-row--${documentStatus.toLowerCase()}`} key={`${datasetId}:${documentId(document)}`}>
                      <td>
                        <div className="table-primary-cell"><span className="file-icon"><FileText size={15} /></span><span><strong>{document.filename || `文档 #${documentId(document)}`}</strong><small>{dataset?.name || `数据集 #${datasetId}`}</small></span></div>
                      </td>
                      <td><StatusPill document={document} /></td>
                      <td>
                        <div className="task-result-cell">
                          <strong title={information.primary}>{information.primary}</strong>
                          <small>{information.secondary}</small>
                        </div>
                      </td>
                      <td><time className="task-row__time"><span>{formatTime(time.value)}</span><small>{time.label}</small></time></td>
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
