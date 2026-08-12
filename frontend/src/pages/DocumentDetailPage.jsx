import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  Check,
  CheckCircle2,
  Clock3,
  Copy,
  Eye,
  EyeOff,
  FileText,
  Layers3,
  Loader2,
  RefreshCw,
  RotateCw,
  Workflow,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import { Link, useParams } from "react-router-dom";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import {
  createDocumentStructuredTablesPlugin,
  DocumentHtmlTable,
  DocumentStructuredTable,
  remarkDocumentHtmlTables,
} from "../components/DocumentHtmlTable";
import { DocumentPreviewImage } from "../components/DocumentPreviewImage";
import {
  createDocumentBoundaryPlugin,
  normalizeDocumentBoundaries,
} from "../lib/document-reader";
import { normalizeDocumentMath } from "../lib/document-math";
import { formatParseQualityWarning, normalizeParseQuality } from "../lib/parse-quality";
import { useApp } from "../state/AppContext";

const CHUNK_TYPE_LABELS = {
  mixed: "综合文本",
  paragraph: "段落",
  heading: "标题",
  list: "列表",
  blockquote: "引用",
  table: "表格",
  image: "图片",
  code_block: "代码",
  math_block: "公式",
  front_matter: "文档元信息",
  text: "文本",
};

function documentId(document) {
  return document?.document_id ?? document?.documentId ?? document?.id;
}

function normalizedStatus(document) {
  const value = String(document?.status ?? document ?? "").toUpperCase();
  if (value === "READY" || value === "SUCCESS") return "READY";
  if (value === "FAILED") return "FAILED";
  if (["QUEUED", "PENDING", "WAITING"].includes(value)) return "QUEUED";
  return "PROCESSING";
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

function formatDuration(milliseconds) {
  if (!Number.isFinite(Number(milliseconds))) return "—";
  const value = Number(milliseconds);
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(1)} s`;
}

function formatRange(start, end, prefix, { lineNumber = false } = {}) {
  if (start === null || start === undefined) return null;
  const displayStart = Number(start) + (lineNumber ? 1 : 0);
  const displayEnd = end === null || end === undefined
    ? displayStart
    : Number(end) + (lineNumber ? 1 : 0);
  if (displayStart === displayEnd) return `${prefix} ${displayStart}`;
  return `${prefix} ${displayStart}–${displayEnd}`;
}

function findDocument(documents, targetId) {
  return Object.values(documents || {}).flat().find((item) => Number(documentId(item)) === Number(targetId));
}

function BoundaryMarker({ entry, approximate = false, renderAnchor = true }) {
  const { boundary, readerIndex, anchorId } = entry;
  const sequence = readerIndex + 1;
  const previousSequence = sequence - 1;
  const pageRange = formatRange(boundary?.start_page, boundary?.end_page, "第", {});
  const readablePageRange = pageRange ? `${pageRange} 页` : null;
  const lineRange = formatRange(boundary?.start_line, boundary?.end_line, "行", { lineNumber: true });
  const structure = boundary?.structure || {};
  const headingTrail = Array.isArray(boundary?.heading_trail)
    ? boundary.heading_trail
    : Array.isArray(structure.heading_trail) ? structure.heading_trail : [];
  const typeLabel = CHUNK_TYPE_LABELS[boundary?.chunk_type] || boundary?.chunk_type || "文本";
  const label = readerIndex === 0
    ? `文档起始 · 分片 ${String(sequence).padStart(2, "0")}`
    : `分片 ${String(previousSequence).padStart(2, "0")} 结束 · 分片 ${String(sequence).padStart(2, "0")} 开始`;

  return (
    <div id={renderAnchor ? anchorId : undefined} className="document-chunk-boundary__item" data-chunk-sequence={sequence}>
      <details>
        <summary aria-label={label}>
          <Layers3 size={14} aria-hidden="true" />
          <span>{label}</span>
          {approximate ? <em>近似位置</em> : null}
          {readerIndex > 0 ? <small>{[typeLabel, readablePageRange, lineRange].filter(Boolean).join(" · ")}</small> : null}
        </summary>
        <div className="document-chunk-boundary__detail">
          {headingTrail.length ? <span>标题路径：{headingTrail.join(" / ")}</span> : null}
          {lineRange ? <span>{lineRange}</span> : null}
          {readablePageRange ? <span>{readablePageRange}</span> : null}
          {boundary?.split_strategy || structure.split_strategy ? <span>切分策略：{boundary.split_strategy || structure.split_strategy}</span> : null}
          {boundary?.chunk_id ? <code>{boundary.chunk_id}</code> : null}
        </div>
      </details>
    </div>
  );
}

function GroupedBoundaryMarker({ entries, approximate = false }) {
  const firstEntry = entries[0];
  const lastEntry = entries[entries.length - 1];
  const firstChunk = firstEntry.readerIndex === 0 ? 1 : firstEntry.readerIndex;
  const lastChunk = lastEntry.readerIndex + 1;
  const chunkRange = firstChunk === lastChunk
    ? `分片 ${String(lastChunk).padStart(2, "0")}`
    : `分片 ${String(firstChunk).padStart(2, "0")}–${String(lastChunk).padStart(2, "0")}`;

  return (
    <div className="document-chunk-boundary__cluster">
      {entries.map((entry) => (
        <span
          key={entry.anchorId}
          id={entry.anchorId}
          className="document-chunk-boundary__anchor"
          data-chunk-sequence={entry.readerIndex + 1}
          aria-hidden="true"
        />
      ))}
      <details>
        <summary aria-label={`此处包含 ${entries.length} 个分片边界`}>
          <Layers3 size={14} aria-hidden="true" />
          <span>此段内包含 {entries.length} 个分片边界</span>
          {approximate ? <em>近似位置</em> : null}
          <small>{chunkRange} · 同一表格或段落内部</small>
        </summary>
        <div className="document-chunk-boundary__cluster-body">
          <p>这些切点位于同一表格或段落内部，正文显示在相邻文档内容中，不是空分片。</p>
          <div className="document-chunk-boundary__cluster-list">
            {entries.map((entry) => (
              <BoundaryMarker
                key={entry.anchorId}
                entry={entry}
                approximate={approximate}
                renderAnchor={false}
              />
            ))}
          </div>
        </div>
      </details>
    </div>
  );
}

function BoundaryGroup({ entries, approximate = false }) {
  if (!entries.length) return null;
  return (
    <div
      className={`document-chunk-boundary${entries[0].readerIndex === 0 ? " document-chunk-boundary--start" : ""}${entries.length > 1 ? " document-chunk-boundary--grouped" : ""}`}
      role="separator"
      aria-label={entries.length > 1 ? `此处包含 ${entries.length} 个分片边界` : `分片 ${entries[0].readerIndex + 1} 边界`}
    >
      <span className="document-chunk-boundary__line" aria-hidden="true" />
      <div className="document-chunk-boundary__items">
        {entries.length > 1
          ? <GroupedBoundaryMarker entries={entries} approximate={approximate} />
          : <BoundaryMarker entry={entries[0]} approximate={approximate} />}
      </div>
      <span className="document-chunk-boundary__line" aria-hidden="true" />
    </div>
  );
}

export function DocumentDetailPage() {
  const params = useParams();
  const datasetId = Number(params.datasetId);
  const targetDocumentId = Number(params.documentId);
  const { datasets = [], documents = {}, loading = {}, actions = {} } = useApp();
  const contextDocument = useMemo(() => findDocument(documents, targetDocumentId), [documents, targetDocumentId]);
  const dataset = datasets.find((item) => Number(item.id) === datasetId);
  const [document, setDocument] = useState(contextDocument || null);
  const [loadingDocument, setLoadingDocument] = useState(!contextDocument);
  const [documentError, setDocumentError] = useState("");
  const [preview, setPreview] = useState(null);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [previewRefreshKey, setPreviewRefreshKey] = useState(0);
  const [showBoundaries, setShowBoundaries] = useState(true);
  const [jumpTarget, setJumpTarget] = useState("");
  const [busyAction, setBusyAction] = useState(false);
  const [copiedValue, setCopiedValue] = useState("");

  const status = normalizedStatus(document);
  const documentVersion = Number(document?.version ?? 0);
  const routeIsValid = Number.isFinite(datasetId) && datasetId > 0 && Number.isFinite(targetDocumentId) && targetDocumentId > 0;
  const routeMatchesDocument = !document || Number(document.dataset_id ?? document.datasetId) === datasetId;
  const readerBoundaries = useMemo(
    () => normalizeDocumentBoundaries(preview?.boundaries || []),
    [preview],
  );
  const boundaryPlugin = useMemo(
    () => createDocumentBoundaryPlugin(readerBoundaries, preview?.boundary_precision),
    [preview?.boundary_precision, readerBoundaries],
  );
  const tableStructures = useMemo(
    () => document?.parse_quality?.table_structure?.tables || [],
    [document?.parse_quality?.table_structure?.tables],
  );
  const tableStructureMap = useMemo(
    () => new Map(tableStructures.map((structure) => [
      String(structure?.table_id || structure?.preview?.id || ""),
      structure,
    ])),
    [tableStructures],
  );
  const structuredTablePlugin = useMemo(
    () => createDocumentStructuredTablesPlugin(tableStructures),
    [tableStructures],
  );
  const markdownComponents = useMemo(
    () => ({
      a: ({ children, node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
      img: (props) => <DocumentPreviewImage {...props} />,
      table: ({ children, node: _node, ...props }) => <div className="document-reader-table"><table {...props}>{children}</table></div>,
      "document-html-table": (props) => <DocumentHtmlTable {...props} />,
      "document-structured-table": ({ node }) => {
        const tableId = String(node?.properties?.tableId ?? node?.properties?.tableid ?? "");
        return <DocumentStructuredTable structure={tableStructureMap.get(tableId)} />;
      },
      "document-chunk-boundary": ({ node }) => {
        const rawIndexes = node?.properties?.boundaryIndexes ?? node?.properties?.boundaryindexes ?? "";
        const entries = String(rawIndexes)
          .split(",")
          .map((value) => readerBoundaries[Number(value)])
          .filter(Boolean);
        const approximate = String(node?.properties?.placementApproximate ?? node?.properties?.placementapproximate) === "true";
        return showBoundaries ? <BoundaryGroup entries={entries} approximate={approximate} /> : null;
      },
    }),
    [readerBoundaries, showBoundaries, tableStructureMap],
  );

  useEffect(() => {
    if (contextDocument) setDocument(contextDocument);
  }, [contextDocument]);

  const refreshDocument = useCallback(async () => {
    if (!routeIsValid || !actions.loadDocument) return null;
    setLoadingDocument(true);
    setDocumentError("");
    try {
      const next = await actions.loadDocument(targetDocumentId);
      if (Number(next.dataset_id ?? next.datasetId) !== datasetId) {
        throw new Error("文档不属于当前数据集");
      }
      setDocument(next);
      return next;
    } catch (error) {
      setDocumentError(error instanceof Error ? error.message : "文档加载失败");
      return null;
    } finally {
      setLoadingDocument(false);
    }
  }, [actions.loadDocument, datasetId, routeIsValid, targetDocumentId]);

  useEffect(() => {
    void refreshDocument();
  }, [refreshDocument]);

  const loadPreview = useCallback(async (signal) => {
    if (status !== "READY" || !actions.loadDocumentPreview) return;
    setLoadingPreview(true);
    setPreviewError("");
    try {
      const next = await actions.loadDocumentPreview(targetDocumentId, { signal });
      if (signal?.aborted) return;
      if (Number(next.dataset_id) !== datasetId || Number(next.document_version) !== documentVersion) {
        throw new Error("文档内容版本与当前文档不一致，请刷新后重试");
      }
      setPreview(next);
    } catch (error) {
      if (signal?.aborted || error?.name === "AbortError") return;
      setPreviewError(error instanceof Error ? error.message : "文档内容加载失败");
    } finally {
      if (!signal?.aborted) setLoadingPreview(false);
    }
  }, [actions.loadDocumentPreview, datasetId, documentVersion, status, targetDocumentId]);

  useEffect(() => {
    if (status !== "READY") {
      setLoadingPreview(false);
      setPreviewError("");
      setPreview(null);
      return undefined;
    }
    const controller = new AbortController();
    void loadPreview(controller.signal);
    return () => controller.abort();
  }, [loadPreview, previewRefreshKey, status]);

  async function handleLifecycleAction(action) {
    if (!action || busyAction) return;
    setBusyAction(true);
    setDocumentError("");
    try {
      const next = await action(datasetId, targetDocumentId);
      setDocument(next);
      setPreview(null);
    } catch (error) {
      setDocumentError(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusyAction(false);
    }
  }

  async function copyText(value, key) {
    try {
      await navigator.clipboard.writeText(value || "");
      setCopiedValue(key);
      window.setTimeout(() => setCopiedValue((current) => current === key ? "" : current), 1600);
    } catch {
      setPreviewError("无法复制文档内容，请手动选择文本复制");
    }
  }

  function jumpToSection(value) {
    setJumpTarget(value);
    if (!value) return;
    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    window.document.getElementById(value)?.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "center" });
  }

  async function refreshPage() {
    const next = await refreshDocument();
    if (next && normalizedStatus(next) === "READY") {
      setPreviewRefreshKey((current) => current + 1);
    }
  }

  if (!routeIsValid) {
    return <div className="page"><div className="empty-state"><AlertCircle size={24} /><h1>文档地址无效</h1><Link className="button button--secondary" to="/datasets">返回数据集</Link></div></div>;
  }

  const loadingDatasets = typeof loading === "boolean" ? loading : Boolean(loading.datasets || loading.initial);
  if ((loadingDocument || loadingDatasets) && !document) {
    return <div className="page"><div className="empty-state empty-state--loading"><Loader2 className="spin" size={22} /><p>正在读取文档详情…</p></div></div>;
  }

  if (!document || !routeMatchesDocument) {
    return <div className="page"><div className="empty-state"><FileText size={24} /><h1>未找到文档</h1><p>{documentError || "文档不存在，或不属于当前数据集。"}</p><Link className="button button--secondary" to={`/datasets/${datasetId}`}>返回数据集</Link></div></div>;
  }

  const canRetry = status === "FAILED" || (status === "PROCESSING" && (!document.lease_expires_at || new Date(document.lease_expires_at).getTime() <= Date.now()));
  const rawSourceChunkCount = Number(preview?.source_chunk_count);
  const sourceChunkCount = preview
    ? Math.max(Number.isFinite(rawSourceChunkCount) ? rawSourceChunkCount : 0, readerBoundaries.length)
    : null;
  const renderedPreviewContent = useMemo(
    () => normalizeDocumentMath(preview?.content || ""),
    [preview?.content],
  );
  const parseQuality = normalizeParseQuality(document);

  return (
    <div className="page page--document-detail">
      <header className="document-detail-header">
        <div className="document-detail-header__main">
          <Link className="icon-button" to={`/datasets/${datasetId}`} aria-label="返回数据集"><ArrowLeft size={18} /></Link>
          <span className="document-detail-file-icon" aria-hidden="true"><FileText size={22} /></span>
          <div className="document-detail-header__identity">
            <p className="eyebrow">文档详情 · {dataset?.name || `数据集 #${datasetId}`}</p>
            <div className="document-detail-title-line"><h1>{document.filename || `文档 #${targetDocumentId}`}</h1><StatusPill document={document} /></div>
            <p>{String(document.file_type || "").toUpperCase()} · {formatBytes(document.file_size)} · {document.parser_backend || "—"} · 更新于 {formatTime(document.updated_at)}</p>
          </div>
        </div>
        <div className="document-detail-header__actions">
          <Link className="button button--secondary" to="/tasks"><Workflow size={16} />解析队列</Link>
          <button type="button" className="button button--secondary" onClick={() => { void refreshPage(); }} disabled={loadingDocument || loadingPreview}><RefreshCw className={loadingDocument || loadingPreview ? "spin" : ""} size={15} />刷新</button>
          {status === "READY" ? <button type="button" className="button button--primary" onClick={() => handleLifecycleAction(actions.reparseDocument)} disabled={busyAction}><RotateCw className={busyAction ? "spin" : ""} size={15} />重新解析</button> : null}
          {canRetry ? <button type="button" className="button button--primary" onClick={() => handleLifecycleAction(actions.retryDocument)} disabled={busyAction}><RefreshCw className={busyAction ? "spin" : ""} size={15} />重试解析</button> : null}
        </div>
      </header>

      <section className="document-detail-meta" aria-label="文档解析摘要">
        <span><Layers3 size={14} />{status === "READY" ? (sourceChunkCount === null ? "正在读取正文分片" : `${sourceChunkCount} 个正文分片`) : statusMeta(document).label}</span>
        <span>{document.page_count == null ? "页数未记录" : `${document.page_count} 页`}</span>
        <span>{status === "READY" ? `解析耗时 ${formatDuration(document.parse_time_ms)}` : `已尝试 ${Number(document.attempt_count || 0)} 次`}</span>
        <span>版本 v{document.version ?? 1}</span>
      </section>

      {documentError ? <div className="notice notice--error" role="alert"><AlertCircle size={16} /><p>{documentError}</p><button type="button" onClick={() => setDocumentError("")} aria-label="关闭错误">×</button></div> : null}

      {status === "READY" && parseQuality.warnings.length ? (
        <div className="notice notice--warning" role="status">
          <AlertCircle size={16} />
          <div>
            <strong>文档已入库，但有 {parseQuality.warnings.length} 项解析提醒</strong>
            <ul>
              {parseQuality.warnings.map((warning) => (
                <li key={warning}>{formatParseQualityWarning(warning)}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      {status !== "READY" ? (
        <section className={`panel document-processing-state document-processing-state--${status.toLowerCase()}`}>
          <span className="document-processing-state__icon">{status === "FAILED" ? <AlertCircle size={24} /> : status === "QUEUED" ? <Clock3 size={24} /> : <Loader2 className="spin" size={24} />}</span>
          <div>
            <p className="eyebrow">{status === "FAILED" ? "解析未完成" : "后台解析队列"}</p>
            <h2>{status === "FAILED" ? "当前版本解析失败" : status === "QUEUED" ? "文档正在等待处理" : "正在解析并建立检索索引"}</h2>
            <p>{status === "FAILED" ? (document.error_message || "解析或索引阶段出现异常，请重试后查看文档。") : "完整文档与分片边界会在解析和检索索引全部完成后开放。"}</p>
            {document.error_code ? <code>{document.error_code}</code> : null}
          </div>
          <Link className="button button--secondary" to="/tasks"><Workflow size={15} />查看解析队列</Link>
        </section>
      ) : (
        <section className="document-reader" aria-busy={loadingPreview}>
          <header className="document-reader__toolbar">
            <div>
              <div className="document-reader__title-line">
                <h2>文档内容</h2>
                <span>v{document.version ?? 1}</span>
              </div>
              <p>按原文连续展示；分割线标记正文进入检索索引的位置。</p>
            </div>
            <div className="document-reader__actions">
              <select aria-label="跳转到分片" value={jumpTarget} onChange={(event) => jumpToSection(event.target.value)} disabled={!readerBoundaries.length}>
                <option value="">跳转到分片</option>
                {readerBoundaries.map((entry) => <option key={entry.anchorId} value={entry.anchorId}>分片 {String(entry.readerIndex + 1).padStart(2, "0")}</option>)}
              </select>
              <button type="button" className="button button--secondary" onClick={() => setShowBoundaries((current) => !current)} disabled={!readerBoundaries.length}>{showBoundaries ? <EyeOff size={15} /> : <Eye size={15} />}{showBoundaries ? "隐藏分片线" : "显示分片线"}</button>
              <button type="button" className="button button--secondary" onClick={() => { void copyText(preview?.content || "", "document"); }} disabled={!preview?.content}>{copiedValue === "document" ? <Check size={14} /> : <Copy size={14} />}{copiedValue === "document" ? "已复制" : "复制全文"}</button>
            </div>
          </header>

          {previewError ? <div className="notice notice--error document-reader__notice" role="alert"><AlertCircle size={16} /><p>{previewError}</p><button type="button" className="button button--tiny" onClick={() => setPreviewRefreshKey((current) => current + 1)}>重试加载</button>{preview ? <button type="button" onClick={() => setPreviewError("")} aria-label="关闭加载错误">×</button> : null}</div> : null}

          {preview?.reparse_required ? <div className="notice notice--warning document-reader__notice document-reader__notice--compact"><AlertCircle size={15} /><div><strong>历史版本边界：</strong><span>原文可正常阅读，分片线按现有行号恢复；重新解析后可获得精确边界。</span></div></div> : null}
          {preview?.boundary_precision === "approximate_line" ? <div className="notice notice--warning document-reader__notice document-reader__notice--compact"><AlertCircle size={15} /><div><strong>近似位置：</strong><span>语义切分可能位于段落内部，分片线显示在最近的安全文档结构边缘；同一位置会完整保留多个边界。</span></div></div> : null}

          {loadingPreview && !preview ? (
            <div className="document-reader__loading"><Loader2 className="spin" size={20} /><p>正在还原完整文档…</p></div>
          ) : !preview ? (
            <div className="empty-state document-reader__empty document-reader__empty--error"><AlertCircle size={22} /><h3>文档内容暂时无法加载</h3><p>{previewError || "请重试加载，或返回数据集确认文档解析状态。"}</p><button type="button" className="button button--secondary" onClick={() => setPreviewRefreshKey((current) => current + 1)}><RefreshCw size={15} />重新加载</button></div>
          ) : preview.content ? (
            <div className="document-reader__canvas">
              <article className="document-reader__paper" aria-label={`${document.filename} 的解析后正文`}>
                <div className="document-reader-markdown">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm, remarkMath, structuredTablePlugin, boundaryPlugin, remarkDocumentHtmlTables]}
                    rehypePlugins={[rehypeKatex]}
                    components={markdownComponents}
                  >
                    {renderedPreviewContent}
                  </ReactMarkdown>
                </div>
              </article>
            </div>
          ) : (
            <div className="empty-state document-reader__empty"><FileText size={22} /><h3>当前版本没有可展示正文</h3><p>解析已完成，但没有生成可阅读的文档内容，可以尝试重新解析。</p></div>
          )}
        </section>
      )}
    </div>
  );
}

export default DocumentDetailPage;
