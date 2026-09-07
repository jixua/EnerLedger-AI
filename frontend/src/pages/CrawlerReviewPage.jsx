import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  BookOpenText,
  CheckCircle2,
  Download,
  ExternalLink,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
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

export function CrawlerReviewPage() {
  const { datasets = [], actions = {} } = useApp();
  const [reviewStatus, setReviewStatus] = useState("PENDING");
  const [submissions, setSubmissions] = useState([]);
  const [submissionTotal, setSubmissionTotal] = useState(0);
  const [reviewsLoading, setReviewsLoading] = useState(true);
  const [reviewActionId, setReviewActionId] = useState(null);
  const [reviewError, setReviewError] = useState("");
  const [targetDatasetIds, setTargetDatasetIds] = useState({});

  const loadReviewQueue = useCallback(async (signal) => {
    setReviewsLoading(true);
    setReviewError("");
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
  }, [reviewStatus]);

  useEffect(() => {
    const controller = new AbortController();
    void loadReviewQueue(controller.signal);
    return () => controller.abort();
  }, [loadReviewQueue]);

  async function handleOpenSubmission(submission) {
    if (reviewActionId) return;
    setReviewActionId(submission.document_id);
    setReviewError("");
    try {
      const blob = await getCrawlerSubmissionFile(submission.document_id);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.target = "_blank";
      anchor.rel = "noreferrer";
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
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
                  <span>{formatBytes(submission.file_size)}</span>
                  <span>{formatDate(submission.created_at)}</span>
                  <span>{submission.source_metadata?.crawler_name || "external-crawler"}</span>
                </div>
                <div className="crawler-review-card__headline">
                  <div className="crawler-review-card__identity">
                    <h3>{submission.source_title || submission.filename}</h3>
                    <p>{submission.filename}</p>
                  </div>
                  <div className="crawler-review-card__actions">
                    <button className="button button--secondary" type="button" onClick={() => handleOpenSubmission(submission)} disabled={Boolean(reviewActionId)}>
                      {busy ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />}查看原文件
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
    </div>
  );
}
