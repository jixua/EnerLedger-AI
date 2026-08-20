import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  BrainCircuit,
  Check,
  Copy,
  Download,
  FileSearch,
  Loader2,
  RefreshCw,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
}

function sourceLocation(source) {
  if (source.page != null) return `第 ${source.page} 页`;
  if (source.page_range?.start != null && source.page_range?.end != null) {
    return `第 ${source.page_range.start}–${source.page_range.end} 页`;
  }
  return "页码未记录";
}

function markdownFilename(filename) {
  const stem = String(filename || "企业文档")
    .replace(/\.[^.]+$/, "")
    .replace(/[\\/:*?"<>|]/g, "-")
    .trim() || "企业文档";
  return `${stem}-分析报告.md`;
}

const ACTIVE_RUN_STATES = new Set(["PENDING", "RUNNING"]);

function runningMessage(stage) {
  if (stage === "SAVING") return "正在保存分析报告";
  if (stage === "WAITING") return "正在启动分析任务";
  return "正在分批提取证据并生成报告";
}

export function DocumentAnalysisPanel({ document, loadDocumentAnalysis, loadDocumentAnalysisStatus, analyzeDocument, downloadDocumentAnalysisDocx }) {
  const documentId = Number(document?.document_id ?? document?.id);
  const documentVersion = Number(document?.version || 1);
  const [analysis, setAnalysis] = useState(null);
  const [analysisError, setAnalysisError] = useState("");
  const [runStatus, setRunStatus] = useState(null);
  const [loadingSaved, setLoadingSaved] = useState(false);
  const [downloadingDocx, setDownloadingDocx] = useState(false);
  const [copied, setCopied] = useState(false);
  const mounted = useRef(false);
  const analyzing = ACTIVE_RUN_STATES.has(runStatus?.state);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setAnalysis(null);
    setAnalysisError("");
    setRunStatus(null);
    setCopied(false);
    if (!loadDocumentAnalysis || !loadDocumentAnalysisStatus) {
      setLoadingSaved(false);
      return () => controller.abort();
    }
    setLoadingSaved(true);
    void (async () => {
      try {
        const status = await loadDocumentAnalysisStatus(documentId, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (Number(status.document_version) !== documentVersion) {
          throw new Error("分析任务对应的文档版本不一致，请刷新后重试");
        }
        setRunStatus(status);
        if (ACTIVE_RUN_STATES.has(status.state)) return;
        if (status.state === "FAILED") {
          throw new Error(status.error_message || "文档分析失败，请重新生成");
        }
        const result = await loadDocumentAnalysis(documentId, { signal: controller.signal });
        if (controller.signal.aborted || !result) return;
        if (Number(result.document_version) !== documentVersion) {
          throw new Error("已保存报告对应的文档版本不一致，请重新生成");
        }
        setAnalysis(result);
      } catch (error) {
        if (controller.signal.aborted || error?.name === "AbortError") return;
        setAnalysisError(error instanceof Error ? error.message : "读取已保存报告失败");
      } finally {
        if (!controller.signal.aborted) setLoadingSaved(false);
      }
    })();
    return () => controller.abort();
  }, [documentId, documentVersion, loadDocumentAnalysis, loadDocumentAnalysisStatus]);

  useEffect(() => {
    if (!analyzing || !loadDocumentAnalysisStatus || !loadDocumentAnalysis) return undefined;
    const controller = new AbortController();
    let timer = null;

    const poll = async () => {
      try {
        const status = await loadDocumentAnalysisStatus(documentId, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (Number(status.document_version) !== documentVersion) {
          throw new Error("分析期间文档版本已变化，请重新生成");
        }
        if (status.state === "SUCCEEDED") {
          const result = await loadDocumentAnalysis(documentId, { signal: controller.signal });
          if (controller.signal.aborted) return;
          if (!result || Number(result.document_version) !== documentVersion) {
            throw new Error("分析已完成，但当前版本报告暂时无法读取，请刷新后重试");
          }
          setAnalysis(result);
          setAnalysisError("");
          setRunStatus(status);
          return;
        }
        if (status.state === "FAILED") {
          setRunStatus(status);
          setAnalysisError(status.error_message || "文档分析失败，请重新生成");
          return;
        }
        setRunStatus(status);
        timer = window.setTimeout(poll, 1800);
      } catch (error) {
        if (controller.signal.aborted || error?.name === "AbortError") return;
        setAnalysisError(error instanceof Error ? error.message : "查询分析任务状态失败");
        timer = window.setTimeout(poll, 3000);
      }
    };

    timer = window.setTimeout(poll, 500);
    return () => {
      controller.abort();
      if (timer) window.clearTimeout(timer);
    };
  }, [analyzing, documentId, documentVersion, loadDocumentAnalysis, loadDocumentAnalysisStatus]);

  async function generateAnalysis() {
    if (!analyzeDocument || analyzing) return;
    setRunStatus({
      run_id: null,
      document_id: documentId,
      document_version: documentVersion,
      state: "PENDING",
      stage: "WAITING",
    });
    setAnalysis(null);
    setAnalysisError("");
    setCopied(false);
    try {
      const result = await analyzeDocument(documentId);
      if (!mounted.current) return;
      if (Number(result.document_version) !== documentVersion) {
        throw new Error("分析任务对应的文档版本已变化，请刷新后重新生成");
      }
      setRunStatus(result);
    } catch (error) {
      if (!mounted.current) return;
      setRunStatus(null);
      setAnalysisError(error instanceof Error ? error.message : "文档分析失败，请稍后重试");
    }
  }

  async function copyMarkdown() {
    if (!analysis?.markdown) return;
    try {
      await navigator.clipboard.writeText(analysis.markdown);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setAnalysisError("无法复制 Markdown，请使用下载功能保存报告");
    }
  }

  function downloadMarkdown() {
    if (!analysis?.markdown) return;
    const blob = new Blob([analysis.markdown], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = window.document.createElement("a");
    link.href = url;
    link.download = markdownFilename(document?.filename);
    link.click();
    URL.revokeObjectURL(url);
  }

  async function downloadDocx() {
    if (!analysis || !downloadDocumentAnalysisDocx || downloadingDocx) return;
    setDownloadingDocx(true);
    setAnalysisError("");
    try {
      const { blob, filename } = await downloadDocumentAnalysisDocx(documentId);
      const url = URL.createObjectURL(blob);
      const link = window.document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setAnalysisError(error instanceof Error ? error.message : "DOCX 报告下载失败");
    } finally {
      setDownloadingDocx(false);
    }
  }

  return (
    <section className="panel document-analysis" aria-busy={loadingSaved || analyzing}>
      <header className="document-analysis__header">
        <div className="document-analysis__heading">
          <span className="document-analysis__icon" aria-hidden="true"><BrainCircuit size={20} /></span>
          <div>
            <p className="eyebrow">大模型辅助分析</p>
            <h2>企业文档分析</h2>
            <p>覆盖当前版本全部主体分片，输出带文档证据引用的 Markdown 报告。</p>
          </div>
        </div>
        <div className="document-analysis__actions">
          {analysis ? <button type="button" className="button button--secondary" onClick={() => { void copyMarkdown(); }}>{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? "已复制" : "复制 Markdown"}</button> : null}
          {analysis ? <button type="button" className="button button--secondary" onClick={downloadMarkdown}><Download size={15} />下载 .md</button> : null}
          {analysis ? <button type="button" className="button button--secondary" onClick={() => { void downloadDocx(); }} disabled={downloadingDocx || !downloadDocumentAnalysisDocx}>{downloadingDocx ? <Loader2 className="spin" size={15} /> : <Download size={15} />}{downloadingDocx ? "正在生成 Word" : "下载 DOCX"}</button> : null}
          <button type="button" className="button button--primary" onClick={() => { void generateAnalysis(); }} disabled={loadingSaved || analyzing || !analyzeDocument}>
            {loadingSaved || analyzing ? <Loader2 className="spin" size={15} /> : analysis ? <RefreshCw size={15} /> : <FileSearch size={15} />}
            {loadingSaved ? "正在读取报告" : analyzing ? "正在分析整份文档" : analysis ? "重新生成" : "生成分析报告"}
          </button>
        </div>
      </header>

      {analysisError ? <div className="notice notice--error document-analysis__notice" role="alert"><AlertCircle size={16} /><p>{analysisError}</p><button type="button" onClick={() => setAnalysisError("")} aria-label="关闭分析错误">×</button></div> : null}

      {loadingSaved ? (
        <div className="document-analysis__loading">
          <Loader2 className="spin" size={22} />
          <div><strong>正在读取已保存的分析报告</strong><p>报告按当前文档版本从 MinIO 加载。</p></div>
        </div>
      ) : analyzing ? (
        <div className="document-analysis__loading">
          <Loader2 className="spin" size={22} />
          <div><strong>{runningMessage(runStatus?.stage)}</strong><p>任务会在后台继续运行，可以离开此页面；返回后将自动恢复状态并展示已完成的报告。</p></div>
        </div>
      ) : !analysis ? (
        <div className="document-analysis__empty">
          <p>报告将按产品碳足迹评价报告的逻辑，覆盖评价对象、方法、边界、功能单位、生命周期清单、核算结果、数据质量、不确定性、规范性和资料缺口。</p>
          <small>本功能不替代正式第三方核查、审计、认证或法规意见；生成成功后会按当前文档版本保存到 MinIO，也可下载 Markdown 或 DOCX。</small>
        </div>
      ) : (
        <>
          <div className="document-analysis__meta" aria-label="分析报告生成信息">
            <span>{analysis.model_name || `模型配置 #${analysis.model_config_id}`}</span>
            <span>{analysis.analyzed_chunk_count} 个主体分片</span>
            <span>{analysis.evidence_batch_count} 个证据批次</span>
            <span>{formatTime(analysis.generated_at)}</span>
          </div>
          <article className="document-analysis__report" aria-label={`${document?.filename || "企业文档"}的分析报告`}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{analysis.markdown}</ReactMarkdown>
          </article>
          {analysis.sources?.length ? (
            <details className="document-analysis__sources">
              <summary>查看报告引用的 {analysis.sources.length} 个文档片段</summary>
              <ol>
                {analysis.sources.map((source) => (
                  <li key={source.citation_index}>
                    <strong>[文档片段{source.citation_index}]</strong>
                    <span>{sourceLocation(source)} · 分片 {source.chunk_index + 1} · {source.chunk_type}</span>
                    {source.excerpt ? <p>{source.excerpt}</p> : null}
                    <code>{source.chunk_id}</code>
                  </li>
                ))}
              </ol>
            </details>
          ) : <p className="document-analysis__source-warning">本次模型输出没有形成可映射的文档片段引用，建议重新生成后再使用结论。</p>}
        </>
      )}
    </section>
  );
}

export default DocumentAnalysisPanel;
