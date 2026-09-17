import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertCircle,
  BookOpenText,
  CheckCircle2,
  Download,
  ExternalLink,
  LoaderCircle,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { formatBytes } from "../components/ui";
import {
  getCrawlerSubmissionFile,
  listCrawlerSubmissions,
  reviewCrawlerSubmission,
} from "../lib/api";
import { useApp } from "../state/AppContext";

function formatDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(value));
}

const MARKDOWN_FILE_TYPES = new Set(["md", "markdown"]);

function submissionPreviewKind(submission) {
  const fileType = String(submission?.file_type || "").toLowerCase();
  if (MARKDOWN_FILE_TYPES.has(fileType)) return "markdown";
  if (fileType === "pdf") return "pdf";
  if (["doc", "docx"].includes(fileType)) return "word";
  return "download";
}

const DEMO_SUBMISSIONS = [
  {
    document_id: 9101,
    dataset_id: 2001,
    dataset_name: "企业碳排放核算库",
    filename: "企业温室气体排放核算与报告指南.pdf",
    file_type: "pdf",
    source_title: "企业温室气体排放核算与报告指南.pdf",
    file_size: 2936012,
    created_at: "2026-09-01T11:42:00+08:00",
    review_status: "PENDING",
    source_metadata: { source_name: "external-client" },
  },
  {
    document_id: 9102,
    dataset_id: 2002,
    dataset_name: "节能政策与标准",
    filename: "供应链碳排放数据说明.md",
    file_type: "md",
    source_title: "供应链碳排放数据说明",
    file_size: 18432,
    created_at: "2026-09-01T10:18:00+08:00",
    review_status: "PENDING",
    source_metadata: { source_name: "partner-system" },
  },
  {
    document_id: 9103,
    dataset_id: 2003,
    dataset_name: "产品碳足迹方法库",
    filename: "ISO 14064-1 温室气体规范.docx",
    file_type: "docx",
    source_title: "ISO 14064-1 温室气体规范",
    file_size: 3355443,
    created_at: "2026-08-31T16:06:00+08:00",
    review_status: "PENDING",
    source_metadata: { source_name: "standard-importer" },
  },
];

export function CrawlerReviewPage() {
  const { datasets = [], actions = {}, isDemo = false } = useApp();
  const [reviewStatus, setReviewStatus] = useState("PENDING");
  const [submissions, setSubmissions] = useState([]);
  const [submissionTotal, setSubmissionTotal] = useState(0);
  const [reviewsLoading, setReviewsLoading] = useState(true);
  const [reviewActionId, setReviewActionId] = useState(null);
  const [reviewError, setReviewError] = useState("");
  const [targetDatasetIds, setTargetDatasetIds] = useState({});
  const [demoSubmissions, setDemoSubmissions] = useState(DEMO_SUBMISSIONS);
  const [preview, setPreview] = useState(null);

  const loadReviewQueue = useCallback(async (signal) => {
    setReviewsLoading(true);
    setReviewError("");
    if (isDemo) {
      const filtered = demoSubmissions.filter((submission) => submission.review_status === reviewStatus);
      setSubmissions(filtered);
      setSubmissionTotal(filtered.length);
      setReviewsLoading(false);
      return;
    }
    try {
      const response = await listCrawlerSubmissions(
        { reviewStatus, limit: 100 },
        { signal },
      );
      const items = response.items || [];
      setSubmissions(items);
      setSubmissionTotal(Number(response.total || 0));
      setTargetDatasetIds(Object.fromEntries(
        items.map((submission) => [submission.document_id, String(submission.dataset_id)]),
      ));
    } catch (requestError) {
      if (requestError?.name !== "AbortError") {
        setReviewError(requestError?.message || "审核队列加载失败");
      }
    } finally {
      if (!signal?.aborted) setReviewsLoading(false);
    }
  }, [demoSubmissions, isDemo, reviewStatus]);

  useEffect(() => {
    const controller = new AbortController();
    void loadReviewQueue(controller.signal);
    return () => controller.abort();
  }, [loadReviewQueue]);

  useEffect(() => {
    if (!preview) return undefined;
    function handlePreviewKeyDown(event) {
      if (event.key === "Escape") setPreview(null);
    }
    window.addEventListener("keydown", handlePreviewKeyDown);
    return () => {
      window.removeEventListener("keydown", handlePreviewKeyDown);
      if (preview.objectUrl) URL.revokeObjectURL(preview.objectUrl);
    };
  }, [preview]);

  async function handleOpenSubmission(submission) {
    if (reviewActionId) return;
    setReviewActionId(submission.document_id);
    setReviewError("");
    try {
      const blob = isDemo
        ? new Blob([
          submission.file_type === "md"
            ? `# ${submission.source_title}\n\n该文件用于审核流程演示，包含供应商范围一、范围二及运输环节的碳排放数据说明。`
            : `预览资料：${submission.source_title || submission.filename}`,
        ], { type: "text/markdown;charset=utf-8" })
        : await getCrawlerSubmissionFile(submission.document_id);
      const kind = isDemo ? "markdown" : submissionPreviewKind(submission);
      setPreview({
        submission,
        kind,
        blob,
        markdown: kind === "markdown" ? await blob.text() : null,
        objectUrl: kind === "pdf" ? URL.createObjectURL(blob) : null,
      });
    } catch (requestError) {
      setReviewError(requestError?.message || "原文件打开失败");
    } finally {
      setReviewActionId(null);
    }
  }

  async function handleReview(submission, decision) {
    if (reviewActionId) return;
    let note = null;
    if (decision === "REJECTED") {
      const input = window.prompt("请输入拒绝原因（可选）", "");
      if (input === null) return;
      note = input.trim() || null;
    }
    setReviewActionId(submission.document_id);
    setReviewError("");
    try {
      const targetDatasetId = Number(
        targetDatasetIds[submission.document_id] ?? submission.dataset_id,
      );
      if (decision === "APPROVED" && (!Number.isSafeInteger(targetDatasetId) || targetDatasetId <= 0)) {
        throw new Error("请选择目标数据集");
      }
      if (isDemo) {
        const targetDataset = datasets.find((dataset) => Number(dataset.id) === targetDatasetId);
        setDemoSubmissions((current) => current.map((item) => (
          item.document_id === submission.document_id
            ? {
              ...item,
              review_status: decision,
              review_note: note,
              ...(decision === "APPROVED" ? {
                dataset_id: targetDatasetId,
                dataset_name: targetDataset?.name || item.dataset_name,
              } : {}),
            }
            : item
        )));
        return;
      }
      await reviewCrawlerSubmission(submission.document_id, {
        decision,
        note,
        ...(decision === "APPROVED" ? { datasetId: targetDatasetId } : {}),
      });
      await loadReviewQueue();
      if (decision === "APPROVED") {
        void Promise.resolve(actions.loadDocuments?.(targetDatasetId)).catch(() => {});
      }
    } catch (requestError) {
      setReviewError(requestError?.message || "审核操作失败");
    } finally {
      setReviewActionId(null);
    }
  }

  return (
    <div className="page page--crawler feature-page">
      <header className="knowledge-hero">
        <div className="knowledge-hero__copy">
          <h1>采集资料审核</h1>
          <p className="knowledge-hero__subtitle">审核第三方爬虫上传的文章或论文；只有人工通过后，文件才会进入解析队列。</p>
        </div>
      </header>

      <section className="crawler-review" aria-labelledby="crawler-review-title">
        <div className="crawler-results__header">
          <div>
            <p className="eyebrow">人工审核</p>
            <h2 id="crawler-review-title">第三方上传资料 · {submissionTotal} 项</h2>
          </div>
          <div className="crawler-results__actions crawler-review__actions">
            <select
              className="crawler-review__filter"
              aria-label="审核状态"
              value={reviewStatus}
              onChange={(event) => setReviewStatus(event.target.value)}
            >
              <option value="PENDING">待审核</option>
              <option value="APPROVED">已通过</option>
              <option value="REJECTED">已拒绝</option>
            </select>
            <button className="button button--secondary" type="button" onClick={() => loadReviewQueue()} disabled={reviewsLoading}>
              <RefreshCw className={reviewsLoading ? "spin" : ""} size={15} />刷新
            </button>
          </div>
        </div>

        {reviewError ? <div className="crawler-error" role="alert"><AlertCircle size={17} /><span>{reviewError}</span></div> : null}
        {reviewsLoading ? (
          <div className="panel crawler-review__loading"><LoaderCircle className="spin" size={18} />正在加载审核队列</div>
        ) : null}
        {!reviewsLoading && submissions.length === 0 ? (
          <div className="empty-state crawler-review__empty"><BookOpenText size={24} /><h3>当前没有{reviewStatus === "PENDING" ? "待审核" : "符合条件的"}资料</h3><p>第三方通过上传接口提交后会显示在这里。</p></div>
        ) : null}
        {submissions.map((submission) => {
          const busy = reviewActionId === submission.document_id;
          return (
            <article className="panel crawler-review-card" key={submission.document_id}>
              <div className="crawler-review-card__body">
                <div className="crawler-review-card__meta">
                  <span>{formatBytes(submission.file_size)}</span>
                  <span>{formatDate(submission.created_at)}</span>
                  <span>{submission.source_metadata?.source_name || submission.source_metadata?.crawler_name || "external-client"}</span>
                </div>
                <div className="crawler-review-card__headline">
                  <div className="crawler-review-card__identity">
                    <h3>{submission.source_title || submission.filename}</h3>
                    <p>{submission.filename}</p>
                  </div>
                  <div className="crawler-review-card__actions">
                    <button className="button button--secondary" type="button" onClick={() => handleOpenSubmission(submission)} disabled={Boolean(reviewActionId)}>
                      {busy ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />}审核预览
                    </button>
                    {submission.review_status === "PENDING" ? (
                      <>
                        <button className="button button--danger" type="button" onClick={() => handleReview(submission, "REJECTED")} disabled={Boolean(reviewActionId)}><XCircle size={15} />拒绝</button>
                        <button className="button button--primary" type="button" onClick={() => handleReview(submission, "APPROVED")} disabled={Boolean(reviewActionId) || datasets.length === 0}><CheckCircle2 size={15} />通过并解析</button>
                      </>
                    ) : null}
                  </div>
                </div>
                <label className="crawler-review-card__target">
                  <span>目标数据集</span>
                  {submission.review_status === "PENDING" ? (
                    <select
                      aria-label={`${submission.source_title || submission.filename}的目标数据集`}
                      value={targetDatasetIds[submission.document_id] ?? String(submission.dataset_id)}
                      onChange={(event) => setTargetDatasetIds((current) => ({
                        ...current,
                        [submission.document_id]: event.target.value,
                      }))}
                      disabled={Boolean(reviewActionId)}
                    >
                      {datasets.map((dataset) => <option value={dataset.id} key={dataset.id}>{dataset.name}</option>)}
                    </select>
                  ) : <strong>{submission.dataset_name}</strong>}
                </label>
                {submission.source_url ? <a href={submission.source_url} target="_blank" rel="noreferrer">查看来源页面<ExternalLink size={13} /></a> : null}
                {submission.review_note ? <small>审核备注：{submission.review_note}</small> : null}
              </div>
            </article>
          );
        })}
      </section>

      {preview ? createPortal(
        <div className="dialog-backdrop crawler-preview-backdrop" role="presentation" onMouseDown={() => setPreview(null)}>
          <section
            className="dialog crawler-preview-dialog"
            role="dialog"
            aria-modal="true"
            aria-label={preview.submission.source_title || preview.submission.filename}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="crawler-preview-dialog__body">
              {preview.kind === "pdf" ? (
                <iframe className="crawler-preview-dialog__pdf" src={preview.objectUrl} title={`预览 ${preview.submission.filename}`} />
              ) : null}
              {preview.kind === "markdown" ? (
                <article className="document-reader-markdown crawler-preview-dialog__markdown">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                      img: ({ src, alt }) => <span className="crawler-preview-dialog__image-ref">[图片引用：{alt || src || "未命名"}]</span>,
                    }}
                  >{preview.markdown}</ReactMarkdown>
                </article>
              ) : null}
              {["word", "download"].includes(preview.kind) ? (
                <div className="crawler-preview-dialog__download">
                  <BookOpenText size={28} />
                  <h3>{preview.kind === "word" ? "Word 原文件需下载后审核" : "该原文件需下载后审核"}</h3>
                  <p>{preview.kind === "word" ? "为确保审核通过前不执行文档解析，系统不会提前把 Word 转换为 HTML。请下载原文件并使用本地办公软件查看。" : "该格式不在页面内执行转换或解析，请下载原文件后使用可信的本地软件查看。"}</p>
                </div>
              ) : null}
            </div>
          </section>
        </div>,
        document.body,
      ) : null}
    </div>
  );
}
