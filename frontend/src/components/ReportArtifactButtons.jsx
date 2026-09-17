import { useState } from "react";
import { AlertCircle, Download, Loader2 } from "lucide-react";

import { downloadReportArtifact } from "../lib/api";
import { reportArtifactLabel } from "../lib/reportRun";

/**
 * 产物下载按钮组。报告中心、详情页和对话卡片此前各自复制了一份
 * 「取 blob → 造 a 标签 → revoke」的实现，这里收敛为一处。
 */
export function ReportArtifactButtons({ runId, artifacts, emptyHint = "", className = "" }) {
  const [downloadingId, setDownloadingId] = useState(null);
  const [error, setError] = useState("");

  const items = Array.isArray(artifacts) ? artifacts : [];

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
      {error ? <p className="form-error" role="alert"><AlertCircle size={14} />{error}</p> : null}
    </div>
  );
}

export default ReportArtifactButtons;
