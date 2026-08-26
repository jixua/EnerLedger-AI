import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertCircle,
  BookOpenText,
  Download,
  ExternalLink,
  Globe2,
  LoaderCircle,
  Search,
  UploadCloud,
} from "lucide-react";
import {
  importArxivPapers,
  searchArxivPapers,
} from "../lib/api";
import { useApp } from "../state/AppContext";

const DEFAULT_QUERY = "carbon footprint";

function formatDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(value));
}

function markdownFor(result) {
  const sections = result.items.map((paper, index) => [
    `## ${index + 1}. ${paper.title}`,
    "",
    `- arXiv ID：${paper.arxiv_id}`,
    `- 作者：${paper.authors.join("、") || "未知"}`,
    `- 发布时间：${formatDate(paper.published_at)}`,
    `- 分类：${paper.categories.join("、") || "未标注"}`,
    `- 摘要页：${paper.abstract_url}`,
    "",
    paper.summary,
  ].join("\n"));
  return [
    `# arXiv 论文采集：${result.query}`,
    "",
    `检索优化：${result.optimized_query || result.query}`,
    `优化方式：${result.optimization_mode === "AI" ? `AI（${result.optimization_model || "对话模型"}）` : "规则"}`,
    `采集时间：${new Date(result.fetched_at).toLocaleString("zh-CN")}`,
    `匹配总数：${result.total_results}`,
    "",
    ...sections,
  ].join("\n\n");
}

function downloadMarkdown(result) {
  const blob = new Blob([markdownFor(result)], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `arxiv-${result.query.replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-|-$/g, "") || "papers"}.md`;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function CrawlerPage() {
  const { datasets = [], actions = {} } = useApp();
  const [query, setQuery] = useState(DEFAULT_QUERY);
  const [maxResults, setMaxResults] = useState(10);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selectedIds, setSelectedIds] = useState([]);
  const [datasetId, setDatasetId] = useState("");
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState(null);
  const selectedDataset = useMemo(
    () => datasets.find((dataset) => String(dataset.id) === String(datasetId)),
    [datasetId, datasets],
  );
  const visibleSelectionIds = useMemo(
    () => (result?.items || []).slice(0, 10).map((paper) => paper.arxiv_id),
    [result],
  );
  const allVisibleSelected = visibleSelectionIds.length > 0
    && visibleSelectionIds.every((id) => selectedIds.includes(id));
  const resultSummary = useMemo(() => {
    if (!result) return "";
    return `找到约 ${result.total_results.toLocaleString("zh-CN")} 篇，当前展示 ${result.items.length} 篇。`;
  }, [result]);

  async function handleSubmit(event) {
    event.preventDefault();
    const normalized = query.trim();
    if (!datasetId || normalized.length < 2 || loading) return;
    setLoading(true);
    setError("");
    setImportResult(null);
    try {
      const nextResult = await searchArxivPapers({
        query: normalized,
        maxResults,
        datasetId,
        aiOptimize: true,
      });
      setResult(nextResult);
      setSelectedIds([]);
    } catch (requestError) {
      setError(requestError?.message || "论文采集失败，请稍后重试");
    } finally {
      setLoading(false);
    }
  }

  function handleDatasetChange(nextDatasetId) {
    setDatasetId(nextDatasetId);
    setResult(null);
    setSelectedIds([]);
    setImportResult(null);
    setError("");
  }

  function togglePaper(arxivId) {
    setSelectedIds((current) => {
      if (current.includes(arxivId)) return current.filter((value) => value !== arxivId);
      if (current.length >= 10) return current;
      return [...current, arxivId];
    });
  }

  function toggleVisiblePapers() {
    setSelectedIds(allVisibleSelected ? [] : visibleSelectionIds);
  }

  async function handleImport() {
    if (!datasetId || selectedIds.length === 0 || importing) return;
    const targetDatasetId = Number(datasetId);
    setImporting(true);
    setError("");
    setImportResult(null);
    try {
      const selectedPapers = result.items
        .filter((paper) => selectedIds.includes(paper.arxiv_id))
        .map((paper) => ({ arxiv_id: paper.arxiv_id, title: paper.title }));
      const response = await importArxivPapers({ datasetId, papers: selectedPapers });
      setImportResult(response);
      const queuedIds = new Set(
        response.items.filter((item) => item.status === "QUEUED").map((item) => item.arxiv_id),
      );
      setSelectedIds((current) => current.filter((id) => !queuedIds.has(id)));
      setImporting(false);
      void Promise.resolve(actions.loadDocuments?.(targetDatasetId)).catch(() => {});
    } catch (requestError) {
      setError(requestError?.message || "论文导入失败，请稍后重试");
    } finally {
      setImporting(false);
    }
  }

  return (
    <div className="page crawler-page">
      <header className="crawler-hero">
        <span className="crawler-hero__icon"><Globe2 size={22} /></span>
        <div>
          <p className="eyebrow">论文采集</p>
          <h1>arXiv 论文采集</h1>
          <p>按关键词检索 arXiv 论文，选择后导入指定知识库并进入解析队列。</p>
        </div>
      </header>

      <form className="panel crawler-search" onSubmit={handleSubmit}>
        <p className="crawler-search__title">选择目标数据集后检索</p>
        <div className="crawler-search__controls">
          <div className="crawler-search__input">
            <Search size={17} />
            <input
              id="crawler-query"
              aria-label="检索关键词"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              maxLength={120}
              placeholder="例如：carbon accounting"
            />
          </div>
          <label className="crawler-search__dataset">
            <span>目标数据集</span>
            <select value={datasetId} onChange={(event) => handleDatasetChange(event.target.value)}>
              <option value="">请选择数据集</option>
              {datasets.map((dataset) => <option value={dataset.id} key={dataset.id}>{dataset.name}</option>)}
            </select>
          </label>
          <label className="crawler-search__limit">
            <span>采集数量</span>
            <select value={maxResults} onChange={(event) => setMaxResults(Number(event.target.value))}>
              <option value={5}>5 篇</option>
              <option value={10}>10 篇</option>
              <option value={20}>20 篇</option>
            </select>
          </label>
          <button className="button button--primary" type="submit" disabled={!datasetId || loading || query.trim().length < 2}>
            {loading ? <LoaderCircle className="spin" size={16} /> : <Search size={16} />}
            {loading ? "正在采集" : "开始采集"}
          </button>
        </div>
      </form>

      {error ? <div className="crawler-error" role="alert"><AlertCircle size={17} /><span>{error}</span></div> : null}

      <section className="crawler-results" aria-live="polite">
        <div className="crawler-results__header">
          <div>
            <p className="eyebrow">采集结果</p>
            {resultSummary ? <h2>{resultSummary}</h2> : null}
          </div>
          {result?.items.length ? (
            <div className="crawler-results__actions">
              <button className="button button--secondary" type="button" onClick={toggleVisiblePapers}>
                {allVisibleSelected ? "取消全选" : result.items.length > 10 ? "选择前 10 篇" : "全选当前结果"}
              </button>
              <button className="button button--secondary" type="button" onClick={() => downloadMarkdown(result)}>
                <Download size={15} />导出 Markdown
              </button>
            </div>
          ) : null}
        </div>

        {result?.optimization_warning ? (
          <div className="crawler-query-warning" role="status">
            <AlertCircle size={16} />
            <span>{result.optimization_warning}</span>
          </div>
        ) : null}

        {result?.items.length ? (
          <div className="panel crawler-import">
            <div className="crawler-import__copy">
              <span className="crawler-import__icon"><UploadCloud size={18} /></span>
              <div><strong>导入知识库并解析</strong><p>已选择 {selectedIds.length}/10 篇；下载完成后将进入现有文档解析队列。</p></div>
            </div>
            <div className="crawler-import__controls">
              <span className="crawler-import__target">导入到：{selectedDataset?.name || "当前数据集"}</span>
              <button className="button button--primary" type="button" onClick={handleImport} disabled={!datasetId || !selectedIds.length || importing}>
                {importing ? <LoaderCircle className="spin" size={16} /> : <UploadCloud size={16} />}
                {importing ? "逐篇下载并入队" : "导入并解析"}
              </button>
            </div>
          </div>
        ) : null}

        {importResult ? (
          <div className={`crawler-import-result ${importResult.failed_count ? "crawler-import-result--warning" : ""}`} role="status">
            <strong>已入队 {importResult.queued_count} 篇，失败 {importResult.failed_count} 篇。</strong>
            <Link to={`/datasets/${importResult.dataset_id}`}>查看数据集文档</Link>
            {importResult.items.filter((item) => item.status === "FAILED").map((item) => <p key={item.arxiv_id}>{item.arxiv_id}：{item.message}</p>)}
          </div>
        ) : null}

        {!result && !loading ? (
          <div className="empty-state crawler-empty"><BookOpenText size={25} /><h3>等待采集</h3><p>{datasets.length ? "请先选择目标数据集，再输入主题开始检索。" : <><Link to="/datasets?create=1">先创建数据集</Link>，再开始论文采集。</>}</p></div>
        ) : null}

        {result?.items.map((paper) => {
          const selected = selectedIds.includes(paper.arxiv_id);
          return (
          <article className={`panel crawler-paper ${selected ? "crawler-paper--selected" : ""}`} key={paper.arxiv_id}>
            <label className="crawler-paper__select">
              <input type="checkbox" checked={selected} onChange={() => togglePaper(paper.arxiv_id)} disabled={!selected && selectedIds.length >= 10} />
              <span>{selected ? "已选择" : "选择论文"}</span>
            </label>
            <div className="crawler-paper__meta">
              <span>{paper.arxiv_id}</span>
              <span>{formatDate(paper.published_at)}</span>
              {paper.categories.slice(0, 3).map((category) => <span key={category}>{category}</span>)}
            </div>
            <h3>{paper.title}</h3>
            <p className="crawler-paper__authors">{paper.authors.join("、") || "作者信息缺失"}</p>
            <p className="crawler-paper__summary">{paper.summary}</p>
            <footer>
              <a className="button button--secondary" href={paper.abstract_url} target="_blank" rel="noreferrer">摘要页<ExternalLink size={14} /></a>
              <a className="button button--secondary" href={paper.pdf_url} target="_blank" rel="noreferrer">查看 PDF<ExternalLink size={14} /></a>
            </footer>
          </article>
          );
        })}

        {result && result.items.length === 0 ? (
          <div className="empty-state crawler-empty"><BookOpenText size={25} /><h3>没有匹配结果</h3><p>请尝试更短或更常用的英文关键词。</p></div>
        ) : null}
      </section>
    </div>
  );
}
