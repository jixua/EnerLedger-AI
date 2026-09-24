import { useEffect, useRef, useState } from "react";
import { AlertCircle, ChevronDown, Download, Loader2 } from "lucide-react";

import { downloadReportArtifact } from "../lib/api";
import { reportArtifactFormat, reportArtifactLabel } from "../lib/reportRun";

/**
 * 产物下载。报告中心、详情页和对话卡片此前各自复制了一份「取 blob → 造 a 标签
 * → revoke」的实现，这里收敛为一处。
 *
 * variant="buttons"：逐个格式一个按钮，用于对话卡片这类横向空间宽裕的位置。
 * variant="menu"：收成一个「下载」按钮（只有一种格式时直接就是它），用于页面头部。
 */
export function ReportArtifactButtons({
  runId,
  artifacts,
  variant = "buttons",
  emptyHint = "",
  className = "",
}) {
  const [downloadingId, setDownloadingId] = useState(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);

  const items = Array.isArray(artifacts) ? artifacts : [];

  useEffect(() => {
    if (!open) return undefined;
    const handlePointerDown = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false);
    };
    const handleKeyDown = (event) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  async function handleDownload(artifact) {
    if (!runId || downloadingId) return;
    setDownloadingId(artifact.id);
    setError("");
    try {
      const { blob, filename } = await downloadReportArtifact(runId, artifact.id);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename || "报告";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "报告下载失败");
    } finally {
      setDownloadingId(null);
    }
  }

  const errorNode = error ? (
    <p className="form-error" role="alert"><AlertCircle size={14} />{error}</p>
  ) : null;

  if (variant === "menu") {
    const busy = Boolean(downloadingId);
    return (
      <div className={`report-download ${className}`.trim()} ref={rootRef}>
        {!items.length ? (
          // 没有可下载格式时不禁默：留一个禁用的入口并说明原因，避免读者以为漏了按钮
          <button type="button" className="button button--secondary" disabled title={emptyHint}>
            <Download size={15} />下载
          </button>
        ) : items.length === 1 ? (
          <button
            type="button"
            className="button button--primary"
            disabled={busy}
            onClick={() => { void handleDownload(items[0]); }}
          >
            {busy ? <Loader2 className="spin" size={15} /> : <Download size={15} />}
            {reportArtifactLabel(items[0].artifact_type)}
          </button>
        ) : (
          <>
            <button
              type="button"
              className="button button--primary"
              aria-haspopup="menu"
              aria-expanded={open}
              disabled={busy}
              onClick={() => setOpen((value) => !value)}
            >
              {busy ? <Loader2 className="spin" size={15} /> : <Download size={15} />}
              下载<ChevronDown size={14} />
            </button>
            {open ? (
              <div className="report-download__menu" role="menu">
                {items.map((artifact) => (
                  <button
                    key={artifact.id}
                    type="button"
                    role="menuitem"
                    disabled={busy}
                    onClick={() => { setOpen(false); void handleDownload(artifact); }}
                  >
                    {reportArtifactFormat(artifact.artifact_type)}
                  </button>
                ))}
              </div>
            ) : null}
          </>
        )}
        {errorNode}
      </div>
    );
  }

  if (!items.length) {
    return emptyHint ? <p className="report-artifacts__hint">{emptyHint}</p> : null;
  }

  return (
    <div className={`report-artifacts ${className}`.trim()}>
      {items.map((artifact) => (
        <button
          key={artifact.id}
          type="button"
          className="button button--secondary"
          disabled={Boolean(downloadingId)}
          onClick={() => { void handleDownload(artifact); }}
        >
          {downloadingId === artifact.id ? <Loader2 className="spin" size={15} /> : <Download size={15} />}
          {reportArtifactLabel(artifact.artifact_type)}
        </button>
      ))}
      {errorNode}
    </div>
  );
}

export default ReportArtifactButtons;
