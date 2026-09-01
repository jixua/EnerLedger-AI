import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import {
  AlertCircle,
  BookOpenText,
  CheckCircle2,
  Download,
  FileText,
  Folder,
  Globe2,
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
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const now = new Date();
  const isToday = date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth()
    && date.getDate() === now.getDate();
  if (isToday) {
    return `今天 ${new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date)}`;
  }
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}/${month}/${day}`;
}

const REVIEW_TABS = [
  { value: "PENDING", label: "待审核" },
  { value: "APPROVED", label: "已通过" },
  { value: "REJECTED", label: "已拒绝" },
];

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function buildTextPreview(content, contentType) {
  const policy = "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; font-src data:\">";
  if (contentType.startsWith("text/html")) {
    return /<head(?:\s[^>]*)?>/i.test(content)
      ? content.replace(/<head(?:\s[^>]*)?>/i, (head) => `${head}${policy}`)
      : `${policy}${content}`;
  }
  return `${policy}<style>body{margin:0;padding:28px;color:#243632;background:#fff;font:14px/1.75 ui-monospace,SFMono-Regular,Menlo,monospace}pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}</style><pre>${escapeHtml(content)}</pre>`;
}

const DEMO_SUBMISSIONS = [
  {
    document_id: 9101,
    dataset_id: 4,
    dataset_name: "测试报告",
    filename: "企业温室气体排放核算与报告指南.pdf",
    source_title: "企业温室气体排放核算与报告指南.pdf",
    file_size: 2936012,
    created_at: "2026-09-01T11:42:00+08:00",
    review_status: "PENDING",
    source_metadata: { crawler_name: "external-crawler" },
  },
  {
    document_id: 9102,
    dataset_id: 3,
    dataset_name: "测试数据集2",
    filename: "动力电池全生命周期碳足迹研究.pdf",
    source_title: "动力电池全生命周期碳足迹研究.pdf",
    file_size: 1677722,
    created_at: "2026-09-01T10:18:00+08:00",
    review_status: "PENDING",
    source_metadata: { crawler_name: "arXiv" },
  },
  {
    document_id: 9103,
    dataset_id: 2,
    dataset_name: "测试数据集",
    filename: "ISO 14064-1 温室气体规范.pdf",
    source_title: "ISO 14064-1 温室气体规范.pdf",
    file_size: 3355443,
    created_at: "2026-08-31T16:06:00+08:00",
    review_status: "PENDING",
    source_metadata: { crawler_name: "standard-importer" },
  },
];

export function CrawlerReviewPage() {
  const { actions = {}, isDemo = false } = useApp();
  const [reviewStatus, setReviewStatus] = useState("PENDING");
  const [submissions, setSubmissions] = useState([]);
  const [submissionTotal, setSubmissionTotal] = useState(0);
  const [reviewsLoading, setReviewsLoading] = useState(true);
  const [reviewActionId, setReviewActionId] = useState(null);
  const [reviewError, setReviewError] = useState("");
  const [demoSubmissions, setDemoSubmissions] = useState(DEMO_SUBMISSIONS);
  const [filePreview, setFilePreview] = useState(null);

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
      setSubmissions(response.items || []);
      setSubmissionTotal(Number(response.total || 0));
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
    if (!filePreview) return undefined;
    const handleKeyDown = (event) => {
      if (event.key === "Escape") setFilePreview(null);
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      URL.revokeObjectURL(filePreview.url);
    };
  }, [filePreview]);

  async function handleOpenSubmission(submission) {
    if (reviewActionId) return;
    setReviewActionId(submission.document_id);
    setReviewError("");
    try {
      const blob = isDemo
        ? new Blob([`预览资料：${submission.source_title || submission.filename}`], { type: "text/plain;charset=utf-8" })
        : await getCrawlerSubmissionFile(submission.document_id);
      const contentType = blob.type || submission.content_type || "application/octet-stream";
      const textDocument = contentType.startsWith("text/")
        ? buildTextPreview(await blob.text(), contentType)
        : null;
      const url = URL.createObjectURL(blob);
      setFilePreview({
        submission,
        url,
        contentType,
        textDocument,
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
      if (isDemo) {
        setDemoSubmissions((current) => current.map((item) => (
          item.document_id === submission.document_id
            ? { ...item, review_status: decision, review_note: note }
            : item
        )));
        return;
      }
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
    <div className="page crawler-page crawler-review-page">
      <header className="crawler-review-hero">
        <h1>资料审核</h1>
        <p>第三方资料通过审核后进入解析队列。</p>
      </header>

      <section className="crawler-review" aria-labelledby="crawler-review-title">
        <h2 className="sr-only" id="crawler-review-title">资料审核队列</h2>
        <div className="crawler-review-toolbar">
          <nav className="crawler-review-tabs" role="tablist" aria-label="审核状态">
            {REVIEW_TABS.map((tab) => (
              <button
                key={tab.value}
                type="button"
                role="tab"
                aria-selected={reviewStatus === tab.value}
                className={`crawler-review-tab${reviewStatus === tab.value ? " is-active" : ""}`}
                onClick={() => setReviewStatus(tab.value)}
              >
                {tab.label}
                {reviewStatus === tab.value ? <span>{submissionTotal}</span> : null}
              </button>
            ))}
          </nav>
          <button className="icon-button crawler-review-refresh" type="button" onClick={() => loadReviewQueue()} disabled={reviewsLoading} aria-label="刷新审核队列" title="刷新审核队列">
            <RefreshCw className={reviewsLoading ? "spin" : ""} size={17} />
          </button>
        </div>

        {reviewError ? <div className="crawler-error" role="alert"><AlertCircle size={17} /><span>{reviewError}</span></div> : null}
        <div className="crawler-review-list">
          {submissions.length > 0 ? (
            <header className="crawler-review-list__header" aria-hidden="true">
              <span>资料</span><span>来源与目标</span><span>提交时间</span><span>操作</span>
            </header>
          ) : null}
          {reviewsLoading ? (
            <div className="crawler-review-list__state"><LoaderCircle className="spin" size={18} />正在加载审核队列</div>
          ) : null}
          {!reviewsLoading && submissions.length === 0 ? (
            <div className="empty-state crawler-review__empty"><BookOpenText size={22} /><h3>当前没有{reviewStatus === "PENDING" ? "待审核" : "符合条件的"}资料</h3></div>
          ) : null}
          {submissions.map((submission) => {
            const busy = reviewActionId === submission.document_id;
            const source = submission.source_metadata?.crawler_name || "external-crawler";
            const title = submission.source_title || submission.filename;
            return (
              <article className="crawler-review-row" key={submission.document_id}>
                <div className="crawler-review-row__identity">
                  <span className="crawler-review-row__icon"><FileText size={18} /></span>
                  <span className="crawler-review-row__copy">
                    <strong title={title}>{title}</strong>
                    <small>{formatBytes(submission.file_size)}{title !== submission.filename ? ` · ${submission.filename}` : ""}</small>
                    {submission.review_note ? <em>审核备注：{submission.review_note}</em> : null}
                  </span>
                </div>
                <div className="crawler-review-row__source">
                  <span><Globe2 size={14} />{source}</span>
                  <span><Folder size={14} />{submission.dataset_name}</span>
                </div>
                <time className="crawler-review-row__time">{formatDate(submission.created_at)}</time>
                <div className="crawler-review-row__actions">
                  <button className="button button--secondary" type="button" onClick={() => handleOpenSubmission(submission)} disabled={Boolean(reviewActionId)}>
                    {busy ? <LoaderCircle className="spin" size={14} /> : <Download size={14} />}查看原文件
                  </button>
                  {submission.review_status === "PENDING" ? (
                    <>
                      <button className="crawler-review-row__reject" type="button" onClick={() => handleReview(submission, "REJECTED")} disabled={Boolean(reviewActionId)}><XCircle size={14} />拒绝</button>
                      <button className="button button--primary" type="button" onClick={() => handleReview(submission, "APPROVED")} disabled={Boolean(reviewActionId)}><CheckCircle2 size={14} />通过并解析</button>
                    </>
                  ) : <span className={`crawler-review-row__decision crawler-review-row__decision--${submission.review_status.toLowerCase()}`}>{submission.review_status === "APPROVED" ? "已通过" : "已拒绝"}</span>}
                </div>
              </article>
            );
          })}
        </div>
      </section>

      {filePreview ? createPortal(
        <div className="dialog-backdrop crawler-preview-backdrop" role="presentation" onMouseDown={() => setFilePreview(null)}>
          <section
            className="dialog crawler-preview-dialog"
            role="dialog"
            aria-modal="true"
            aria-label={filePreview.submission.source_title || filePreview.submission.filename}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="crawler-preview-dialog__body">
              {filePreview.contentType.startsWith("image/") ? (
                <img src={filePreview.url} alt={filePreview.submission.source_title || filePreview.submission.filename} />
              ) : filePreview.contentType === "application/pdf" || filePreview.contentType.startsWith("text/") ? (
                <iframe
                  src={filePreview.textDocument ? undefined : filePreview.url}
                  srcDoc={filePreview.textDocument || undefined}
                  title={filePreview.submission.source_title || filePreview.submission.filename}
                  sandbox={filePreview.contentType.startsWith("text/") ? "" : undefined}
                />
              ) : (
                <div className="crawler-preview-dialog__unsupported">
                  <FileText size={34} />
                  <h3>该格式暂不支持在线预览</h3>
                  <p>可以下载原文件后使用本地应用查看，再返回此处完成审核。</p>
                </div>
              )}
            </div>
          </section>
        </div>,
        document.body,
      ) : null}
    </div>
  );
}
