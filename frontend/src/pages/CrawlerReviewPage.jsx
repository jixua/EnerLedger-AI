import { useCallback, useEffect, useState } from "react";
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
  ShieldCheck,
  X,
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

export function CrawlerReviewPage() {
  const { actions = {} } = useApp();
  const [reviewStatus, setReviewStatus] = useState("PENDING");
  const [submissions, setSubmissions] = useState([]);
  const [submissionTotal, setSubmissionTotal] = useState(0);
  const [reviewsLoading, setReviewsLoading] = useState(true);
  const [reviewActionId, setReviewActionId] = useState(null);
  const [reviewError, setReviewError] = useState("");
  const [preview, setPreview] = useState(null);

  const loadReviewQueue = useCallback(async (signal) => {
    setReviewsLoading(true);
    setReviewError("");
    try {
      const response = await listCrawlerSubmissions(
        { reviewStatus, limit: 100 },
        { signal },
      );
      setSubmissions(response.items || []);
      setSubmissionTotal(Number(response.total || 0));
    } catch (requestError) {
      if (requestError?.name !== "AbortError") {
        setReviewError(requestError?.message || "审核队列加载失败");
      }
    } finally {
      if (!signal?.aborted) setReviewsLoading(false);
    }
  }, [reviewStatus]);

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
      const blob = await getCrawlerSubmissionFile(submission.document_id);
      const kind = submissionPreviewKind(submission);
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

  function downloadPreview() {
    if (!preview?.blob) return;
    const objectUrl = URL.createObjectURL(preview.blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = preview.submission.filename;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
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
      await reviewCrawlerSubmission(submission.document_id, { decision, note });
      await loadReviewQueue();
      if (decision === "APPROVED") {
        void Promise.resolve(actions.loadDocuments?.(submission.dataset_id)).catch(() => {});
      }
    } catch (requestError) {
      setReviewError(requestError?.message || "审核操作失败");
    } finally {
      setReviewActionId(null);
    }
  }

  return (
    <div className="page crawler-page">
      <header className="crawler-hero">
        <span className="crawler-hero__icon"><ShieldCheck size={22} /></span>
        <div>
          <p className="eyebrow">内容准入</p>
          <h1>采集资料审核</h1>
          <p>审核第三方爬虫上传的文章或论文；只有人工通过后，文件才会进入解析队列。</p>
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
                <div className="crawler-paper__meta">
                  <span>{submission.dataset_name}</span>
                  <span>{formatBytes(submission.file_size)}</span>
                  <span>{formatDate(submission.created_at)}</span>
                  <span>{submission.source_metadata?.source_name || submission.source_metadata?.crawler_name || "external-client"}</span>
                </div>
                <h3>{submission.source_title || submission.filename}</h3>
                <p>{submission.filename}</p>
                {submission.source_url ? <a href={submission.source_url} target="_blank" rel="noreferrer">查看来源页面<ExternalLink size={13} /></a> : null}
                {submission.review_note ? <small>审核备注：{submission.review_note}</small> : null}
              </div>
              <div className="crawler-review-card__actions">
                <button className="button button--secondary" type="button" onClick={() => handleOpenSubmission(submission)} disabled={Boolean(reviewActionId)}>
                  {busy ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />}审核预览
                </button>
                {submission.review_status === "PENDING" ? (
                  <>
                    <button className="button button--danger" type="button" onClick={() => handleReview(submission, "REJECTED")} disabled={Boolean(reviewActionId)}><XCircle size={15} />拒绝</button>
                    <button className="button button--primary" type="button" onClick={() => handleReview(submission, "APPROVED")} disabled={Boolean(reviewActionId)}><CheckCircle2 size={15} />通过并解析</button>
                  </>
                ) : null}
              </div>
            </article>
          );
        })}
      </section>

      {preview ? (
        <div className="dialog-backdrop crawler-preview-backdrop" role="presentation" onMouseDown={() => setPreview(null)}>
          <section className="dialog crawler-preview-dialog" role="dialog" aria-modal="true" aria-labelledby="crawler-preview-title" onMouseDown={(event) => event.stopPropagation()}>
            <header className="dialog__header">
              <div>
                <p className="eyebrow">原文件审核</p>
                <h2 id="crawler-preview-title">{preview.submission.source_title || preview.submission.filename}</h2>
                <p className="dialog__subtitle">{preview.submission.filename} · {preview.submission.dataset_name}</p>
              </div>
              <button className="icon-button" type="button" aria-label="关闭审核预览" onClick={() => setPreview(null)}><X size={18} /></button>
            </header>
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
            <footer className="dialog__footer crawler-preview-dialog__footer">
              <button className="button button--secondary" type="button" onClick={downloadPreview}><Download size={15} />下载原文件</button>
              <button className="button button--primary" type="button" onClick={() => setPreview(null)}>完成查看</button>
            </footer>
          </section>
        </div>
      ) : null}
    </div>
  );
}
